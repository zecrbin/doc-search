import json
import logging
import mimetypes
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import Body, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, embedder, highlight, ingest, search, textproc

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)
mimetypes.add_type("text/javascript", ".mjs")  # PDF.js 是 ES module，Windows 注册表里常缺这个类型


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    textproc.init()
    ingest.worker.start()
    yield


app = FastAPI(title="文档检索", lifespan=lifespan)


def _doc_or_404(conn, doc_id: int) -> dict:
    row = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not row:
        raise HTTPException(404, "文档不存在")
    return dict(row)


@app.post("/api/documents")
def upload(files: list[UploadFile] = File(...)):
    out = []
    for f in files:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            shutil.copyfileobj(f.file, tmp)
        try:
            out.append(ingest.register_file(Path(tmp.name), Path(f.filename).name))
        except ValueError as e:
            out.append({"filename": f.filename, "error": str(e)})
        except Exception as e:  # 单个文件失败不影响同批其他文件
            log.exception("上传失败：%s", f.filename)
            out.append({"filename": f.filename, "error": str(e) or type(e).__name__})
        finally:
            Path(tmp.name).unlink(missing_ok=True)
    return out


_STATUS_WHERE = {
    "all": "1",
    "busy": "status NOT IN ('done', 'failed')",
    "done": "status = 'done'",
    "failed": "status = 'failed'",
}


def _list_filter(status: str, q: str) -> tuple[str, list]:
    if status not in _STATUS_WHERE:
        raise HTTPException(400, f"status 只能是 {list(_STATUS_WHERE)}")
    where, args = [_STATUS_WHERE[status]], []
    if q.strip():
        where.append("instr(lower(filename), ?) > 0")
        args.append(q.strip().lower())
    return " AND ".join(where), args


