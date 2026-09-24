"""检索：BM25（jieba 分词 + FTS5）与向量（sqlite-vec）用 RRF 融合，原文精确命中加分，可选重排，折叠重复段落。"""
import json
import time

import sqlite_vec

from . import db, embedder, textproc

RRF_K = 60
CANDIDATES = 100
MODES = ("hybrid", "keyword", "semantic")


def search(q: str, mode: str = "hybrid", top_k: int = 20, doc_id: int | None = None) -> dict:
    t0 = time.time()
    q = q.strip()
    tokens = textproc.query_tokens(q)
    terms = [t for t in q.lower().split() if t]
    scores: dict[int, float] = {}
    reranked = False

    with db.session() as conn:
        if mode == "keyword":
            ids = _keyword(conn, tokens, terms, doc_id)
            for rank, cid in enumerate(ids):
                scores[cid] = 1 / (RRF_K + rank + 1)
        else:
            if mode == "hybrid" and tokens:
                for rank, cid in enumerate(_fts(conn, textproc.fts_query(tokens, "OR"), doc_id, CANDIDATES)):
                    scores[cid] = scores.get(cid, 0) + 1 / (RRF_K + rank + 1)
            for rank, cid in enumerate(_vector(conn, q, doc_id, CANDIDATES)):
                scores[cid] = scores.get(cid, 0) + 1 / (RRF_K + rank + 1)

        rows = _load(conn, list(scores))
        if mode == "hybrid" and terms:
            # 原文里逐字出现查询词的，排到前面（标书检索很依赖编号、名称的精确命中）
            for cid, r in rows.items():
                low = r["text"].lower()
                scores[cid] += 0.03 * sum(t in low for t in terms) / len(terms)

        ranked = sorted(scores, key=scores.get, reverse=True)
        if mode != "keyword":
            head = ranked[:30]
            rr = embedder.rerank(q, [(str(c), f"{rows[c]['heading'] or ''}\n{rows[c]['text']}") for c in head])
            if rr:
                reranked = True
                head.sort(key=lambda c: rr.get(str(c), -1e9), reverse=True)
                for c in head:
                    scores[c] = rr.get(str(c), scores[c])
                ranked = head + ranked[30:]

        # 相同段落（text_hash 相同）只保留最相关的一条，其余作为"也出现在"
        results, seen = [], set()
        for cid in ranked:
            r = rows[cid]
            if r["text_hash"] in seen:
                continue
            seen.add(r["text_hash"])
            results.append(cid)
            if len(results) >= top_k:
                break
        dups = _duplicates(conn, [rows[c]["text_hash"] for c in results])

        out = []
        for cid in results:
            r = rows[cid]
            regions = json.loads(r["regions"])
            out.append({
                "chunk_id": cid,
                "doc_id": r["doc_id"],
                "filename": r["filename"],
                "kind": r["kind"],
                "heading": r["heading"],
                "text": r["text"],
                "page": r["page_start"] + 1,
                "pages": sorted({int(g[0]) + 1 for g in regions}),
                "regions": regions,
                "score": round(scores[cid], 4),
                "duplicates": [d for d in dups.get(r["text_hash"], []) if d["chunk_id"] != cid],
            })

    return {"query": q, "mode": mode, "tokens": tokens, "reranked": reranked,
            "took_ms": int((time.time() - t0) * 1000), "results": out}


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


def _keyword(conn, tokens: list[str], terms: list[str], doc_id) -> list[int]:
    """精确模式：原文必须逐字包含每个查询词。先用 FTS 缩小范围，分词对不上时退回全表扫描。"""
    if not terms:
        return []
    ids = _fts(conn, textproc.fts_query(tokens, "AND"), doc_id, 1000) if tokens else []
    rows = _load(conn, ids)
    hits = [c for c in ids if all(t in rows[c]["text"].lower() for t in terms)]
    if hits:
        return hits[:CANDIDATES]
    sql = "SELECT id, text FROM chunks WHERE " + " AND ".join("instr(lower(text), ?) > 0" for _ in terms)
    args = list(terms)
    if doc_id:
        sql += " AND doc_id = ?"
        args.append(doc_id)
    found = [(r[0], sum(r[1].lower().count(t) for t in terms)) for r in conn.execute(sql + " LIMIT 1000", args)]
    return [cid for cid, _ in sorted(found, key=lambda x: -x[1])][:CANDIDATES]


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


def _duplicates(conn, hashes: list[str]) -> dict[str, list[dict]]:
    if not hashes:
        return {}
    marks = ",".join("?" * len(hashes))
    out: dict[str, list[dict]] = {}
    for r in conn.execute(
        f"SELECT c.id, c.text_hash, c.doc_id, c.page_start, c.regions, d.filename FROM chunks c"
        f" JOIN documents d ON d.id = c.doc_id WHERE c.text_hash IN ({marks}) ORDER BY d.filename", hashes):
        out.setdefault(r["text_hash"], []).append({
            "chunk_id": r["id"], "doc_id": r["doc_id"], "filename": r["filename"],
            "page": r["page_start"] + 1, "regions": json.loads(r["regions"]),
        })
    return out
