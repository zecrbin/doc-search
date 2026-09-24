from app import chunker, config


def test_split_text_keeps_order_around_hard_cut(monkeypatch):
    monkeypatch.setattr(config, "CHUNK_MAX", 20)
    monkeypatch.setattr(config, "CHUNK_TARGET", 10)
    text = "甲乙。" + "X" * 50 + "。尾"
    pieces = chunker._split_text(text)
    assert "".join(pieces) == text
    assert all(len(p) <= 20 for p in pieces)


def test_split_table_keeps_long_rows(monkeypatch):
    monkeypatch.setattr(config, "CHUNK_MAX", 20)
    rows = ["名称 | 数量", "A" * 45, "B | 2"]
    pieces = chunker._split_table("\n".join(rows))
    body = "".join(p.split("\n", 1)[1].replace("\n", "") for p in pieces)
    assert body == "A" * 45 + "B | 2"
    assert all(p.startswith("名称 | 数量\n") for p in pieces)


def test_split_table_single_line(monkeypatch):
    monkeypatch.setattr(config, "CHUNK_MAX", 20)
    text = "C" * 45
    assert "".join(chunker._split_table(text)) == text
