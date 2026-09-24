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
