"""文档服务内容概述：解析完成后由大模型生成。

长文档一次放不进模型上下文：先按 LLM_CHUNK_CHARS 分段逐段提取要点（保留数值），要点太多再合并压缩，
最后按固定格式写成概述。单独的后台线程处理，不耽误文档解析和检索。
"""
import logging
import math
import threading
import time

from . import db, llm

log = logging.getLogger(__name__)

SYSTEM = ("你是招投标与项目文件分析助手。只根据用户提供的原文作答，不编造原文没有的内容；"
          "涉及指标、参数、金额、期限、数量时照抄原文数值和单位。用简体中文回答。")

FORMAT = """请按以下格式输出（Markdown），不要输出其他内容：

## 概述
用 2～4 句话说明：这是什么文件、哪个项目、为谁提供什么服务或产品、项目编号和预算（原文有才写）。

## 服务内容
- 逐条列出要提供的服务、产品、系统或功能模块

## 主要技术指标
- 逐条列出关键技术指标和参数，保留原文数值和单位；指标很多时按类别归纳，优先保留有具体数值的

## 服务与商务要求
- 工期/交付期、质保期、运维与售后、培训、驻场、验收、付款方式、资质要求等；原文没有的项写"文档未提及"
"""

EXTRACT = """下面是文件《{name}》的第 {i}/{n} 部分原文。请提取其中与项目有关的关键信息，用简洁的要点列出：
项目名称/编号/预算、服务或采购内容、技术指标与参数（保留具体数值和单位）、服务要求（工期、质保、运维、培训、驻场、验收等）、商务条款（付款、资质等）。
这一部分没有的信息不要写，也不要编造。

原文：
{text}"""

MERGE = """下面是从文件《{name}》各部分提取的要点。请合并去重、归类整理，保留所有具体数值和单位，不要丢掉技术指标，也不要编造：

{text}"""

FINAL_FROM_TEXT = """下面是文件《{name}》的原文。请根据原文写一份这份文件的服务内容概述。

{format}
原文：
{text}"""

FINAL_FROM_NOTES = """下面是从文件《{name}》各部分提取的要点。请根据这些要点写一份这份文件的服务内容概述。

{format}
要点：
{text}"""


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


# 估算步内进度：输出越多越接近完成，按 1 - e^(-字数/常数) 增长（400 字的要点约 55%，800 字的概述约 70%），
# 输出长短不一也不会卡住或提前到 100%
_EXPECT_NOTES, _EXPECT_SUMMARY = 500, 650


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

    if len(parts) == 1:
        return step("生成概述", FINAL_FROM_TEXT.format(name=name, format=FORMAT, text=text), _EXPECT_SUMMARY, True)
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
    return step("生成概述", FINAL_FROM_NOTES.format(name=name, format=FORMAT, text=merged), _EXPECT_SUMMARY, True)


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
