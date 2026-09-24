import json

import pytest
from fastapi.testclient import TestClient

from app import config, db, ingest, main
from conftest import add_doc


def _status(doc_id):
    with db.session() as conn:
        row = conn.execute("SELECT status FROM documents WHERE id=?", (doc_id,)).fetchone()
    return row and row[0]


def _src(tmp_path, name="x.pdf", data=b"%PDF-1.4 test"):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_register_duplicate(tmp_path):
    first = ingest.register_file(_src(tmp_path), "x.pdf")
    assert first["status"] == "queued"
    assert ingest.register_file(_src(tmp_path, "y.pdf"), "y.pdf")["duplicate"]


def test_concurrent_register_same_file(tmp_path, monkeypatch):
    """拷贝期间另一个请求上传同一文件：只有一方入库，另一方得到 duplicate 而不是 500。"""
    src = _src(tmp_path)
    real_copy = ingest.shutil.copyfile
    nested = []

    def copy(a, b):
        if not nested:
            nested.append(ingest.register_file(src, "again.pdf"))
        return real_copy(a, b)

    monkeypatch.setattr(ingest.shutil, "copyfile", copy)
    doc = ingest.register_file(src, "x.pdf")
    assert nested[0]["duplicate"] and nested[0]["id"] == doc["id"]
    assert doc["status"] == "queued"


def test_failed_copy_releases_claim(tmp_path, monkeypatch):
    def boom(*_):
        raise OSError("disk full")
    monkeypatch.setattr(ingest.shutil, "copyfile", boom)
    with pytest.raises(OSError):
        ingest.register_file(_src(tmp_path), "x.pdf")
    with db.session() as conn:
        assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 0


def test_worker_stops_and_cleans_up_deleted_doc(tmp_path, monkeypatch):
    doc = ingest.register_file(_src(tmp_path), "x.pdf")
    parsed = config.PARSED_DIR / f"{doc['id']}.json"

    def process(d):
        parsed.write_text("[]")
        # 用户在处理过程中点了删除（Windows 上文件被占用，删除接口没删掉文件）
        with db.session() as conn:
            conn.execute("DELETE FROM documents WHERE id=?", (d["id"],))
        ingest._update(d["id"], status="parsing")

    monkeypatch.setattr(ingest, "process", process)
    ingest.Worker()._step()
    assert not parsed.exists()
    assert not ingest.Path(doc["orig_path"]).exists()


def test_worker_marks_failure(tmp_path, monkeypatch):
    doc = ingest.register_file(_src(tmp_path), "x.pdf")
    monkeypatch.setattr(ingest, "process", lambda d: (_ for _ in ()).throw(RuntimeError("坏文件")))
    ingest.Worker()._step()
    assert _status(doc["id"]) == "failed"
    assert ingest.Path(doc["orig_path"]).exists()


def test_remove_files_keeps_files_of_reuploaded_doc(tmp_path):
    doc = ingest.register_file(_src(tmp_path), "x.pdf")
    old = {**doc, "id": 999}  # 被删的旧记录，和新文档 sha256 相同
    ingest.remove_files(old)
    assert ingest.Path(doc["orig_path"]).exists()


def test_reindex_rejects_busy_doc():
    client = TestClient(main.app)
    busy, done = add_doc("a.pdf", "parsing"), add_doc("b.pdf", "done")
    assert client.post(f"/api/documents/{busy}/reindex").status_code == 409
    assert _status(busy) == "parsing"
    assert client.post(f"/api/documents/{done}/reindex").status_code == 200
    assert _status(done) == "queued"


def test_delete_removes_files(tmp_path):
    doc = ingest.register_file(_src(tmp_path), "x.pdf")
    parsed = config.PARSED_DIR / f"{doc['id']}.json"
    parsed.write_text(json.dumps([]))
    client = TestClient(main.app)
    assert client.delete(f"/api/documents/{doc['id']}").status_code == 200
    assert not parsed.exists() and not ingest.Path(doc["orig_path"]).exists()
    assert _status(doc["id"]) is None


def _real_pdf(tmp_path):
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((60, 100), "设备质保期限为两年，付款方式为银行转账。", fontname="china-s", fontsize=12)
    path = tmp_path / "real.pdf"
    doc.save(path)
    return path


def test_ingest_without_embedding_service(tmp_path, monkeypatch):
    """默认不开语义检索：入库不调用 Embedding，记录开始/完成时间，精确检索和关键字定位可用。"""
    from app import embedder, search

    def must_not_call(*_):
        raise AssertionError("不应调用 Embedding")
    monkeypatch.setattr(embedder, "embed_documents", must_not_call)
    doc = ingest.register_file(_real_pdf(tmp_path), "real.pdf")
    ingest.Worker()._step()
    with db.session() as conn:
        row = dict(conn.execute("SELECT * FROM documents WHERE id=?", (doc["id"],)).fetchone())
    assert row["status"] == "done", row["message"]
    assert row["started_at"] and row["finished_at"] and row["started_at"] <= row["finished_at"]

    res = search.search("质保期", "keyword")["results"]
    assert len(res) == 1
    client = TestClient(main.app)
    hl = client.get(f"/api/chunks/{res[0]['chunk_id']}/highlights", params={"q": "质保期"}).json()
    assert hl["exact"] and len(hl["boxes"]) == 1


def test_failure_records_finish_time(tmp_path, monkeypatch):
    doc = ingest.register_file(_src(tmp_path), "x.pdf")  # 不是合法 PDF
    ingest.Worker()._step()
    with db.session() as conn:
        row = conn.execute("SELECT status, message, finished_at FROM documents WHERE id=?", (doc["id"],)).fetchone()
    assert row["status"] == "failed" and row["message"] and row["finished_at"]


def test_highlights_fall_back_to_paragraph():
    from conftest import add_chunk
    cid = add_chunk(add_doc(), "质保期三年")  # 没有 PDF 文件（相当于扫描页找不到文字层）
    hl = TestClient(main.app).get(f"/api/chunks/{cid}/highlights", params={"q": "质保期"}).json()
    assert hl == {"doc_id": hl["doc_id"], "boxes": [[0, 0, 0, 10, 10]], "exact": False, "ocr": False}


def test_reindex_all_and_old_db_migration():
    busy, done = add_doc("a.pdf", "parsing"), add_doc("b.pdf", "failed")
    r = TestClient(main.app).post("/api/documents/reindex").json()
    assert r == {"queued": 1} and _status(busy) == "parsing" and _status(done) == "queued"
    # 旧库没有时间列：init 时补上
    with db.session() as conn:
        conn.execute("ALTER TABLE documents DROP COLUMN finished_at")
    db.init()
    with db.session() as conn:
        assert "finished_at" in {r[1] for r in conn.execute("PRAGMA table_info(documents)")}
