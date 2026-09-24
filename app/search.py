"""检索：默认精确模式——原文逐字包含查询词，文件名也参与匹配；结果按文件分组，完整统计不截断。

可选语义模式（DOCSEARCH_SEMANTIC=1）：BM25（jieba 分词 + FTS5）与向量（sqlite-vec）用 RRF 融合，可选重排。
"""
import json
import time

import sqlite_vec

from . import config, db, embedder, textproc

RRF_K = 60
CANDIDATES = 100
PREVIEW = 3  # 检索结果里每个文件先带几段，其余展开时用 document_hits 分页取（前端收起时也只留这么多，见 style.css）
MODES = ("keyword", "hybrid", "semantic") if config.SEMANTIC else ("keyword",)


def terms_of(q: str) -> list[str]:
    """查询词：空格分隔，不区分大小写。"""
    return list(dict.fromkeys(t for t in q.lower().split() if t))


def search(q: str, mode: str = "keyword", doc_id: int | None = None) -> dict:
    """按文件分组的结果。每个文件：命中段数 chunk_count、出现次数 hit_count，以及前 PREVIEW 段（按页码排序）。"""
    t0 = time.time()
    q = q.strip()
    terms = terms_of(q)
    extra = {}
    with db.session() as conn:
        if mode == "keyword":  # 已按页码排好
            matches = keyword_matches(conn, terms, doc_id)
        else:  # 按相关度排序，每个文件的前几段就是最相关的
            ranked, extra = _semantic(conn, q, terms, mode, doc_id)
            matches = [(cid, doc, sum(r.lower().count(t) for t in terms)) for cid, doc, r in ranked]
        name_hits = _filename_hits(conn, terms, doc_id) if terms else {}

        docs: dict[int, dict] = {}
        for rank, (cid, did, count) in enumerate(matches):
            d = docs.setdefault(did, {"doc_id": did, "rank": rank, "hit_count": 0, "ids": [], "counts": {}})
            d["hit_count"] += count
            d["ids"].append(cid)
            d["counts"][cid] = count
        for did in name_hits:
            docs.setdefault(did, {"doc_id": did, "rank": len(matches), "hit_count": 0, "ids": [], "counts": {}})

        preview = [cid for d in docs.values() for cid in d["ids"][:PREVIEW]]
        rows = load_chunks(conn, preview)
        names = _filenames(conn, list(docs))

    out = []
    for d in docs.values():
        ids = d["ids"]
        out.append({
            "doc_id": d["doc_id"],
            "filename": names.get(d["doc_id"], ""),
            "filename_match": d["doc_id"] in name_hits,
            "hit_count": d["hit_count"],
            "chunk_count": len(ids),
            "chunks": [chunk_out(rows[c], d["counts"][c]) for c in ids[:PREVIEW] if c in rows],
            "rank": d["rank"],
        })
    if mode == "keyword":  # 文件名命中的排前面，其次按正文命中次数
        out.sort(key=lambda d: (not d["filename_match"], -d["hit_count"], d["filename"]))
    else:
        out.sort(key=lambda d: (not d["filename_match"], d["rank"]))
    for d in out:
        del d["rank"]
    return {"query": q, "terms": terms, "mode": mode, **extra,
            "total_docs": len(out), "total_hits": sum(d["hit_count"] for d in out),
            "total_chunks": sum(d["chunk_count"] for d in out),
            "took_ms": int((time.time() - t0) * 1000), "docs": out}


def document_hits(q: str, doc_id: int, offset: int = 0, limit: int = 500) -> dict:
    """一个文件里命中的段落（按页码排序），分页取，用于展开检索结果。"""
    terms = terms_of(q)
    with db.session() as conn:
        matches = keyword_matches(conn, terms, doc_id)
        page = matches[offset:offset + limit]
        rows = load_chunks(conn, [cid for cid, _, _ in page])
    return {"total": len(matches), "offset": offset,
            "chunks": [chunk_out(rows[cid], count) for cid, _, count in page if cid in rows]}


def chunk_out(r: dict, count: int) -> dict:
    regions = json.loads(r["regions"])
    return {
        "chunk_id": r["id"],
        "kind": r["kind"],
        "heading": r["heading"],
        "text": r["text"],
        "page": r["page_start"] + 1,
        "pages": sorted({int(g[0]) + 1 for g in regions}),
        "count": count,
    }


def _filenames(conn, doc_ids: list[int]) -> dict[int, str]:
    out = {}
    for i in range(0, len(doc_ids), 900):
        batch = doc_ids[i:i + 900]
        marks = ",".join("?" * len(batch))
        out.update({r[0]: r[1] for r in conn.execute(f"SELECT id, filename FROM documents WHERE id IN ({marks})", batch)})
    return out


