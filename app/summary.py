"""文档"服务内容"：解析完成后由大模型写成一段话。

长文档一次放不进模型上下文：先按 LLM_CHUNK_CHARS 分段逐段提取要点，要点太多再合并压缩，最后写成一段话。
单独的后台线程处理，不耽误文档解析和检索。
写法可以用本地示例文件引导：数据目录下 summary_examples.txt，每行一条示例（只留在本机，不进代码仓库）。
"""
import logging
import math
import re
import threading
import time

from . import config, db, llm

log = logging.getLogger(__name__)

SYSTEM = ("你是招投标与项目文件分析助手，负责为项目撰写\u201c服务内容\u201d简介。只根据用户提供的原文作答，"
          "不编造原文没有的内容。用简体中文，书面化表达。")

FORMAT = """写作要求：
1. 只输出一段话，150～300 字；不分点、不加标题、不用 Markdown，不要写"服务内容："之类的开头。
2. 以"本项目"开头，依次说明：建设或提供什么（平台、系统、软件或服务）；整合了哪些系统、数据或资源；包含哪些主要功能模块或服务内容；采用了哪些关键技术；最终实现什么效果和价值。
3. 原文写明了服务范围（如软件部署、系统集成、技术培训、质保运维期限）时，在最后一句带上。
4. 概括归纳，不罗列具体技术参数和指标数值，不写金额、编号、日期；原文没有的内容不要写。

写法示例（××为占位，只参考结构和语气，内容必须来自本文件）：
本项目为××单位建设××综合管理平台，整合××、××等业务系统与数据资源，搭建集数据采集、处理分析与可视化展示于一体的应用体系。平台提供××、××及××等功能，依托××技术实现对××的智能研判与动态预警，全面提升××工作的信息化与智能化水平。项目包含软件部署、系统集成、技术培训及×年质保运维服务。"""

EXTRACT = """下面是文件《{name}》的第 {i}/{n} 部分原文。请提取其中与项目服务内容有关的信息，用简洁的要点列出：
项目名称和建设目标；要建设或提供的平台、系统、软件或服务；整合的系统、数据和资源；主要功能模块；采用的关键技术；
服务范围（部署、集成、培训、质保运维期限等）；项目要实现的效果。
这一部分没有的信息不要写，也不要编造。

原文：
{text}"""

MERGE = """下面是从文件《{name}》各部分提取的要点。请合并去重、归类整理，保留建设内容、功能模块、关键技术、服务范围和建设目标，不要编造：

{text}"""

FINAL_FROM_TEXT = """下面是文件《{name}》的原文。请根据原文写这个项目的"服务内容"。

{format}
{examples}
原文：
{text}"""

FINAL_FROM_NOTES = """下面是从文件《{name}》各部分提取的要点。请根据这些要点写这个项目的"服务内容"。

{format}
{examples}
要点：
{text}"""

EXAMPLES_FILE = "summary_examples.txt"


def examples_text(limit: int = 3) -> str:
    """数据目录下的 summary_examples.txt（每行一条示例），取前几条作为写法参考。"""
    path = config.DATA_DIR / EXAMPLES_FILE
    try:
        lines = [x.strip() for x in path.read_text(encoding="utf-8-sig").splitlines()]
    except (OSError, UnicodeDecodeError):
        return ""
    picked = [x for x in lines if len(x) >= 30][:limit]
    if not picked:
        return ""
    return ("\n参考以下本单位的示例写法（只学习写法和详略，不要照搬其中的内容）：\n"
            + "\n".join(f"示例{i}：{x}" for i, x in enumerate(picked, 1)) + "\n")


_HEADING = re.compile(r"^(#+\s*|【?\s*服务内容\s*】?\s*[:：]?\s*$)")
_BULLET = re.compile(r"^(?:[-*•·]|\d+[.、)）])\s*")
_LEAD = re.compile(r"^\s*(?:服务内容|项目服务内容)\s*[:：]\s*")


