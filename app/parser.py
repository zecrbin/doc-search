"""PDF 解析：有文字层的页用 PyMuPDF 本地提取（毫秒级），扫描页交给 MinerU OCR（秒级/页）。

输出统一的 Block 列表，bbox 为 PDF 坐标（pt，左上原点），和 PDF.js 渲染坐标一致。
"""
import json
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Callable

import httpx
import pymupdf

from . import config

log = logging.getLogger(__name__)

Progress = Callable[[float, str], None]
# OCR 页的文字行：{页号: [[文字, x0, y0, x1, y1], ...]}，扫描件上定位关键字用
OcrLines = dict[int, list[list]]


@dataclass
class Block:
    page: int
    bbox: tuple[float, float, float, float]
    kind: str  # text / title / table
    text: str
    level: int = 0
    cont: bool = False  # 与上一个文字块属于同一段（折行或跨页），拼接时不换行


@dataclass
class _Raw:
    page: int
    bbox: tuple[float, float, float, float]
    text: str
    is_table: bool = False
    size: float = 0
    bold: bool = False
    nlines: int = 1
    page_height: float = field(default=0, repr=False)


def parse_pdf(pdf_path, progress: Progress) -> tuple[list[Block], int, int, OcrLines]:
    """返回 (blocks, 页数, OCR 页数, OCR 文字行)。"""
    doc = pymupdf.open(pdf_path)
    try:
        n = doc.page_count
        if config.PARSE_MODE == "mineru":
            ocr_pages = list(range(n))
        else:
            ocr_pages = [i for i in range(n) if _needs_ocr(doc[i])]
        ocr_set = set(ocr_pages)

        raws: list[_Raw] = []
        local_pages = [i for i in range(n) if i not in ocr_set]
        for k, i in enumerate(local_pages):
            raws.extend(_extract_page(doc[i]))
            if k % 20 == 0:
                progress(0.3 * k / max(1, len(local_pages)), f"提取文字 {k}/{len(local_pages)} 页")
        local_blocks = _classify(_drop_margins(raws, len(local_pages)))

        remote_blocks: list[Block] = []
        ocr_lines: OcrLines = {}
        if ocr_pages:
            remote_blocks, ocr_lines = _mineru(doc, ocr_pages, progress, force_ocr=config.PARSE_MODE != "mineru")

        by_page: dict[int, list[Block]] = defaultdict(list)
        for b in local_blocks + remote_blocks:
            by_page[b.page].append(b)
        blocks = [b for p in sorted(by_page) for b in by_page[p]]
        _mark_continuations(blocks)
        return blocks, n, len(ocr_pages), ocr_lines
    finally:
        doc.close()


_PARA_END = tuple("。！？；：!?;:…”」』")


def _mark_continuations(blocks: list[Block]):
    """PDF 里每行常是独立的块：上一块没以句末标点结束、且写满到右边缘（或在上一页末尾）时视为同段续行。"""
    right: dict[int, float] = defaultdict(float)
    for b in blocks:
        if b.kind == "text":
            right[b.page] = max(right[b.page], b.bbox[2])
    prev: Block | None = None
    for b in blocks:
        if b.kind != "text":
            prev = None
            continue
        if prev and not prev.text.endswith(_PARA_END):
            if prev.page == b.page:
                line_h = min(prev.bbox[3] - prev.bbox[1], b.bbox[3] - b.bbox[1])
                b.cont = (prev.bbox[2] >= right[b.page] * 0.9
                          and 0 <= b.bbox[1] - prev.bbox[3] < line_h * 1.2)
            else:
                b.cont = b.page == prev.page + 1
        prev = b


# ---------------------------------------------------------------- 扫描页判断