@app.get("/api/documents")
def list_documents(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500),
                   status: str = "all", q: str = Query("", max_length=200)):
    """文件库分页列表（新上传的在前）。counts 是按文件名筛选后各状态的数量，overall 是整个库的数量。"""
    where, args = _list_filter(status, q)
    name_where, name_args = _list_filter("all", q)
    with db.session() as conn:
        total = conn.execute(f"SELECT count(*) FROM documents WHERE {where}", args).fetchone()[0]
        items = [dict(r) for r in conn.execute(
            "SELECT id, filename, ext, size, pages, ocr_pages, chunk_count, status, progress, message,"
            " created_at, updated_at, started_at, finished_at,"
            # 排队位置：后台按 id 从小到大处理
            " CASE WHEN status='queued' THEN (SELECT count(*) FROM documents q WHERE q.status='queued' AND q.id <= documents.id)"
            " END AS queue_pos"
            f" FROM documents WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*args, page_size, (page - 1) * page_size])]
        counts = {k: conn.execute(f"SELECT count(*) FROM documents WHERE {name_where} AND {w}", name_args).fetchone()[0]
                  for k, w in _STATUS_WHERE.items()}
        overall = conn.execute(
            f"SELECT count(*), sum({_STATUS_WHERE['busy']}) FROM documents").fetchone()
    return {"items": items, "total": total, "page": page, "page_size": page_size, "counts": counts,
            "overall": {"all": overall[0], "busy": overall[1] or 0}}


@app.get("/api/documents/ids")
def list_document_ids(status: str = "all", q: str = Query("", max_length=200)):
    """当前筛选条件下的全部文件 id（批量操作"选中全部"用）。"""
    where, args = _list_filter(status, q)
    with db.session() as conn:
        return [r[0] for r in conn.execute(f"SELECT id FROM documents WHERE {where} ORDER BY id DESC", args)]


def _delete(conn, doc_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not row:
        return None
    db.delete_chunks(conn, doc_id)
    conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
    return dict(row)


@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: int):
    with db.session() as conn:
        doc = _delete(conn, doc_id)
        if doc is None:
            raise HTTPException(404, "文档不存在")
    # 正在处理的文档：后台线程在下一次更新进度时发现已删除，会停下并再清理一次文件
    ingest.remove_files(doc)
    return {"ok": True}


@app.post("/api/documents/delete")
def delete_documents(ids: list[int] = Body(..., embed=True, max_length=100000)):
    """批量删除。已经不存在的 id 忽略。"""
    with db.session() as conn:
        docs = [d for d in (_delete(conn, i) for i in dict.fromkeys(ids)) if d]
    for doc in docs:
        ingest.remove_files(doc)
    return {"deleted": len(docs)}


_REQUEUE = ("UPDATE documents SET status='queued', progress=0, message=NULL, started_at=NULL, finished_at=NULL,"
            " updated_at=datetime('now', 'localtime')")


@app.post("/api/documents/reindex")
def reindex_many(ids: list[int] | None = Body(None, embed=True)):
    """批量重新解析；不传 ids 表示全部（改了词典、升级了解析逻辑后用）。处理中的文档不受影响。"""
    with db.session() as conn:
        if ids is None:
            n = conn.execute(_REQUEUE + " WHERE status IN ('done', 'failed')").rowcount
        else:
            n = sum(conn.execute(_REQUEUE + " WHERE id=? AND status IN ('done', 'failed')", (i,)).rowcount
                    for i in dict.fromkeys(ids))
    ingest.worker.wake.set()
    return {"queued": n}


@app.post("/api/documents/{doc_id}/reindex")
def reindex(doc_id: int):
    with db.session() as conn:
        _doc_or_404(conn, doc_id)
        # 只有已结束的文档能重新排队，否则后台线程写回进度/完成状态时会把这次请求覆盖掉
        cur = conn.execute(_REQUEUE + " WHERE id=? AND status IN ('done', 'failed')", (doc_id,))
        if not cur.rowcount:
            raise HTTPException(409, "文档正在处理中")
    ingest.worker.wake.set()
    return {"ok": True}


@app.get("/api/documents/{doc_id}/pdf")
def document_pdf(doc_id: int):
    with db.session() as conn:
        doc = _doc_or_404(conn, doc_id)
    pdf = ingest.pdf_file(doc)
    if not pdf.exists():
        raise HTTPException(404, "PDF 尚未生成")
    return FileResponse(pdf, media_type="application/pdf")


@app.get("/api/documents/{doc_id}/file")
def document_file(doc_id: int):
    with db.session() as conn:
        doc = _doc_or_404(conn, doc_id)
    orig = ingest.orig_file(doc)
    if not orig.exists():
        raise HTTPException(404, "原文件不存在")
    return FileResponse(orig, filename=doc["filename"])


@app.get("/api/search")
def do_search(q: str = Query(..., min_length=1, max_length=500), mode: str = "keyword",
              doc_id: int | None = None):
    if mode not in search.MODES:
        raise HTTPException(400, f"mode 只能是 {search.MODES}")
    try:
        return search.search(q, mode, doc_id)
    except RuntimeError as e:  # embedding 服务不可用
        raise HTTPException(503, str(e))


@app.get("/api/documents/{doc_id}/hits")
def document_hits(doc_id: int, q: str = Query(..., min_length=1, max_length=500),
                  offset: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=2000)):
    """一个文件里命中的段落，分页取（检索结果里每个文件只先带几段）。"""
    with db.session() as conn:
        _doc_or_404(conn, doc_id)
    return search.document_hits(q, doc_id, offset, limit)


@app.get("/api/documents/{doc_id}/highlights")
def document_highlights(doc_id: int, q: str = Query(..., min_length=1, max_length=500)):
    """文档里每处命中在原文 PDF 上的位置，按阅读顺序排列。

    hits：[{chunk_id, boxes: [[page, x0, y0, x1, y1], ...], exact}]，一处命中跨行时有多个框。
    扫描页没有文字层、也没有 OCR 行坐标时找不到关键字，退回整段的框（exact=false）。
    """
    terms = search.terms_of(q)
    with db.session() as conn:
        doc = _doc_or_404(conn, doc_id)
        ids = [cid for cid, _, _ in search.keyword_matches(conn, terms, doc_id)]
        rows = search.load_chunks(conn, ids)
    chunks = [(cid, json.loads(rows[cid]["regions"])) for cid in ids if cid in rows]
    per_chunk = [[] for _ in chunks]
    ocr_lines = {}
    pdf = ingest.pdf_file(doc)
    if pdf.exists():
        try:
            ocr_lines = ingest.load_ocr_lines(doc_id)
            per_chunk = highlight.keyword_matches(pdf, [r for _, r in chunks], terms, ocr_lines)
        except Exception:
            log.exception("关键字定位失败 doc=%s", doc_id)
    # 一个文字块/表格被切成多段时，每段记录的位置都是整个块，各段会找到同样的命中：按位置去重，
    # 再按阅读顺序分给真正包含它的段（每段最多分到它原文里出现的次数），左侧标出的段落才和右侧对得上
    found: dict[tuple, dict] = {}
    for (cid, regions), matches in zip(chunks, per_chunk):
        if not matches:
            if not any(t in rows[cid]["text"].lower() for t in terms):  # 只靠文件名命中的词不用框
                continue
            matches, exact = [regions], False
        else:
            exact = True
        for m in matches:
            found.setdefault(tuple(m[0]), {"boxes": m, "exact": exact, "candidates": []})["candidates"].append(cid)
    quota = {cid: sum(rows[cid]["text"].lower().count(t) for t in terms) for cid, _ in chunks}
    hits = []
    for h in sorted(found.values(), key=lambda h: (h["boxes"][0][0], h["boxes"][0][2], h["boxes"][0][1])):
        cid = next((c for c in h["candidates"] if quota[c] > 0), h["candidates"][0])
        quota[cid] -= 1
        hits.append({"chunk_id": cid, "boxes": h["boxes"], "exact": h["exact"]})
    # ocr：有关键字位置是按 OCR 行坐标估算的（扫描件）
    ocr = any(h["exact"] and int(h["boxes"][0][0]) in ocr_lines for h in hits)
    return {"doc_id": doc_id, "hits": hits, "ocr": ocr}


@app.get("/api/health")
def health():
    try:
        mineru = httpx.get(f"{config.MINERU_URL}/health", timeout=5).status_code == 200
    except Exception:
        mineru = False
    with db.session() as conn:
        stats = conn.execute(
            "SELECT (SELECT count(*) FROM documents WHERE status='done'), (SELECT count(*) FROM chunks)").fetchone()
    emb = embedder.health() if config.SEMANTIC else {}
    return {"mineru": mineru, "semantic": config.SEMANTIC, **emb, "documents": stats[0], "chunks": stats[1]}


app.mount("/static", StaticFiles(directory=config.WEB_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(config.WEB_DIR / "index.html")


if __name__ == "__main__":
    uvicorn.run(app, host=config.HOST, port=config.PORT)
