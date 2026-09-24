import logging
import mimetypes
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, embedder, ingest, search, textproc

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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
        finally:
            Path(tmp.name).unlink(missing_ok=True)
    return out


@app.get("/api/documents")
def list_documents():
    with db.session() as conn:
        docs = [dict(r) for r in conn.execute(
            "SELECT id, filename, ext, size, pages, ocr_pages, chunk_count, status, progress, message,"
            " created_at, updated_at FROM documents ORDER BY id DESC")]
    return docs


@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: int):
    with db.session() as conn:
        doc = _doc_or_404(conn, doc_id)
        db.delete_chunks(conn, doc_id)
        conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
    for p in {doc["orig_path"], doc["pdf_path"]} - {None}:
        Path(p).unlink(missing_ok=True)
    (config.PARSED_DIR / f"{doc_id}.json").unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/documents/{doc_id}/reindex")
def reindex(doc_id: int):
    with db.session() as conn:
        _doc_or_404(conn, doc_id)
    db.update_doc(doc_id, status="queued", progress=0, message=None)
    ingest.worker.wake.set()
    return {"ok": True}


@app.get("/api/documents/{doc_id}/pdf")
def document_pdf(doc_id: int):
    with db.session() as conn:
        doc = _doc_or_404(conn, doc_id)
    if not doc["pdf_path"] or not Path(doc["pdf_path"]).exists():
        raise HTTPException(404, "PDF 尚未生成")
    return FileResponse(doc["pdf_path"], media_type="application/pdf")


@app.get("/api/documents/{doc_id}/file")
def document_file(doc_id: int):
    with db.session() as conn:
        doc = _doc_or_404(conn, doc_id)
    return FileResponse(doc["orig_path"], filename=doc["filename"])


@app.get("/api/search")
def do_search(q: str = Query(..., min_length=1, max_length=500), mode: str = "hybrid",
              top_k: int = Query(20, ge=1, le=100), doc_id: int | None = None):
    if mode not in search.MODES:
        raise HTTPException(400, f"mode 只能是 {search.MODES}")
    try:
        return search.search(q, mode, top_k, doc_id)
    except RuntimeError as e:  # embedding 服务不可用
        raise HTTPException(503, str(e))


@app.get("/api/health")
def health():
    try:
        mineru = httpx.get(f"{config.MINERU_URL}/health", timeout=5).status_code == 200
    except Exception:
        mineru = False
    with db.session() as conn:
        stats = conn.execute(
            "SELECT (SELECT count(*) FROM documents WHERE status='done'), (SELECT count(*) FROM chunks)").fetchone()
    return {"mineru": mineru, **embedder.health(), "documents": stats[0], "chunks": stats[1]}


app.mount("/static", StaticFiles(directory=config.WEB_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(config.WEB_DIR / "index.html")


if __name__ == "__main__":
    uvicorn.run(app, host=config.HOST, port=config.PORT)
