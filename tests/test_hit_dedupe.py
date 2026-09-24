"""一个文字块/表格被切成多段时，各段记录的位置都是整个块；右侧的命中不能因此重复。"""
import pymupdf
from fastapi.testclient import TestClient

from app import config, db, ingest, main


def test_split_block_hits_not_duplicated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CHUNK_MAX", 60)
    monkeypatch.setattr(config, "CHUNK_TARGET", 30)
    text = "".join(f"第{i}条：系统应支持态势感知与分析。" for i in range(1, 9))  # 一个文字块，8 处"态势"
    d = pymupdf.open()
    d.new_page().insert_textbox(pymupdf.Rect(60, 60, 540, 400), text, fontname="china-s", fontsize=12)
    path = tmp_path / "t.pdf"
    d.save(path)
    doc = ingest.register_file(path, "t.pdf")
    ingest.Worker()._step()
    with db.session() as conn:
        chunks = {r["id"]: r["text"] for r in conn.execute("SELECT id, text FROM chunks WHERE doc_id=?", (doc["id"],))}
    assert len(chunks) > 1  # 确实被切成了多段

    hits = TestClient(main.app).get(f"/api/documents/{doc['id']}/highlights", params={"q": "态势"}).json()["hits"]
    positions = [tuple(h["boxes"][0]) for h in hits]
    assert len(positions) == len(set(positions)) == 8  # 每处只出现一次
    # 每处归到真正包含它的那一段，且按顺序：左侧同步标出的段落和右侧跳到的位置一致
    per_chunk = {}
    for h in hits:
        per_chunk[h["chunk_id"]] = per_chunk.get(h["chunk_id"], 0) + 1
    assert per_chunk == {cid: t.count("态势") for cid, t in chunks.items() if "态势" in t}
    order = [h["chunk_id"] for h in hits]
    assert order == sorted(order)
