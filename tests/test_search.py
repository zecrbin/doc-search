import pytest

from app import embedder, search
from conftest import add_chunk, add_doc, chunk_ids


def test_keyword_finds_literal_hits_that_tokenize_differently():
    doc = add_doc()
    a = add_chunk(doc, "质保期：两年")  # 切成 质保/期，FTS 能命中
    b = add_chunk(doc, "设备质保期限为两年")  # jieba 切成 质保/期限，FTS AND "质保 期" 匹配不上
    assert set(chunk_ids(search.search("质保期"))) == {a, b}


def test_results_grouped_by_document():
    d1, d2 = add_doc("甲.pdf"), add_doc("乙.pdf")
    a1 = add_chunk(d1, "付款方式：转账")
    a2 = add_chunk(d1, "付款方式一；付款方式二")
    b1 = add_chunk(d2, "付款方式：现金")
    add_chunk(d2, "无关内容")
    data = search.search("付款方式")
    assert [(d["filename"], d["hit_count"]) for d in data["docs"]] == [("甲.pdf", 3), ("乙.pdf", 1)]
    assert {c["chunk_id"]: c["count"] for c in data["docs"][0]["chunks"]} == {a1: 1, a2: 2}
    assert chunk_ids(data)[-1] == b1
    assert (data["total_docs"], data["total_hits"]) == (2, 4)
    # 同一段内容出现在两个文件里：两个文件各自列出，不再折叠
    assert search.search("付款方式：")["total_docs"] == 2


def test_filename_is_searched():
    scan = add_doc("智慧城市项目扫描件.pdf")
    add_chunk(scan, "质保期为三年")
    other = add_doc("投标方案.pdf")
    add_chunk(other, "本方案不涉及扫描件")
    data = search.search("扫描件")
    # 文件名命中的排前面，即使正文里没有这个词
    assert [(d["filename"], d["filename_match"], len(d["chunks"])) for d in data["docs"]] == [
        ("智慧城市项目扫描件.pdf", True, 0), ("投标方案.pdf", False, 1)]
    # 一个词在文件名里、一个词在正文里，也算命中
    data = search.search("扫描件 质保期")
    assert [d["filename"] for d in data["docs"]] == ["智慧城市项目扫描件.pdf"]
    assert data["docs"][0]["hit_count"] == 1
    # 文件名里的词不会让这个文件的每一段都算命中
    add_chunk(scan, "付款方式")
    assert len(search.search("扫描件 质保期")["docs"][0]["chunks"]) == 1
    # 还在处理中的文件不出现
    add_doc("处理中的扫描件.pdf", status="parsing")
    assert "处理中的扫描件.pdf" not in [d["filename"] for d in search.search("扫描件")["docs"]]


def test_hybrid_falls_back_to_keyword_when_embedding_down(monkeypatch):
    def down(_):
        raise RuntimeError("Embedding 服务调用失败")
    monkeypatch.setattr(embedder, "embed_query", down)
    cid = add_chunk(add_doc(), "项目编号 ZB-2024-001")
    data = search.search("项目编号", "hybrid")
    assert data["semantic_ok"] is False
    assert chunk_ids(data) == [cid]
    with pytest.raises(RuntimeError):
        search.search("项目编号", "semantic")


def test_ids_that_disappear_mid_search_are_skipped(monkeypatch):
    cid = add_chunk(add_doc(), "付款方式为转账")
    # 模拟 FTS/向量查询之后、加载片段之前该片段被删除
    monkeypatch.setattr(search, "_vector", lambda *a: [987654, cid])
    assert chunk_ids(search.search("付款方式", "hybrid")) == [cid]
