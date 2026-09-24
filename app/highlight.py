"""在原文 PDF 上定位关键字：逐字取坐标后自己匹配，跨行、跨文字块的命中也能找到。

有文字层的页用 PDF 里每个字的坐标；扫描页用 MinerU OCR 的行坐标，行内按字宽估算每个字的位置。
坐标统一为旋转后的页面坐标（pt，左上原点），和 PDF.js 渲染一致。都找不到时由调用方退回段落框。
"""
import unicodedata

import pymupdf


def _norm(c: str) -> str:
    low = c.lower()
    return low if len(low) == 1 else c  # 个别字符小写后变长，保持一一对应


def normalize_term(t: str) -> str:
    return "".join(_norm(c) for c in t if not c.isspace())


def _page_chars(page: pymupdf.Page) -> list[tuple[str, pymupdf.Rect]]:
    m = page.rotation_matrix
    out = []
    for b in page.get_text("rawdict")["blocks"]:
        if b.get("type", 0) != 0:
            continue
        for line in b["lines"]:
            for span in line["spans"]:
                for ch in span["chars"]:
                    c = ch["c"]
                    if c and not c.isspace():
                        out.append((_norm(c), pymupdf.Rect(ch["bbox"]) * m))
    return out


def _char_weight(c: str) -> float:
    """估算字宽：中文等全角字符 1，半角字母数字约一半。"""
    if c.isspace():
        return 0.3
    return 1.0 if unicodedata.east_asian_width(c) in ("W", "F") else 0.55


def _ocr_chars(lines: list[list]) -> list[tuple[str, pymupdf.Rect]]:
    """OCR 行 [[文字, x0, y0, x1, y1], ...] → 逐字框（行内按字宽比例切分）。"""
    out = []
    for text, x0, y0, x1, y1 in lines:
        weights = [_char_weight(c) for c in text]
        total = sum(weights) or 1
        vertical = (y1 - y0) > (x1 - x0) * 1.5 and len(text.strip()) > 1  # 竖排
        span = (y1 - y0) if vertical else (x1 - x0)
        pos = 0.0
        for c, w in zip(text, weights):
            a, b = pos / total * span, (pos + w) / total * span
            pos += w
            if c.isspace():
                continue
            r = pymupdf.Rect(x0, y0 + a, x1, y0 + b) if vertical else pymupdf.Rect(x0 + a, y0, x0 + b, y1)
            out.append((_norm(c), r))
    return out


def _merge(hits: list[tuple[int, pymupdf.Rect]]) -> list[list[float]]:
    """把一次命中的逐字框按行合并：相邻字挨着就并进同一个框，换行后另起一个。"""
    groups: list[tuple[int, pymupdf.Rect]] = []
    for pg, r in hits:
        if groups and groups[-1][0] == pg:
            last = groups[-1][1]
            gap = min(r.width, r.height) * 0.6
            if (r + (-gap, -gap, gap, gap)).intersects(last):
                groups[-1] = (pg, last | r)
                continue
        groups.append((pg, pymupdf.Rect(r)))
    return [[pg, *(round(v, 1) for v in r)] for pg, r in groups]


def _chunk_matches(doc: pymupdf.Document, cache: dict, regions: list[list[float]], terms: list[str],
                   ocr_lines: dict[int, list]) -> list[list[list[float]]]:
    """一个片段里的每处命中，每处是一组框（跨行时有多个）。"""
    chars: list[str] = []
    boxes: list[tuple[int, pymupdf.Rect]] = []
    for pg, x0, y0, x1, y1 in regions:
        pg = int(pg)
        if not 0 <= pg < doc.page_count:
            continue
        ocr = ocr_lines.get(pg)
        if pg not in cache:
            # OCR 过的页以识别结果为准（扫描页没有文字层，乱码页的文字层不可信）
            cache[pg] = _ocr_chars(ocr) if ocr else _page_chars(doc[pg])
        pad = 4 if ocr else 2  # OCR 的段落框和行框来自不同步骤，多留点余量
        area = pymupdf.Rect(x0, y0, x1, y1) + (-pad, -pad, pad, pad)
        for c, r in cache[pg]:
            if (r.tl + r.br) / 2 in area:
                chars.append(c)
                boxes.append((pg, r))

    text = "".join(chars)
    found: dict[tuple, list[list[float]]] = {}
    for t in terms:
        i = text.find(t)
        while i >= 0:
            m = _merge(boxes[i:i + len(t)])
            found.setdefault(tuple(m[0]), m)
            i = text.find(t, i + len(t))
    return sorted(found.values(), key=lambda m: (m[0][0], m[0][2], m[0][1]))


def keyword_matches(pdf_path, chunks: list[list[list[float]]], terms: list[str],
                    ocr_lines: dict[int, list] | None = None) -> list[list[list[list[float]]]]:
    """chunks：每个片段的文字块 [[page, x0, y0, x1, y1], ...]；返回每个片段的命中列表，顺序和 chunks 一致。"""
    terms = [t for t in (normalize_term(t) for t in terms) if t]
    if not terms:
        return [[] for _ in chunks]
    doc = pymupdf.open(pdf_path)
    try:
        cache: dict[int, list] = {}
        return [_chunk_matches(doc, cache, regions, terms, ocr_lines or {}) for regions in chunks]
    finally:
        doc.close()


def keyword_boxes(pdf_path, regions: list[list[float]], terms: list[str],
                  ocr_lines: dict[int, list] | None = None) -> list[list[float]]:
    """单个片段的所有关键字框（不分组）。"""
    [matches] = keyword_matches(pdf_path, [regions], terms, ocr_lines)
    return sorted((b for m in matches for b in m), key=lambda b: (b[0], b[2], b[1]))
