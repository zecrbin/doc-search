"""命令行工具。

批量导入：uv run python -m app.cli import <目录或文件>...   （只登记入库队列，解析由运行中的服务后台完成）
大模型自检：uv run python -m app.cli llm                   （Docker 里：docker compose exec doc-search python -m app.cli llm）
"""
import argparse
import sys
import time
from pathlib import Path

from . import config, db, ingest, llm


def check_llm():
    if not llm.enabled():
        print("没有配置大模型：请设置 DOCSEARCH_LLM_URL，例如 http://192.168.18.61:8001/v1")
        return 1
    print(f"接口地址：{llm.base_url()}")
    try:
        print(f"模型：{llm.model()}")
    except Exception as e:
        print(f"连接失败：{e}\n请检查地址和端口；大模型和本服务在同一台机器时不能写 127.0.0.1（容器里指容器自己）")
        return 1
    ctx = llm.context_tokens()
    print(f"上下文长度：{ctx or '接口未报告'} token")
    print(f"每段原文：{llm.chunk_chars()} 字{'（自动）' if config.LLM_CHUNK_CHARS <= 0 else '（DOCSEARCH_LLM_CHUNK_CHARS）'}，"
          f"每次最多输出 {llm.output_tokens()} token")
    print("试调用中…")
    t0 = time.time()
    try:
        reply = llm.chat("你是助手，用简体中文回答。", "用一句话说明你是什么模型。", max_tokens=200)
    except Exception as e:
        print(f"调用失败：{e}")
        return 1
    print(f"回复（{time.time() - t0:.1f} 秒）：{reply}")
    print("正常。")
    return 0


def main():
    ap = argparse.ArgumentParser(prog="app.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import", help="导入文件或目录（递归）")
    imp.add_argument("paths", nargs="+", type=Path)
    sub.add_parser("llm", help="检查大模型连接（文档概述用）")
    args = ap.parse_args()
    if args.cmd == "llm":
        sys.exit(check_llm())

    db.init()
    files = []
    for p in args.paths:
        if p.is_dir():
            files += [f for f in p.rglob("*") if f.suffix.lower() in config.ALLOWED_EXTS and not f.name.startswith("~$")]
        elif p.is_file():
            files.append(p)
    added = dup = 0
    for f in files:
        r = ingest.register_file(f, f.name)
        if r.get("duplicate"):
            dup += 1
        else:
            added += 1
        print(("跳过（重复）" if r.get("duplicate") else "已排队"), f)
    print(f"\n新增 {added} 份，重复 {dup} 份。请保持服务运行以完成解析。")


if __name__ == "__main__":
    main()
