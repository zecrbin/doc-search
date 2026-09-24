import pytest

from app import config, db, textproc


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    """每个测试用独立的数据目录和数据库。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "FILES_DIR", tmp_path / "files")
    monkeypatch.setattr(config, "PARSED_DIR", tmp_path / "parsed")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "docsearch.db")
    monkeypatch.setattr(config, "USER_DICT", tmp_path / "userdict.txt")
    monkeypatch.setattr(config, "RERANK_ENABLED", False)
    monkeypatch.setattr(config, "LLM_URL", "")  # 默认不连大模型；需要的测试用 FakeLLM 打开
    db.init()
    textproc.init()
    return tmp_path


def add_doc(filename="a.pdf", status="done", sha=None) -> int:
    with db.session() as conn:
        cur = conn.execute(
            "INSERT INTO documents(filename, ext, sha256, size, orig_path, status) VALUES (?,?,?,?,?,?)",
            (filename, ".pdf", sha or filename, 0, str(config.FILES_DIR / filename), status),
        )
        return cur.lastrowid


def add_chunk(doc_id: int, text: str, heading: str = "") -> int:
    with db.session() as conn:
        cur = conn.execute(
            "INSERT INTO chunks(doc_id, seq, kind, heading, text, text_hash, page_start, regions)"
            " VALUES (?, 0, 'text', ?, ?, ?, 0, '[[0, 0, 0, 10, 10]]')",
            (doc_id, heading, text, textproc.text_hash(text)),
        )
        conn.execute("INSERT INTO chunks_fts(rowid, tokens) VALUES (?, ?)",
                     (cur.lastrowid, textproc.index_tokens(f"{heading}\n{text}")))
        return cur.lastrowid


def chunk_ids(result: dict) -> list[int]:
    """检索结果里的全部片段 id（按文件分组后的顺序）。"""
    return [c["chunk_id"] for d in result["docs"] for c in d["chunks"]]
