"""扫描件：用 MinerU 返回的行坐标定位关键字。MinerU 服务用 httpx.MockTransport 模拟。"""
import json

import httpx
import pymupdf
import pytest
from fastapi.testclient import TestClient

from app import db, highlight, ingest, main, parser

LINE1 = "本项目为某市智慧城市综合管理平台建设"  # 18 个全角字，每字 12pt，行宽 216
LINE2 = "项目，项目编号GZ-2026-0815。"


def _scanned_pdf(path):
    """先画出文字再整页转成图片：得到一份没有文字层的"扫描件"。"""
    src = pymupdf.open()
    page = src.new_page(width=595, height=842)
    page.insert_text((60, 100), LINE1, fontname="china-s", fontsize=12)
    page.insert_text((60, 118), LINE2, fontname="china-s", fontsize=12)
    pix = page.get_pixmap(dpi=100)
    out = pymupdf.open()
    out.new_page(width=595, height=842).insert_image(pymupdf.Rect(0, 0, 595, 842), pixmap=pix)
    out.save(path)


def _fake_mineru(request: httpx.Request) -> httpx.Response:
    # MinerU 的 middle_json 按渲染尺寸给坐标（这里故意用 2 倍页面尺寸，检验换算）
    s = 2
    middle = {"pdf_info": [{
        "page_idx": 0, "page_size": [595 * s, 842 * s],
        "para_blocks": [{"type": "text", "bbox": [60 * s, 89 * s, 290 * s, 122 * s], "lines": [
            {"bbox": [60 * s, 89 * s, 276 * s, 103 * s], "spans": [
                {"type": "text", "content": LINE1, "bbox": [60 * s, 89 * s, 276 * s, 103 * s]}]},
            {"bbox": [60 * s, 107 * s, 245 * s, 121 * s], "spans": [
                {"type": "text", "content": LINE2, "bbox": [60 * s, 107 * s, 245 * s, 121 * s]}]},
        ]}, {"type": "table", "bbox": [0, 0, 1, 1], "blocks": [
            {"type": "table_body", "lines": [{"spans": [{"type": "table", "html": "<table></table>"}]}]}]}],
    }]}
    content = [{"type": "text", "text": LINE1 + LINE2, "page_idx": 0,
                "bbox": [60 / 595 * 1000, 89 / 842 * 1000, 290 / 595 * 1000, 122 / 842 * 1000]}]
    return httpx.Response(200, json={"results": {"part": {
        "content_list": json.dumps(content, ensure_ascii=False),
        "middle_json": json.dumps(middle, ensure_ascii=False),
    }}})


@pytest.fixture
def mineru(monkeypatch):
    real = httpx.Client
    monkeypatch.setattr(parser.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(_fake_mineru), **kw))


def test_mineru_lines_scaled_to_page(tmp_path, mineru):
    pdf = tmp_path / "scan.pdf"
    _scanned_pdf(pdf)
    blocks, pages, ocr_pages, lines = parser.parse_pdf(pdf, lambda *_: None)
    assert ocr_pages == 1 and blocks[0].text.startswith("本项目")
    assert lines == {0: [[LINE1, 60, 89, 276, 103], [LINE2, 60, 107, 245, 121]]}  # 表格 span 没有文字，跳过


def test_scanned_pdf_keyword_boxes_end_to_end(tmp_path, mineru):
    pdf = tmp_path / "scan.pdf"
    _scanned_pdf(pdf)
    doc = ingest.register_file(pdf, "智慧城市项目扫描件.pdf")
    ingest.Worker()._step()
    assert ingest.ocr_lines_path(doc["id"]).exists()

    url = f"/api/documents/{doc['id']}/highlights"
    hl = TestClient(main.app).get(url, params={"q": "城市"}).json()
    assert hl["ocr"] and [h["exact"] for h in hl["hits"]] == [True]
    [[pg, x0, y0, x1, y1]] = hl["hits"][0]["boxes"]
    # "城市" 是第 9、10 个字：60 + 8×12 = 156 到 180
    assert pg == 0 and abs(x0 - 156) < 1 and abs(x1 - 180) < 1 and (y0, y1) == (89, 103)

    # 跨行 + 半角字母数字按字宽估算
    hl = TestClient(main.app).get(url, params={"q": "建设项目"}).json()
    assert [b[2] for b in hl["hits"][0]["boxes"]] == [89, 107]  # 一处命中：行尾一个框、下一行一个框
    hl = TestClient(main.app).get(url, params={"q": "gz-2026-0815"}).json()
    [[_, x0, _, x1, _]] = hl["hits"][0]["boxes"]
    assert 60 + 7 * 12 - 20 < x0 < x1 < 245  # 在第二行的编号附近

    ingest.remove_files({**doc, "sha256": "gone"})
    assert not ingest.ocr_lines_path(doc["id"]).exists()


def test_ocr_chars_vertical_and_weights():
    chars = highlight._ocr_chars([["AB中", 0, 0, 21, 10]])
    widths = [round(r.width, 1) for _, r in chars]
    assert [c for c, _ in chars] == ["a", "b", "中"] and widths[0] == widths[1] < widths[2]
    vert = highlight._ocr_chars([["竖排文字", 0, 0, 10, 40]])
    assert [round(r.y0) for _, r in vert] == [0, 10, 20, 30]


def test_old_mineru_without_middle_json_still_ingests(tmp_path, monkeypatch):
    def no_middle(request):
        r = _fake_mineru(request)
        body = r.json()
        body["results"]["part"].pop("middle_json")
        return httpx.Response(200, json=body)
    real = httpx.Client
    monkeypatch.setattr(parser.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(no_middle), **kw))
    pdf = tmp_path / "scan.pdf"
    _scanned_pdf(pdf)
    doc = ingest.register_file(pdf, "scan.pdf")
    ingest.Worker()._step()
    with db.session() as conn:
        assert conn.execute("SELECT status FROM documents WHERE id=?", (doc["id"],)).fetchone()[0] == "done"
    hl = TestClient(main.app).get(f"/api/documents/{doc['id']}/highlights", params={"q": "城市"}).json()
    assert [h["exact"] for h in hl["hits"]] == [False]  # 没有行坐标：退回整段框
