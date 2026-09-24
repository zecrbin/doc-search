import pymupdf
import pytest

from app import highlight, parser


def _pdf(tmp_path, rotate=0):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    # 一个段落自动折行："管理平台" 在行尾，"建设" 在下一行
    page.insert_textbox(pymupdf.Rect(60, 60, 260, 200),
                        "本项目为某市智慧城市综合管理平台建设项目，项目编号 GZ-2026-0815，预算金额。",
                        fontname="china-s", fontsize=12)
    page.insert_text((60, 400), "另一段也有城市二字", fontname="china-s", fontsize=12)
    if rotate:
        page.set_rotation(rotate)
    path = tmp_path / "t.pdf"
    doc.save(path)
    return path


def _regions(path):
    """和入库时一样，用解析器取文字块作为片段范围。"""
    blocks, _, _ = parser.parse_pdf(path, lambda *_: None)
    return [[b.page, *b.bbox] for b in blocks]


def _text_at(path, box):
    """框里有哪些字（按字的中心点判断）。"""
    page = pymupdf.open(path)[int(box[0])]
    area = pymupdf.Rect(box[1:])
    return "".join(c for c, r in highlight._page_chars(page) if (r.tl + r.br) / 2 in area)


def test_boxes_cover_only_the_keyword(tmp_path):
    path = _pdf(tmp_path)
    regions = _regions(path)[:1]  # 只取第一段
    boxes = highlight.keyword_boxes(path, regions, ["城市"])
    assert len(boxes) == 1
    assert _text_at(path, boxes[0]) == "城市"


def test_match_across_line_break(tmp_path):
    path = _pdf(tmp_path)
    boxes = highlight.keyword_boxes(path, _regions(path)[:1], ["管理平台建设"])
    assert len(boxes) == 2  # 行尾一个框、下一行一个框
    assert "".join(_text_at(path, b) for b in boxes) == "管理平台建设"


def test_case_insensitive_and_multiple_terms(tmp_path):
    path = _pdf(tmp_path)
    boxes = highlight.keyword_boxes(path, _regions(path), ["gz-2026", "城市"])
    texts = sorted(_text_at(path, b) for b in boxes)
    assert texts == ["gz-2026", "城市", "城市"]  # _text_at 取到的是归一化后的小写


@pytest.mark.parametrize("rotate", [90, 270])
def test_rotated_page_uses_displayed_coordinates(tmp_path, rotate):
    path = _pdf(tmp_path, rotate)
    page = pymupdf.open(path)[0]
    regions = _regions(path)
    # 文字块坐标在旋转后的页面范围内（PDF.js 按旋转后的页面显示）
    assert all(0 <= r[1] <= r[3] <= page.rect.width and 0 <= r[2] <= r[4] <= page.rect.height for r in regions)
    boxes = highlight.keyword_boxes(path, regions[:1], ["城市"])
    assert [_text_at(path, b) for b in boxes] == ["城市"]


def test_no_text_layer_returns_nothing(tmp_path):
    doc = pymupdf.open()
    doc.new_page()
    path = tmp_path / "scan.pdf"
    doc.save(path)
    assert highlight.keyword_boxes(path, [[0, 0, 0, 500, 500]], ["城市"]) == []
