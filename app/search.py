"""检索：默认精确模式——原文逐字包含查询词，文件名也参与匹配；结果按文件分组。

可选语义模式（DOCSEARCH_SEMANTIC=1）：BM25（jieba 分词 + FTS5）与向量（sqlite-vec）用 RRF 融合，可选重排。
"""
import json
import time

import sqlite_vec

from . import config, db, embedder, textproc

RRF_K = 60
CANDIDATES = 100
KEYWORD_LIMIT = 2000  # 精确模式最多统计多少个命中段落
MODES = ("keyword", "hybrid", "semantic") if config.SEMANTIC else ("keyword",)


def terms_of(q: str) -> list[str]:
    """查询词：空格分隔，不区分大小写。"""
    return list(dict.fromkeys(t for t in q.lower().split() if t))


def search(q: str, mode: str = "keyword", top_k: int = 100, doc_id: int | None = None) -> dict:
    """返回按文件分组的结果：每个文件列出命中的段落（按页码排序）和命中次数。top_k 是最多返回几个文件。"""
    t0 = time.time()
    q = q.strip()
    tokens = textproc.query_tokens(q)
    terms = terms_of(q)
    scores: dict[int, float] = {}
    reranked = False
    semantic_ok = True
    truncated = False

    with db.session() as conn:
        if mode == "keyword":
            ids = _keyword(conn, terms, doc_id)
            truncated = len(ids) >= KEYWORD_LIMIT
            for rank, cid in enumerate(ids):
                scores[cid] = 1 / (RRF_K + rank + 1)
        else:
            if mode == "hybrid" and tokens:
                for rank, cid in enumerate(_fts(conn, textproc.fts_query(tokens, "OR"), doc_id, CANDIDATES)):
                    scores[cid] = scores.get(cid, 0) + 1 / (RRF_K + rank + 1)
            try:
                vec_ids = _vector(conn, q, doc_id, CANDIDATES)
            except RuntimeError:  # embedding 服务不可用：综合模式退回纯关键字，语义模式只能报错
                if mode != "hybrid" or not tokens:
                    raise
                vec_ids, semantic_ok = [], False
            for rank, cid in enumerate(vec_ids):
                scores[cid] = scores.get(cid, 0) + 1 / (RRF_K + rank + 1)

        rows = _load(conn, list(scores))
        # 各条 SELECT 不在同一快照里，期间被删除/重新解析的片段在这里会查不到
        scores = {cid: s for cid, s in scores.items() if cid in rows}
        if mode == "hybrid" and terms:
            # 原文里逐字出现查询词的，排到前面（标书检索很依赖编号、名称的精确命中）
            for cid, r in rows.items():
                low = r["text"].lower()
                scores[cid] += 0.03 * sum(t in low for t in terms) / len(terms)

        ranked = sorted(scores, key=scores.get, reverse=True)
        if mode != "keyword" and semantic_ok:  # 向量服务挂了，重排服务（同一台机器）多半也连不上
            head = ranked[:30]
            rr = embedder.rerank(q, [(str(c), f"{rows[c]['heading'] or ''}\n{rows[c]['text']}") for c in head])
            if rr:
                reranked = True
                head.sort(key=lambda c: rr.get(str(c), -1e9), reverse=True)
                ranked = head + ranked[30:]

        name_hits = _filename_hits(conn, terms, doc_id) if terms else {}

    docs: dict[int, dict] = {}
    for rank, cid in enumerate(ranked):
        r = rows[cid]
        low = r["text"].lower()
        count = sum(low.count(t) for t in terms)
        d = docs.setdefault(r["doc_id"], {"doc_id": r["doc_id"], "filename": r["filename"], "rank": rank,
                                          "hit_count": 0, "chunks": []})
        d["hit_count"] += count
        regions = json.loads(r["regions"])
        d["chunks"].append({
            "chunk_id": cid,
            "seq": r["seq"],
            "kind": r["kind"],
            "heading": r["heading"],
            "text": r["text"],
            "page": r["page_start"] + 1,
            "pages": sorted({int(g[0]) + 1 for g in regions}),
            "count": count,
        })
    for did, filename in name_hits.items():
        docs.setdefault(did, {"doc_id": did, "filename": filename, "rank": len(ranked), "hit_count": 0, "chunks": []})
    for d in docs.values():
        d["filename_match"] = d["doc_id"] in name_hits
        d["chunks"].sort(key=lambda c: (c["page"], c["seq"]))

    if mode == "keyword":  # 文件名命中的排前面，其次按正文命中次数
        order = sorted(docs.values(), key=lambda d: (not d["filename_match"], -d["hit_count"], d["filename"]))
    else:
        order = sorted(docs.values(), key=lambda d: (not d["filename_match"], d["rank"]))
    out = [{k: v for k, v in d.items() if k != "rank"} for d in order[:top_k]]
    return {"query": q, "terms": terms, "mode": mode, "tokens": tokens, "reranked": reranked,
            "semantic_ok": semantic_ok, "truncated": truncated,
            "total_docs": len(order), "total_hits": sum(d["hit_count"] for d in order),
            "took_ms": int((time.time() - t0) * 1000), "docs": out}


