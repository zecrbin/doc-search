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
    dst = config.FILES_DIR / f"{sha}{ext}"
    # 先占位再拷文件：并发上传同一文件（多人上传、CLI 与网页同时导入）时，sha256 唯一约束保证只有一方继续
    with db.session() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO documents(filename, ext, sha256, size, orig_path, status) VALUES (?,?,?,?,?,'importing')",
            (filename, ext, sha, src.stat().st_size, str(dst)),
        )
        if not cur.rowcount:
            row = conn.execute("SELECT * FROM documents WHERE sha256=?", (sha,)).fetchone()
            if row is None:  # 另一方拷贝失败刚删掉了占位
                raise RuntimeError("同一文件正在导入，请稍后重试")
            return {**dict(row), "duplicate": True}
        doc_id = cur.lastrowid
    try:
        shutil.copyfile(src, dst)
    except BaseException:
        with db.session() as conn:
            conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
        raise
    with db.session() as conn:
        conn.execute("UPDATE documents SET status='queued', message=NULL WHERE id=?", (doc_id,))
        row = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if row is None:  # 拷贝期间被删除
        remove_files({"id": doc_id, "orig_path": str(dst), "pdf_path": None, "sha256": sha})
        raise RuntimeError("导入过程中文档被删除")
    worker.wake.set()
    return dict(row)


def remove_files(doc: dict):
    """删除文档的原文件、转换出的 PDF 和解析缓存。文件被占用（Windows 上正在解析）时只记日志，由后台线程处理完后再清一次。"""
    orig = Path(doc["orig_path"])
    paths = [config.PARSED_DIR / f"{doc['id']}.json"]
    with db.session() as conn:
        # 删除后又重新上传了同一文件：文件名按 sha256 命名，是新文档在用
        reused = conn.execute("SELECT 1 FROM documents WHERE sha256=?", (doc["sha256"],)).fetchone()
    if not reused:
        paths += {orig, orig.with_suffix(".pdf"), *([Path(doc["pdf_path"])] if doc.get("pdf_path") else [])}
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except OSError as e:
            log.warning("删除文件失败 %s：%s", p, e)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class _Deleted(Exception):
    """处理过程中文档被删除。"""


def _update(doc_id: int, **fields):
    if not db.update_doc(doc_id, **fields):
        raise _Deleted


def process(doc: dict):
    doc_id = doc["id"]

    def progress(stage: str, lo: float, hi: float):
        def cb(frac: float, msg: str):
            _update(doc_id, status=stage, progress=round(lo + (hi - lo) * frac, 3), message=msg)
        return cb

    orig = Path(doc["orig_path"])
    if doc["ext"] == ".pdf":
        pdf = orig
    else:
        pdf = orig.with_suffix(".pdf")
        if not pdf.exists():
            progress("converting", 0, 0.05)(0, "转换为 PDF")
            convert.to_pdf(orig, pdf)
    _update(doc_id, pdf_path=str(pdf))

    t0 = time.time()
    blocks, pages, ocr_pages = parser.parse_pdf(pdf, progress("parsing", 0.05, 0.75 if config.SEMANTIC else 0.95))
    (config.PARSED_DIR / f"{doc_id}.json").write_text(
        json.dumps([asdict(b) for b in blocks], ensure_ascii=False), encoding="utf-8")
    chunks = chunker.build_chunks(blocks)
    log.info("%s：%d 页（OCR %d 页），%d 块，解析 %.1fs", doc["filename"], pages, ocr_pages, len(chunks), time.time() - t0)
    if not chunks:
        raise RuntimeError("未提取到任何文字")

    vectors: list = [None] * len(chunks)
    if config.SEMANTIC:
        embed_cb = progress("embedding", 0.75, 0.98)
        texts = [f"{c.heading}\n{c.text}" if c.heading else c.text for c in chunks]
        vectors = []
        step = config.EMBED_BATCH * 4
        for i in range(0, len(texts), step):
            embed_cb(i / len(texts), f"向量化 {i}/{len(texts)}")
            vectors.extend(embedder.embed_documents(texts[i:i + step]))
        if len(vectors) != len(chunks):
            raise RuntimeError(f"Embedding 服务返回 {len(vectors)} 个向量，应为 {len(chunks)} 个")

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
            if vec is not None:
                conn.execute("INSERT INTO chunks_vec(rowid, embedding, doc_id) VALUES (?, ?, ?)",
                             (cid, sqlite_vec.serialize_float32(vec), doc_id))
        conn.execute(
            "UPDATE documents SET status='done', progress=1, message=NULL, pages=?, ocr_pages=?, chunk_count=?,"
            " updated_at=datetime('now', 'localtime'), finished_at=datetime('now', 'localtime') WHERE id=?",
            (pages, ocr_pages, len(chunks), doc_id),
        )


class Worker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="ingest-worker")
        self.wake = threading.Event()

    def run(self):
        with db.session() as conn:
            # 拷贝到一半中断的文件不完整，不能直接解析
            conn.execute("UPDATE documents SET status='failed', message='导入中断，请删除后重新上传' WHERE status='importing'")
            # 上次中断的任务重新排队
            conn.execute("UPDATE documents SET status='queued', progress=0 WHERE status NOT IN ('done', 'failed')")
        while True:
            try:
                self._step()
            except Exception:  # 数据库被锁等意外错误：线程不能退出，否则队列会悄悄停下
                log.exception("入库线程出错，5 秒后重试")
                time.sleep(5)

    def _step(self):
        with db.session() as conn:
            row = conn.execute("SELECT * FROM documents WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
        if not row:
            self.wake.wait(5)
            self.wake.clear()
            return
        doc = dict(row)
        try:
            _update(doc["id"], started_at=_now(), finished_at=None)
            process(doc)
        except Exception as e:
            if db.update_doc(doc["id"], status="failed", message=str(e)[:500], finished_at=_now()):
                log.exception("处理失败：%s", doc["filename"])
        with db.session() as conn:
            deleted = conn.execute("SELECT 1 FROM documents WHERE id=?", (doc["id"],)).fetchone() is None
        if deleted:  # 处理中被删除：删除时被占用的文件、之后才生成的 PDF / 解析缓存都在这里清掉
            remove_files(doc)


worker = Worker()
