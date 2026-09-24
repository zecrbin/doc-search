"""入库：登记文件 → 后台线程依次 转换 → 解析 → 切块 → 向量化 → 写库。"""
import hashlib
import json
import logging
import shutil
import threading
import time
from dataclasses import asdict
from pathlib import Path

import sqlite_vec

from . import chunker, config, convert, db, embedder, parser, textproc

log = logging.getLogger(__name__)


def register_file(src: Path, filename: str) -> dict:
    """把文件复制进库并排队；同内容文件（sha256 相同）不重复入库。"""
    ext = Path(filename).suffix.lower()
    if ext not in config.ALLOWED_EXTS:
        raise ValueError(f"不支持的文件类型：{ext}")
    h = hashlib.sha256()
    with open(src, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    sha = h.hexdigest()
    with db.session() as conn:
        row = conn.execute("SELECT * FROM documents WHERE sha256=?", (sha,)).fetchone()
        if row:
            return {**dict(row), "duplicate": True}
        dst = config.FILES_DIR / f"{sha}{ext}"
        shutil.copyfile(src, dst)
        cur = conn.execute(
            "INSERT INTO documents(filename, ext, sha256, size, orig_path) VALUES (?,?,?,?,?)",
            (filename, ext, sha, dst.stat().st_size, str(dst)),
        )
        row = conn.execute("SELECT * FROM documents WHERE id=?", (cur.lastrowid,)).fetchone()
    worker.wake.set()
    return dict(row)


def process(doc: dict):
    doc_id = doc["id"]

    def progress(stage: str, lo: float, hi: float):
        def cb(frac: float, msg: str):
            db.update_doc(doc_id, status=stage, progress=round(lo + (hi - lo) * frac, 3), message=msg)
        return cb

    orig = Path(doc["orig_path"])
    if doc["ext"] == ".pdf":
        pdf = orig
    else:
        pdf = orig.with_suffix(".pdf")
        if not pdf.exists():
            progress("converting", 0, 0.05)(0, "转换为 PDF")
            convert.to_pdf(orig, pdf)
    db.update_doc(doc_id, pdf_path=str(pdf))

    t0 = time.time()
    blocks, pages, ocr_pages = parser.parse_pdf(pdf, progress("parsing", 0.05, 0.75))
    (config.PARSED_DIR / f"{doc_id}.json").write_text(
        json.dumps([asdict(b) for b in blocks], ensure_ascii=False), encoding="utf-8")
    chunks = chunker.build_chunks(blocks)
    log.info("%s：%d 页（OCR %d 页），%d 块，解析 %.1fs", doc["filename"], pages, ocr_pages, len(chunks), time.time() - t0)
    if not chunks:
        raise RuntimeError("未提取到任何文字")

    embed_cb = progress("embedding", 0.75, 0.98)
    texts = [f"{c.heading}\n{c.text}" if c.heading else c.text for c in chunks]
    vectors = []
    step = config.EMBED_BATCH * 4
    for i in range(0, len(texts), step):
        embed_cb(i / len(texts), f"向量化 {i}/{len(texts)}")
        vectors.extend(embedder.embed_documents(texts[i:i + step]))

    with db.session() as conn:
        db.delete_chunks(conn, doc_id)
        for seq, (c, vec) in enumerate(zip(chunks, vectors)):
            cur = conn.execute(
                "INSERT INTO chunks(doc_id, seq, kind, heading, text, text_hash, page_start, regions)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (doc_id, seq, c.kind, c.heading, c.text, textproc.text_hash(c.text), c.page_start,
                 json.dumps(c.regions)),
            )
            cid = cur.lastrowid
            conn.execute("INSERT INTO chunks_fts(rowid, tokens) VALUES (?, ?)",
                         (cid, textproc.index_tokens(f"{c.heading}\n{c.text}")))
            conn.execute("INSERT INTO chunks_vec(rowid, embedding, doc_id) VALUES (?, ?, ?)",
                         (cid, sqlite_vec.serialize_float32(vec), doc_id))
        conn.execute(
            "UPDATE documents SET status='done', progress=1, message=NULL, pages=?, ocr_pages=?, chunk_count=?,"
            " updated_at=datetime('now', 'localtime') WHERE id=?",
            (pages, ocr_pages, len(chunks), doc_id),
        )


class Worker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="ingest-worker")
        self.wake = threading.Event()

    def run(self):
        # 上次中断的任务重新排队
        with db.session() as conn:
            conn.execute("UPDATE documents SET status='queued', progress=0 WHERE status NOT IN ('done', 'failed')")
        while True:
            with db.session() as conn:
                row = conn.execute("SELECT * FROM documents WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if not row:
                self.wake.wait(5)
                self.wake.clear()
                continue
            doc = dict(row)
            try:
                process(doc)
            except Exception as e:
                log.exception("处理失败：%s", doc["filename"])
                db.update_doc(doc["id"], status="failed", message=str(e)[:500])


worker = Worker()
