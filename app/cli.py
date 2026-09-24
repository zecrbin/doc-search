"""批量导入目录：uv run python -m app.cli import <目录或文件>...

只负责登记入库队列，实际解析由运行中的服务后台完成。
"""
import argparse
from pathlib import Path

from . import config, db, ingest


def main():
    ap = argparse.ArgumentParser(prog="app.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import", help="导入文件或目录（递归）")
    imp.add_argument("paths", nargs="+", type=Path)
    args = ap.parse_args()

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