def _needs_ocr(page: pymupdf.Page) -> bool:
    chars = [c for c in page.get_text("text") if not c.isspace()]
    if len(chars) >= 30:
        # 字体缺 ToUnicode 时会提取出乱码/私有区字符，这种页也按扫描页处理
        bad = sum(1 for c in chars if c == "�" or 0xE000 <= ord(c) <= 0xF8FF)
        if bad / len(chars) > 0.3:
            return True
    area = abs(page.rect) or 1
    img_area = sum(abs(pymupdf.Rect(i["bbox"]) & page.rect) for i in page.get_image_info())
    ratio = img_area / area
    if ratio > 0.5 and len(chars) < 300:  # 整页扫描件、插在文档里的证书图片
        return True
    if len(chars) < 30:
        return ratio > 0.1 or len(page.get_drawings()) > 200  # 文字转曲的页
    return False


# ---------------------------------------------------------------- 本地提取

def _extract_page(page: pymupdf.Page) -> list[_Raw]:
    h = page.rect.height
    tables: list[_Raw] = []
    try:
        for t in page.find_tables().tables:
            if t.row_count >= 2 and t.col_count >= 2:
                text = _rows_text(t.extract())
                if text:
                    tables.append(_Raw(page.number, tuple(t.bbox), text, is_table=True, page_height=h))
    except Exception as e:
        log.debug("表格识别失败 page=%s: %s", page.number, e)
    table_rects = [pymupdf.Rect(t.bbox) for t in tables]

    texts: list[_Raw] = []
    for b in page.get_text("dict", sort=True)["blocks"]:
        if b["type"] != 0:
            continue
        rect = pymupdf.Rect(b["bbox"])
        center = (rect.tl + rect.br) / 2
        if any(center in tr for tr in table_rects):
            continue
        lines, sizes, bold, total = [], Counter(), 0, 0
        for line in b["lines"]:
            s = "".join(sp["text"] for sp in line["spans"])
            if s.strip():
                lines.append(s.strip())
            for sp in line["spans"]:
                n = len(sp["text"].strip())
                sizes[round(sp["size"] * 2) / 2] += n
                total += n
                if sp["flags"] & 16 or re.search(r"bold|heavy|black", sp["font"], re.I):
                    bold += n
        text = _join_lines(lines)
        if text:
            texts.append(_Raw(page.number, tuple(b["bbox"]), text, size=sizes.most_common(1)[0][0],
                              bold=bold > total / 2, nlines=len(lines), page_height=h))

    # 表格按纵坐标插回文字流
    out = texts
    for t in sorted(tables, key=lambda r: r.bbox[1]):
        idx = next((i for i, r in enumerate(out) if r.bbox[1] > t.bbox[1]), len(out))
        out.insert(idx, t)
    # PyMuPDF 给的是未旋转页面的坐标，PDF.js 显示的是旋转后的页面（横向页常见），统一换成旋转后的
    if page.rotation:
        m = page.rotation_matrix
        for r in out:
            r.bbox = tuple(pymupdf.Rect(r.bbox) * m)
    return out


def _join_lines(lines: list[str]) -> str:
    out = ""
    for s in lines:
        if out and out[-1].isascii() and out[-1].isalnum() and s[0].isascii() and s[0].isalnum():
            out += " "
        out += s
    return out.strip()


def _rows_text(rows: list[list]) -> str:
    lines = []
    for row in rows:
        cells = [re.sub(r"\s+", " ", c or "").strip() for c in row]
        if any(cells):
            lines.append(" | ".join(cells))
    return "\n".join(lines)


_PAGE_NO = re.compile(r"[-—\s]*(第\s*)?\d+\s*页?\s*([/／共]\s*\d+\s*页?)?[-—\s]*")


def _drop_margins(raws: list[_Raw], n_pages: int) -> list[_Raw]:
    """去掉页眉页脚：页面上下边缘、跨页重复（数字归一化后）或纯页码的块。"""
    def in_margin(r: _Raw) -> bool:
        return r.bbox[1] < r.page_height * 0.08 or r.bbox[3] > r.page_height * 0.92

    def key(r: _Raw) -> str:
        return re.sub(r"\d+", "#", re.sub(r"\s+", "", r.text))

    pages_by_key: dict[str, set[int]] = defaultdict(set)
    for r in raws:
        if in_margin(r) and not r.is_table:
            pages_by_key[key(r)].add(r.page)
    threshold = max(3, n_pages * 0.3)
    return [
        r for r in raws
        if r.is_table or not in_margin(r)
        or not (len(pages_by_key[key(r)]) >= threshold or _PAGE_NO.fullmatch(r.text))
    ]


