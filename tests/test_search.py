import pytest

from app import embedder, search
from conftest import add_chunk, add_doc


def test_keyword_finds_literal_hits_that_tokenize_differently():
    doc = add_doc()
    a = add_chunk(doc, "质保期：两年")  # 切成 质保/期，FTS 能命中
    b = add_chunk(doc, "设备质保期限为两年")  # jieba 切成 质保/期限，FTS AND "质保 期" 匹配不上
    ids = {r["chunk_id"] for r in search.search("质保期", "keyword")["results"]}
    assert ids == {a, b}


def test_keyword_ranks_by_count_and_filters_doc():
    d1, d2 = add_doc("a.pdf"), add_doc("b.pdf")
    once = add_chunk(d1, "付款方式：转账")
    twice = add_chunk(d1, "付款方式一；付款方式二")
    add_chunk(d2, "付款方式：现金")
    res = search.search("付款方式", "keyword", doc_id=d1)["results"]
    assert [r["chunk_id"] for r in res] == [twice, once]


def test_hybrid_falls_back_to_keyword_when_embedding_down(monkeypatch):
    def down(_):
        raise RuntimeError("Embedding 服务调用失败")
    monkeypatch.setattr(embedder, "embed_query", down)
    cid = add_chunk(add_doc(), "项目编号 ZB-2024-001")
    data = search.search("项目编号", "hybrid")
    assert data["semantic_ok"] is False
    assert [r["chunk_id"] for r in data["results"]] == [cid]
    with pytest.raises(RuntimeError):
        search.search("项目编号", "semantic")


def test_ids_that_disappear_mid_search_are_skipped(monkeypatch):
    cid = add_chunk(add_doc(), "付款方式为转账")
    # 模拟 FTS/向量查询之后、加载片段之前该片段被删除
    monkeypatch.setattr(search, "_vector", lambda *a: [987654, cid])
    data = search.search("付款方式", "hybrid")
    assert [r["chunk_id"] for r in data["results"]] == [cid]