def one_paragraph(text: str) -> str:
    """模型没按要求时的兜底：去掉标题、列表符号和 Markdown 加粗，合成一段话。"""
    parts = []
    for line in text.replace("**", "").splitlines():
        line = line.strip()
        if not line or _HEADING.match(line):
            continue
        line = _LEAD.sub("", _BULLET.sub("", line))
        if parts and not parts[-1].endswith(("。", "；", "！", "？", "，", "：")):
            parts[-1] += "；"
        parts.append(line)
    out = "".join(parts).rstrip("；，：")
    return out + "。" if out and not out.endswith(("。", "！", "？")) else out


def document_text(doc_id: int) -> str:
    """按原文顺序拼出文档全文，标题变化处插一行【标题】，给模型结构线索。"""
    with db.session() as conn:
        rows = conn.execute("SELECT heading, text FROM chunks WHERE doc_id=? ORDER BY seq", (doc_id,)).fetchall()
    out, prev = [], None
    for heading, text in rows:
        if heading and heading != prev:
            out.append(f"【{heading}】")
        prev = heading
        out.append(text)
    return "\n".join(out)


def split_text(text: str, size: int) -> list[str]:
    """按长度切分，尽量在换行处断开。"""
    parts = []
    while len(text) > size:
        cut = text.rfind("\n", size // 2, size)
        cut = cut if cut > 0 else size
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text.strip():
        parts.append(text)
    return parts


def summarize(name: str, text: str, report=lambda msg, frac, draft=None: None) -> str:
    """report(说明, 整体进度 0～1, 概述草稿)：进度回调，生成最后的概述时带上已生成的部分。"""
    size = llm.chunk_chars()
    while True:
        try:
            return _summarize(name, text, size, report)
        except llm.ContextOverflow:  # 估算偏大或服务端上下文比报告的小：分段减半重试，直到每段不足 1000 字
            if size <= 1000:
                raise RuntimeError("原文分段后仍超出大模型上下文长度：请调大 llama-server 的 -c 参数，"
                                   "或设置 DOCSEARCH_LLM_CHUNK_CHARS 为更小的值")
            size //= 2
            log.info("%s：超出上下文，改为每段 %d 字重试", name, size)
            report(f"超出上下文，改为每段 {size} 字重试", 0)


# 估算步内进度：输出越多越接近完成，按 1 - e^(-字数/常数) 增长（400 字的要点约 55%，250 字的服务内容约 75%），
# 输出长短不一也不会卡住或提前到 100%
_EXPECT_NOTES, _EXPECT_SUMMARY = 500, 180


def _summarize(name: str, text: str, size: int, report) -> str:
    parts = split_text(text, size) if len(text) > size else [text]
    plan = {"total": len(parts) + 1 if len(parts) > 1 else 1, "done": 0, "floor": 0.0}

    def emit(msg, frac, draft=None):
        # 要点需要额外合并时总步数会变多，按步数算的进度可能回退：只进不退
        plan["floor"] = max(plan["floor"], frac)
        report(msg, plan["floor"], draft)

    def step(label: str, prompt: str, expect: int, draft: bool = False) -> str:
        i, total = plan["done"], plan["total"]
        head = f"第 {i + 1}/{total} 步：{label}" if total > 1 else label
        # 模型先读完输入（量化大模型读一万多字可能要一两分钟）才开始输出
        emit(f"{head} · 读取原文中（{len(prompt)} 字）", i / total)

        def on(chars, out, thinking):
            what = f"思考中（{thinking} 字）" if thinking and not chars else f"已输出 {chars} 字"
            emit(f"{head} · {what}", (i + min(0.95, 1 - math.exp(-chars / expect))) / total, out if draft else None)

        result = llm.chat(SYSTEM, prompt, on_progress=on)
        plan["done"] += 1
        return result

    ex = examples_text()
    if len(parts) == 1:
        return one_paragraph(step("生成服务内容", FINAL_FROM_TEXT.format(name=name, format=FORMAT, examples=ex, text=text),
                                  _EXPECT_SUMMARY, True))
    notes = [f"【第 {i} 部分】\n" + step(f"分析第 {i}/{len(parts)} 部分",
                                        EXTRACT.format(name=name, i=i, n=len(parts), text=p), _EXPECT_NOTES)
             for i, p in enumerate(parts, 1)]
    merged = "\n\n".join(notes)
    for _ in range(3):  # 要点本身也放不下：分组合并压缩，直到放得下（最多 3 轮，模型压不下来就截断）
        if len(merged) <= size:
            break
        groups = split_text(merged, size)
        plan["total"] += len(groups)
        merged = "\n\n".join(step(f"合并要点 {j}/{len(groups)}", MERGE.format(name=name, text=g), _EXPECT_NOTES)
                               for j, g in enumerate(groups, 1))
    merged = merged[:size]
    return one_paragraph(step("生成服务内容", FINAL_FROM_NOTES.format(name=name, format=FORMAT, examples=ex, text=merged),
                              _EXPECT_SUMMARY, True))


def queue(conn, where: str, args=()) -> int:
    """把已解析完成的文档排进概述队列。"""
    return conn.execute(
        "UPDATE documents SET summary_status='queued', summary_message=NULL"
        f" WHERE status='done' AND ({where})", args).rowcount


class _Cancelled(Exception):
    """生成期间文档被删除或重新解析。"""


class Worker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="summary-worker")
        self.wake = threading.Event()

    def run(self):
        with db.session() as conn:
            conn.execute("UPDATE documents SET summary_status='queued' WHERE summary_status='running'")
        while True:
            try:
                self._step()
            except Exception:
                log.exception("概述线程出错，10 秒后重试")
                time.sleep(10)

    def _step(self):
        row = None
        if llm.enabled():
            with db.session() as conn:
                row = conn.execute("SELECT id, filename FROM documents WHERE status='done' AND summary_status='queued'"
                                   " ORDER BY id LIMIT 1").fetchone()
        if not row:
            self.wake.wait(10)
            self.wake.clear()
            return
        doc_id, name = row

        def set_(**fields):
            cols = ", ".join(f"{k}=?" for k in fields)
            with db.session() as conn:
                # 只在仍是 running 时写：期间文档被删除或重新解析（又排回 queued），这次结果作废
                return conn.execute(f"UPDATE documents SET {cols} WHERE id=? AND summary_status='running'",
                                    (*fields.values(), doc_id)).rowcount > 0

        with db.session() as conn:
            conn.execute("UPDATE documents SET summary_status='running', summary_message='准备中', summary_progress=0,"
                         " summary_draft=NULL, summary_started_at=? WHERE id=?", (time.strftime("%Y-%m-%d %H:%M:%S"), doc_id))
        last = {"t": 0.0, "head": None}

        def report(msg, frac, draft=None):
            # 流式输出每几个字就回调一次，写库限流到每秒一次；换步骤时立即写
            head, now = msg.split(" · ")[0], time.monotonic()
            if head == last["head"] and now - last["t"] < 1:
                return
            last.update(t=now, head=head)
            if not set_(summary_message=msg, summary_progress=round(frac, 3), summary_draft=draft):
                raise _Cancelled

        t0 = time.time()
        try:
            text = summarize(name, document_text(doc_id), report)
        except _Cancelled:
            return
        except Exception as e:
            log.warning("生成概述失败：%s：%s", name, e)
            set_(summary_status="failed", summary_message=str(e)[:500], summary_draft=None)
            return
        if set_(summary=text, summary_status="done", summary_message=None, summary_progress=1, summary_draft=None,
                summary_at=time.strftime("%Y-%m-%d %H:%M:%S")):
            log.info("%s：概述已生成，%.0fs", name, time.time() - t0)


worker = Worker()