# ---------------------------------------------------------------- 标题识别

_CN_NUM = "一二三四五六七八九十百零〇"
_H1 = re.compile(rf"^(第[{_CN_NUM}\d]+[章部篇]|[{_CN_NUM}]+[、．.]\s*\S)")
_H2 = re.compile(rf"^(第[{_CN_NUM}\d]+节|[（(][{_CN_NUM}]+[)）]|\d+[.．]\d+(?![.．]?\d)\s*\S)")
_H3 = re.compile(r"^\d+[.．]\d+[.．]\d+")
_STRONG = re.compile(rf"^第[{_CN_NUM}\d]+[章节部篇]")
_TOC_LINE = re.compile(r"(\.{3,}|…{2,}|·{3,}|-{4,})\s*\d+\s*$")


def _pattern_level(t: str) -> int:
    if _H3.match(t):
        return 3
    if _H2.match(t):
        return 2
    if _H1.match(t):
        return 1
    return 0


def _classify(raws: list[_Raw]) -> list[Block]:
    weights = Counter()
    for r in raws:
        if not r.is_table:
            weights[r.size] += len(r.text)
    body = weights.most_common(1)[0][0] if weights else 10.5

    heads: list[tuple[_Raw, int, bool]] = []
    for r in raws:
        if r.is_table:
            continue
        t = r.text
        short = len(t) <= 40 and r.nlines <= 2 and not t.endswith(("。", "；", ";", "，", ","))
        if not short or _TOC_LINE.search(t):
            continue
        lvl = _pattern_level(t)
        bigger = r.size >= body * 1.15
        if bigger or (lvl and r.bold) or _STRONG.match(t):
            heads.append((r, lvl, bigger))

    # 没有编号的标题按字号排级别
    sizes = sorted({r.size for r, _, big in heads if big}, reverse=True)
    level_of = {id(r): (lvl or (sizes.index(r.size) + 1 if r.size in sizes else 2)) for r, lvl, _ in heads}

    out = []
    for r in raws:
        if r.is_table:
            out.append(Block(r.page, r.bbox, "table", r.text))
        elif id(r) in level_of:
            out.append(Block(r.page, r.bbox, "title", r.text, min(level_of[id(r)], 4)))
        else:
            out.append(Block(r.page, r.bbox, "text", r.text))
    return out


# ---------------------------------------------------------------- MinerU

_SKIP_TYPES = {"header", "footer", "page_number", "page_footnote", "aside_text", "discarded"}


def _mineru(doc: pymupdf.Document, pages: list[int], progress: Progress,
            force_ocr: bool) -> tuple[list[Block], OcrLines]:
    out: list[Block] = []
    lines: OcrLines = {}
    step = max(1, config.MINERU_BATCH_PAGES)
    with httpx.Client(timeout=config.MINERU_TIMEOUT) as client:
        for start in range(0, len(pages), step):
            batch = pages[start:start + step]
            progress(0.3 + 0.7 * start / len(pages), f"OCR 识别 {start}/{len(pages)} 页（MinerU）")
            sub = pymupdf.open()
            for p in batch:
                sub.insert_pdf(doc, from_page=p, to_page=p)
            data = sub.tobytes()
            sub.close()
            resp = client.post(
                f"{config.MINERU_URL}/file_parse",
                files=[("files", ("part.pdf", data, "application/pdf"))],
                data={
                    "backend": "pipeline",
                    "parse_method": "ocr" if force_ocr else "auto",
                    "lang_list": ["ch"],
                    "formula_enable": "false",
                    "table_enable": "true",
                    "return_md": "false",
                    "return_content_list": "true",
                    "return_middle_json": "true",  # 带行级坐标，用于在扫描件上定位关键字
                },
            )
            if resp.status_code != 200:
                raise RuntimeError(f"MinerU 返回 {resp.status_code}: {resp.text[:300]}")
            result = next(iter(resp.json()["results"].values()))
            items = result["content_list"]
            if isinstance(items, str):
                items = json.loads(items)
            for item in items:
                b = _mineru_block(item, batch, doc)
                if b:
                    out.append(b)
            try:
                lines.update(_mineru_lines(result.get("middle_json"), batch, doc))
            except Exception as e:  # 拿不到行坐标只影响扫描件上的关键字定位（退回整段框），不影响入库
                log.warning("MinerU 行坐标解析失败：%s", e)
    return out, lines


