"""文档概述：大模型用 httpx.MockTransport 模拟 OpenAI 兼容接口。"""
import json

import httpx
import pymupdf
import pytest
from fastapi.testclient import TestClient

from app import config, db, ingest, llm, main, summary


class FakeLLM:
    def __init__(self):
        self.calls = []  # 每次 chat 的 user 内容
        self.payloads = []
        self.fail = 0  # 前几次返回 500
        self.on_call = None
        self.reply = lambda user: "<think>先想一想</think>\n```markdown\n## 概述\n这是概述。\n```"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen-test"}]})
        body = json.loads(request.content)
        self.payloads.append(body)
        if self.fail:
            self.fail -= 1
            return httpx.Response(500, text="GPU 忙")
        user = body["messages"][-1]["content"]
        self.calls.append(user)
        if self.on_call:
            self.on_call()
        return httpx.Response(200, json={"choices": [{"message": {"content": self.reply(user)}, "finish_reason": "stop"}]})


@pytest.fixture
def fake(monkeypatch):
    f = FakeLLM()
    monkeypatch.setattr(config, "LLM_URL", "http://llm:8000")
    monkeypatch.setattr(llm, "_client", httpx.Client(base_url=llm.base_url(), transport=httpx.MockTransport(f)))
    monkeypatch.setattr(llm, "_model", None)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    return f


def _ingest(tmp_path, paragraphs: list[str], name="标书.pdf") -> dict:
    d = pymupdf.open()
    for text in paragraphs:
        d.new_page().insert_textbox(pymupdf.Rect(60, 120, 540, 780), text, fontname="china-s", fontsize=11)
    path = tmp_path / name
    d.save(path)
    doc = ingest.register_file(path, name)
    ingest.Worker()._step()
    return doc


def _row(doc_id):
    with db.session() as conn:
        return dict(conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone())


def test_clean_and_base_url(monkeypatch):
    assert llm.clean("<think>x\ny</think>\n\n```markdown\n## 概述\n内容\n```") == "## 概述\n内容"
    assert llm.clean("推理过程</think>结果") == "结果"
    monkeypatch.setattr(config, "LLM_URL", "http://h:8000")
    assert llm.base_url() == "http://h:8000/v1"
    monkeypatch.setattr(config, "LLM_URL", "http://h:8000/api/v1")
    assert llm.base_url() == "http://h:8000/api/v1"


def test_split_text_prefers_line_breaks():
    text = "\n".join(f"第{i}行内容" for i in range(100))
    parts = summary.split_text(text, 60)
    assert all(len(p) <= 60 for p in parts)
    assert "\n".join(parts) == text  # 在换行处断开，不丢字


def test_summary_generated_after_ingest(tmp_path, fake):
    doc = _ingest(tmp_path, ["本项目为智慧城市平台建设，质保期三年，预算 386.5 万元。"])
    assert _row(doc["id"])["summary_status"] == "queued"
    summary.Worker()._step()
    row = _row(doc["id"])
    assert row["summary_status"] == "done" and row["summary"] == "## 概述\n这是概述。" and row["summary_at"]
    assert len(fake.calls) == 1 and "质保期三年" in fake.calls[0] and "## 主要技术指标" in fake.calls[0]
    body = fake.payloads[0]
    assert body["model"] == "qwen-test" and body["chat_template_kwargs"] == {"enable_thinking": False}
    r = TestClient(main.app).get(f"/api/documents/{doc['id']}/summary").json()
    assert r["status"] == "done" and r["summary"].startswith("## 概述")


def test_long_document_is_split_then_combined(tmp_path, fake, monkeypatch):
    monkeypatch.setattr(config, "LLM_CHUNK_CHARS", 200)
    pages = [f"第{i}章：系统应支持态势感知，指标{i}：响应时间不超过{i}秒。" * 4 for i in range(1, 6)]
    fake.reply = lambda user: "要点：" + user[-30:] if "部分原文" in user else "## 概述\n汇总"
    doc = _ingest(tmp_path, pages)
    summary.Worker()._step()
    extract = [c for c in fake.calls if "部分原文" in c]
    assert len(extract) >= 2  # 分段提取
    assert "各部分提取的要点" in fake.calls[-1]  # 最后按要点汇总
    assert _row(doc["id"])["summary"] == "## 概述\n汇总"


def test_notes_too_long_are_merged(tmp_path, fake, monkeypatch):
    monkeypatch.setattr(config, "LLM_CHUNK_CHARS", 120)
    fake.reply = lambda user: ("长要点" * 30) if "部分原文" in user else ("短" if "合并去重" in user else "## 概述\n终稿")
    doc = _ingest(tmp_path, ["第一段内容。" * 30, "第二段内容。" * 30])
    summary.Worker()._step()
    assert any("合并去重" in c for c in fake.calls)
    assert _row(doc["id"])["summary"] == "## 概述\n终稿"


def test_failure_is_recorded_and_can_retry(tmp_path, fake):
    doc = _ingest(tmp_path, ["质保期三年。"])
    fake.fail = 10
    summary.Worker()._step()
    row = _row(doc["id"])
    assert row["summary_status"] == "failed" and "GPU 忙" in row["summary_message"]
    fake.fail = 0
    client = TestClient(main.app)
    assert client.post("/api/documents/summarize", json={"ids": [doc["id"]]}).json() == {"queued": 1}
    summary.Worker()._step()
    assert _row(doc["id"])["summary_status"] == "done"


def test_transient_error_retried(tmp_path, fake):
    doc = _ingest(tmp_path, ["质保期三年。"])
    fake.fail = 2  # 前两次 500，第三次成功
    summary.Worker()._step()
    assert _row(doc["id"])["summary_status"] == "done"


def test_reindex_during_generation_discards_result(tmp_path, fake):
    doc = _ingest(tmp_path, ["质保期三年。"])

    def reindexed():  # 生成期间文档被重新解析：又排回 queued
        with db.session() as conn:
            summary.queue(conn, "id=?", (doc["id"],))
    fake.on_call = reindexed
    summary.Worker()._step()
    row = _row(doc["id"])
    assert row["summary_status"] == "queued" and row["summary"] is None


def test_summarize_endpoint(tmp_path, fake, monkeypatch):
    doc = _ingest(tmp_path, ["质保期三年。"])
    client = TestClient(main.app)
    with db.session() as conn:
        conn.execute("UPDATE documents SET summary_status='running' WHERE id=?", (doc["id"],))
    assert client.post("/api/documents/summarize", json={"ids": [doc["id"]]}).json() == {"queued": 0}  # 正在生成不重复排队
    assert client.get("/api/health").json()["llm"] == {"enabled": True, "up": True, "model": "qwen-test"}
    monkeypatch.setattr(config, "LLM_URL", "")
    assert client.post("/api/documents/summarize", json={"ids": [doc["id"]]}).status_code == 400


def test_no_llm_configured_skips_summary(tmp_path):
    doc = _ingest(tmp_path, ["质保期三年。"])
    assert _row(doc["id"])["summary_status"] is None
    r = TestClient(main.app).get(f"/api/documents/{doc['id']}/summary").json()
    assert r["llm"] is False and r["status"] is None
