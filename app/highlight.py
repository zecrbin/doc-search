"""在原文 PDF 上定位关键字：逐字取坐标后自己匹配，跨行、跨文字块的命中也能找到。

坐标统一为旋转后的页面坐标（pt，左上原点），和 PDF.js 渲染一致。扫描页没有文字层，找不到时由调用方退回段落框。
"""
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


def keyword_boxes(pdf_path, regions: list[list[float]], terms: list[str]) -> list[list[float]]:
    """regions：片段所在的文字块 [[page, x0, y0, x1, y1], ...]；返回关键字框，同样格式。"""
    terms = [t for t in (normalize_term(t) for t in terms) if t]
    if not terms or not regions:
        return []
    doc = pymupdf.open(pdf_path)
    try:
        page_chars: dict[int, list] = {}
        chars: list[str] = []
        boxes: list[tuple[int, pymupdf.Rect]] = []
        for pg, x0, y0, x1, y1 in regions:
            pg = int(pg)
            if not 0 <= pg < doc.page_count:
                continue
            if pg not in page_chars:
                page_chars[pg] = _page_chars(doc[pg])
            area = pymupdf.Rect(x0, y0, x1, y1) + (-2, -2, 2, 2)
            for c, r in page_chars[pg]:
                if (r.tl + r.br) / 2 in area:
                    chars.append(c)
                    boxes.append((pg, r))
    finally:
        doc.close()

    text = "".join(chars)
    out: list[list[float]] = []
    for t in terms:
        i = text.find(t)
        while i >= 0:
            out += _merge(boxes[i:i + len(t)])
            i = text.find(t, i + len(t))
    uniq = {tuple(b): b for b in out}
    return sorted(uniq.values(), key=lambda b: (b[0], b[2], b[1]))
