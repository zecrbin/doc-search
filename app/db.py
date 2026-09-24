import sqlite3
from contextlib import contextmanager

import sqlite_vec

from . import config

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS documents(
  id INTEGER PRIMARY KEY AUTOINCREMENT,  -- 不复用已删除的 id，避免后台任务把结果写到新文档上
  filename TEXT NOT NULL,
  ext TEXT NOT NULL,
  sha256 TEXT NOT NULL UNIQUE,
  size INTEGER NOT NULL,
  orig_path TEXT NOT NULL,
  pdf_path TEXT,
  pages INTEGER,
  ocr_pages INTEGER DEFAULT 0,
  chunk_count INTEGER DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'queued',   -- importing / queued / converting / parsing / embedding / done / failed
  progress REAL DEFAULT 0,
  message TEXT,
  created_at TEXT DEFAULT (datetime('now', 'localtime')),
  updated_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS chunks(
  id INTEGER PRIMARY KEY,
  doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  kind TEXT NOT NULL,          -- text / table
  heading TEXT,                -- 标题路径，如 "第一章 项目概况 > 1.2 付款方式"
  text TEXT NOT NULL,
  text_hash TEXT NOT NULL,     -- 归一化文本的哈希，用于折叠标书间的重复段落
  page_start INTEGER NOT NULL, -- 0 起
  regions TEXT NOT NULL        -- JSON: [[page, x0, y0, x1, y1], ...]，PDF 坐标（pt，左上原点）
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_chunks_hash ON chunks(text_hash);
-- 入库前已用 jieba 分词并以空格拼接，unicode61 按空格切即可
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(tokens, content='', contentless_delete=1);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
  embedding float[{config.EMBED_DIM}] distance_metric=cosine,
  doc_id integer
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def session():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init():
    for d in (config.DATA_DIR, config.FILES_DIR, config.PARSED_DIR):
        d.mkdir(parents=True, exist_ok=True)
    with session() as conn:
        conn.executescript(SCHEMA)


def delete_chunks(conn: sqlite3.Connection, doc_id: int):
    ids = [(r[0],) for r in conn.execute("SELECT id FROM chunks WHERE doc_id=?", (doc_id,))]
    conn.executemany("DELETE FROM chunks_fts WHERE rowid=?", ids)
    conn.executemany("DELETE FROM chunks_vec WHERE rowid=?", ids)
    conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))


def update_doc(doc_id: int, **fields) -> bool:
    """返回文档是否还存在。"""
    cols = ", ".join(f"{k}=?" for k in fields)
    with session() as conn:
        return conn.execute(
            f"UPDATE documents SET {cols}, updated_at=datetime('now', 'localtime') WHERE id=?",
            (*fields.values(), doc_id),
        ).rowcount > 0
