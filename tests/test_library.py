"""文件库：分页、筛选、批量删除、批量重新解析。"""
from fastapi.testclient import TestClient

from app import config, db, ingest, main
from conftest import add_chunk, add_doc

client = TestClient(main.app)


def _status(doc_id):
    with db.session() as conn:
        row = conn.execute("SELECT status FROM documents WHERE id=?", (doc_id,)).fetchone()
    return row and row[0]


def test_paging_filters_and_counts():
    ids = [add_doc(f"标书{i:02d}.pdf", "done") for i in range(23)]
    failed = add_doc("坏文件.pdf", "failed")
    q1, q2 = add_doc("排队A.pdf", "queued"), add_doc("排队B.pdf", "queued")
    parsing = add_doc("解析中.pdf", "parsing")

    r = client.get("/api/documents", params={"page": 1, "page_size": 10}).json()
    assert r["total"] == 27 and len(r["items"]) == 10
    assert [d["id"] for d in r["items"]][:4] == [parsing, q2, q1, failed]  # 新上传的在前
    assert r["counts"] == {"all": 27, "busy": 3, "done": 23, "failed": 1}
    assert r["overall"] == {"all": 27, "busy": 3}
    assert {d["id"]: d["queue_pos"] for d in r["items"] if d["status"] == "queued"} == {q1: 1, q2: 2}

    last = client.get("/api/documents", params={"page": 3, "page_size": 10}).json()
    assert [d["id"] for d in last["items"]] == ids[:7][::-1]

    busy = client.get("/api/documents", params={"status": "busy"}).json()
    assert {d["id"] for d in busy["items"]} == {q1, q2, parsing}

    # 按文件名筛选：状态数量也跟着筛选；overall 始终是整个库
    r = client.get("/api/documents", params={"q": "标书0", "status": "done"}).json()
    assert r["total"] == 10 and r["counts"] == {"all": 10, "busy": 0, "done": 10, "failed": 0}
    assert r["overall"]["all"] == 27

    assert client.get("/api/documents", params={"status": "bad"}).status_code == 400
    assert client.get("/api/documents/ids", params={"status": "failed"}).json() == [failed]
    assert len(client.get("/api/documents/ids", params={"q": "标书"}).json()) == 23


def test_batch_delete(tmp_path):
    docs = []
    for i in range(3):
        src = tmp_path / f"{i}.pdf"
        src.write_bytes(f"%PDF-1.4 {i}".encode())
        docs.append(ingest.register_file(src, f"{i}.pdf"))
    for d in docs:
        add_chunk(d["id"], "付款方式")
        (config.PARSED_DIR / f"{d['id']}.json").write_text("[]")
    keep = docs[2]
    r = client.post("/api/documents/delete", json={"ids": [docs[0]["id"], docs[1]["id"], docs[1]["id"], 99999]}).json()
    assert r == {"deleted": 2}
    for d in docs[:2]:
        assert _status(d["id"]) is None
        assert not ingest.orig_file(d).exists()
        assert not (config.PARSED_DIR / f"{d['id']}.json").exists()
    assert _status(keep["id"]) == "queued" and ingest.orig_file(keep).exists()
    with db.session() as conn:
        assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH '付款'").fetchone()[0] == 1


def test_batch_reindex_skips_busy():
    done, failed, parsing = add_doc("a.pdf", "done"), add_doc("b.pdf", "failed"), add_doc("c.pdf", "parsing")
    r = client.post("/api/documents/reindex", json={"ids": [done, failed, parsing]}).json()
    assert r == {"queued": 2}
    assert (_status(done), _status(failed), _status(parsing)) == ("queued", "queued", "parsing")