def _filename_hits(conn, terms: list[str], doc_id) -> dict[int, str]:
    """文件名逐字包含全部查询词的文档。"""
    sql = "SELECT id, filename FROM documents WHERE status='done' AND " + " AND ".join(
        "instr(lower(filename), ?) > 0" for _ in terms)
    args: list = list(terms)
    if doc_id:
        sql += " AND id = ?"
        args.append(doc_id)
    return {r[0]: r[1] for r in conn.execute(sql, args)}


def _semantic(conn, q: str, terms: list[str], mode: str, doc_id) -> tuple[list[tuple[int, int, str]], dict]:
    """综合/语义模式（需开启 DOCSEARCH_SEMANTIC）：返回按相关度排序的 [(chunk_id, doc_id, text)]。"""
    tokens = textproc.query_tokens(q)
    scores: dict[int, float] = {}
    reranked, semantic_ok = False, True
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

    rows = load_chunks(conn, list(scores))
    # 各条 SELECT 不在同一快照里，期间被删除/重新解析的片段在这里会查不到
    scores = {cid: s for cid, s in scores.items() if cid in rows}
    if mode == "hybrid" and terms:
        # 原文里逐字出现查询词的，排到前面（标书检索很依赖编号、名称的精确命中）
        for cid, r in rows.items():
            low = r["text"].lower()
            scores[cid] += 0.03 * sum(t in low for t in terms) / len(terms)
    ranked = sorted(scores, key=scores.get, reverse=True)
    if semantic_ok:  # 向量服务挂了，重排服务（同一台机器）多半也连不上
        head = ranked[:30]
        rr = embedder.rerank(q, [(str(c), f"{rows[c]['heading'] or ''}\n{rows[c]['text']}") for c in head])
        if rr:
            reranked = True
            head.sort(key=lambda c: rr.get(str(c), -1e9), reverse=True)
            ranked = head + ranked[30:]
    return [(c, rows[c]["doc_id"], rows[c]["text"]) for c in ranked], {"reranked": reranked, "semantic_ok": semantic_ok}


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


def keyword_matches(conn, terms: list[str], doc_id=None) -> list[tuple[int, int, int]]:
    """精确模式的全部命中段落（不设上限）：[(chunk_id, doc_id, 本段出现次数)]，按文件、页码、段落顺序排列。

    多个词时，每个词要么出现在这一段，要么出现在文件名里（如"扫描件 质保期"），且至少一个词出现在这一段。
    不能用 FTS 预筛：jieba 在不同上下文里切分不同（"质保期" 切成 质保/期，"质保期限" 切成 质保/期限），会漏掉逐字命中的片段。
    """
    if not terms:
        return []
    args: dict = {}
    each, any_in_text, counts = [], [], []
    for i, t in enumerate(terms):
        args[f"t{i}"] = t
        in_text = f"instr(lower(c.text), :t{i}) > 0"
        each.append(f"({in_text} OR instr(lower(d.filename), :t{i}) > 0)" if len(terms) > 1 else in_text)
        any_in_text.append(in_text)
        counts.append(f"(length(lower(c.text)) - length(replace(lower(c.text), :t{i}, ''))) / length(:t{i})")
    where = ["d.status = 'done'", *each] + ([f"({' OR '.join(any_in_text)})"] if len(terms) > 1 else [])
    if doc_id:
        where.append("c.doc_id = :doc_id")
        args["doc_id"] = doc_id
    sql = (f"SELECT c.id, c.doc_id, {' + '.join(counts)} FROM chunks c JOIN documents d ON d.id = c.doc_id"
           f" WHERE {' AND '.join(where)} ORDER BY c.doc_id, c.page_start, c.seq")
    return [(r[0], r[1], r[2]) for r in conn.execute(sql, args)]


def _vector(conn, q: str, doc_id, k: int) -> list[int]:
    vec = sqlite_vec.serialize_float32(embedder.embed_query(q))
    sql = "SELECT rowid FROM chunks_vec WHERE embedding MATCH ? AND k = ?"
    args: list = [vec, k]
    if doc_id:
        sql += " AND doc_id = ?"
        args.append(doc_id)
    return [r[0] for r in conn.execute(sql + " ORDER BY distance", args)]


def load_chunks(conn, ids: list[int]) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for i in range(0, len(ids), 900):  # SQLite 单条语句的参数个数有上限
        batch = ids[i:i + 900]
        marks = ",".join("?" * len(batch))
        for r in conn.execute(
                f"SELECT c.*, d.filename FROM chunks c JOIN documents d ON d.id = c.doc_id WHERE c.id IN ({marks})",
                batch):
            out[r["id"]] = dict(r)
    return out