def _iter_spans(blocks: list):
    for b in blocks or []:
        for line in b.get("lines") or []:
            yield from line.get("spans") or []
        yield from _iter_spans(b.get("blocks"))  # 表格、列表等嵌套块


def _mineru_lines(middle, batch: list[int], doc: pymupdf.Document) -> OcrLines:
    """middle_json 里每个文字 span（通常一行一个）的内容和坐标（PDF pt，按页面尺寸换算到本页）。"""
    if not middle:
        return {}
    if isinstance(middle, str):
        middle = json.loads(middle)
    out: OcrLines = {}
    for info in middle.get("pdf_info") or []:
        idx = info.get("page_idx", 0)
        if not 0 <= idx < len(batch):
            continue
        page = batch[idx]
        rect = doc[page].rect
        pw, ph = info.get("page_size") or (rect.width, rect.height)
        sx, sy = rect.width / pw, rect.height / ph
        spans = []
        for s in _iter_spans(info.get("para_blocks")):
            text, bbox = s.get("content"), s.get("bbox")
            if s.get("type", "text") != "text" or not isinstance(text, str) or not text.strip() or not bbox or len(bbox) != 4:
                continue
            x0, y0, x1, y1 = bbox
            spans.append([text, round(x0 * sx, 1), round(y0 * sy, 1), round(x1 * sx, 1), round(y1 * sy, 1)])
        if spans:
            out[page] = spans
    return out


def _mineru_block(item: dict, batch: list[int], doc: pymupdf.Document) -> Block | None:
    typ = item.get("type", "text")
    if typ in _SKIP_TYPES:
        return None
    idx = item.get("page_idx", 0)
    if idx >= len(batch):
        return None
    page = batch[idx]
    rect = doc[page].rect
    # MinerU content_list 的 bbox 是按页宽高归一化到 0~1000 的坐标
    x0, y0, x1, y1 = item.get("bbox") or (0, 0, 1000, 1000)
    sx, sy = rect.width / 1000, rect.height / 1000
    bbox = (x0 * sx, y0 * sy, x1 * sx, y1 * sy)

    if typ == "table":
        parts = [*item.get("table_caption", []), _html_table_text(item.get("table_body", "")),
                 *item.get("table_footnote", [])]
        text = "\n".join(p for p in parts if p and p.strip())
        return Block(page, bbox, "table", text) if text else None
    if typ == "image":
        text = " ".join(item.get("image_caption", []) + item.get("image_footnote", []))
    elif typ == "list":
        text = "\n".join(item.get("list_items", [])) or item.get("text", "")
    else:
        text = item.get("text", "")
    text = (text or "").strip()
    if not text:
        return None
    level = item.get("text_level") or 0
    if level:
        return Block(page, bbox, "title", text, _pattern_level(text) or min(level, 4))
    return Block(page, bbox, "text", text)


class _TableHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.rows.append([])
        elif tag in ("td", "th"):
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            if not self.rows:
                self.rows.append([])
            self.rows[-1].append("".join(self._cell))
            self._cell = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _html_table_text(html: str) -> str:
    p = _TableHTML()
    p.feed(html or "")
    return _rows_text(p.rows)
