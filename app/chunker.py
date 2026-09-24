"""按文档结构切块：标题处断开，段落累积到目标长度，表格单独成块（过长按行拆并重复表头）。"""
import re
from dataclasses import dataclass, field

from . import config
from .parser import Block


@dataclass
class Chunk:
    kind: str
    heading: str
    text: str
    regions: list[list[float]] = field(default_factory=list)  # [page, x0, y0, x1, y1]

    @property
    def page_start(self) -> int:
        return int(self.regions[0][0]) if self.regions else 0


def _region(b: Block) -> list[float]:
    return [b.page, *(round(v, 1) for v in b.bbox)]


def build_chunks(blocks: list[Block]) -> list[Chunk]:
    out: list[Chunk] = []
    stack: list[str] = []
    buf: list[str] = []
    regions: list[list[float]] = []

    def heading() -> str:
        return " > ".join(stack)

    def flush():
        if buf:
            out.append(Chunk("text", heading(), "\n".join(buf), list(regions)))
        buf.clear()
        regions.clear()

    for b in blocks:
        if b.kind == "title":
            flush()
            lvl = max(1, b.level or 1)
            del stack[lvl - 1:]
            stack.append(b.text)
            continue
        if b.kind == "table":
            flush()
            for piece in _split_table(b.text):
                out.append(Chunk("table", heading(), piece, [_region(b)]))
            continue
        for i, piece in enumerate(_split_text(b.text)):
            if buf and sum(map(len, buf)) + len(piece) > config.CHUNK_MAX:
                flush()
            # 同一段落的折行/拆句直接接上，保证跨行的关键字也能整体命中
            if buf and (i > 0 or b.cont):
                ascii_gap = buf[-1][-1:].isascii() and buf[-1][-1:].isalnum() and piece[:1].isascii() and piece[:1].isalnum()
                buf[-1] += (" " if ascii_gap and i == 0 else "") + piece
            else:
                buf.append(piece)
            r = _region(b)
            if not regions or regions[-1] != r:
                regions.append(r)
            if sum(map(len, buf)) >= config.CHUNK_TARGET:
                flush()
    flush()
    return out


_SENT_END = re.compile(r"(?<=[。！？；!?;\n])")


def _split_text(text: str) -> list[str]:
    if len(text) <= config.CHUNK_MAX:
        return [text]
    pieces, cur = [], ""
    for sent in _SENT_END.split(text):
        if len(sent) > config.CHUNK_MAX and cur:  # 硬切前先交出前面累积的句子，保持原文顺序
            pieces.append(cur)
            cur = ""
        while len(sent) > config.CHUNK_MAX:  # 没有标点的超长句硬切
            pieces.append(sent[:config.CHUNK_MAX])
            sent = sent[config.CHUNK_MAX:]
        if cur and len(cur) + len(sent) > config.CHUNK_TARGET:
            pieces.append(cur)
            cur = ""
        cur += sent
    if cur.strip():
        pieces.append(cur)
    return pieces


def _split_table(text: str) -> list[str]:
    if len(text) <= config.CHUNK_MAX:
        return [text]
    lines = text.split("\n")
    if len(lines) == 1:
        return [text[i:i + config.CHUNK_MAX] for i in range(0, len(text), config.CHUNK_MAX)]
    header, pieces, cur = lines[0], [], [lines[0]]
    # 超长的行按 CHUNK_MAX 拆成多段，不丢内容
    segs = [line[i:i + config.CHUNK_MAX] for line in lines[1:] for i in range(0, len(line), config.CHUNK_MAX)]
    for line in segs:
        if len(cur) > 1 and sum(map(len, cur)) + len(line) > config.CHUNK_MAX:
            pieces.append("\n".join(cur))
            cur = [header]
        cur.append(line)
    if len(cur) > 1:
        pieces.append("\n".join(cur))
    return pieces