def _filename_hits(conn, terms: list[str], doc_id) -> dict[int, str]:
    """文件名逐字包含全部查询词的文档。"""
    sql = "SELECT id, filename FROM documents WHERE status='done' AND " + " AND ".join(
        "instr(lower(filename), ?) > 0" for _ in terms)
    args: list = list(terms)
    if doc_id:
        sql += " AND id = ?"
        args.append(doc_id)
    return {r[0]: r[1] for r in conn.execute(sql, args)}


def _fts(conn, match: str, doc_id, limit: int) -> list[int]:
    sql = "SELECT f.rowid FROM chunks_fts f"
    args: list = [match]
    if doc_id:
        sql += " JOIN chunks c ON c.id = f.rowid WHERE chunks_fts MATCH ? AND c.doc_id = ?"
        args.append(doc_id)
    else:
        sql += " WHERE chunks_fts MATCH ?"
    sql += " ORDER BY bm25(chunks_fts) LIMIT ?"
    args.append(limit)
    return [r[0] for r in conn.execute(sql, args)]


def _keyword(conn, terms: list[str], doc_id) -> list[int]:
    """精确模式：段落里逐字包含查询词，按出现次数排序。

    多个词时，每个词要么出现在这一段，要么出现在文件名里（如"扫描件 质保期"），且至少一个词出现在这一段。
    不能用 FTS 预筛：jieba 在不同上下文里切分不同（"质保期" 切成 质保/期，"质保期限" 切成 质保/期限），会漏掉逐字命中的片段。
    """
    if not terms:
        return []
    args: dict = {"limit": KEYWORD_LIMIT}
    each, any_in_text, counts = [], [], []
    for i, t in enumerate(terms):
        args[f"t{i}"] = t
        in_text = f"instr(lower(c.text), :t{i}) > 0"
        each.append(f"({in_text} OR instr(lower(d.filename), :t{i}) > 0)" if len(terms) > 1 else in_text)
        any_in_text.append(in_text)
        counts.append(f"(length(lower(c.text)) - length(replace(lower(c.text), :t{i}, ''))) / length(:t{i})")
    where = each + ([f"({' OR '.join(any_in_text)})"] if len(terms) > 1 else [])
    if doc_id:
        where.append("c.doc_id = :doc_id")
        args["doc_id"] = doc_id
    sql = (f"SELECT c.id FROM chunks c JOIN documents d ON d.id = c.doc_id WHERE {' AND '.join(where)}"
           f" ORDER BY {' + '.join(counts)} DESC, c.id LIMIT :limit")
    return [r[0] for r in conn.execute(sql, args)]


def _vector(conn, q: str, doc_id, k: int) -> list[int]:
    vec = sqlite_vec.serialize_float32(embedder.embed_query(q))
    sql = "SELECT rowid FROM chunks_vec WHERE embedding MATCH ? AND k = ?"
    args: list = [vec, k]
    if doc_id:
        sql += " AND doc_id = ?"
        args.append(doc_id)
    return [r[0] for r in conn.execute(sql + " ORDER BY distance", args)]


def _load(conn, ids: list[int]) -> dict[int, dict]:
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT c.*, d.filename FROM chunks c JOIN documents d ON d.id = c.doc_id WHERE c.id IN ({marks})", ids)
    return {r["id"]: dict(r) for r in rows}
