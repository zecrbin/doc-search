"""配置：全部可用环境变量覆盖（前缀 DOCSEARCH_）。"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"


def _env(name: str, default) -> str:
    return os.environ.get(f"DOCSEARCH_{name}", str(default))


DATA_DIR = Path(_env("DATA_DIR", BASE_DIR / "data")).resolve()
FILES_DIR = DATA_DIR / "files"
PARSED_DIR = DATA_DIR / "parsed"
DB_PATH = DATA_DIR / "docsearch.db"
# jieba 自定义词典（每行：词 [词频] [词性]），放行业术语、公司名可提升关键字检索
USER_DICT = DATA_DIR / "userdict.txt"

HOST = _env("HOST", "0.0.0.0")
PORT = int(_env("PORT", 18090))

MINERU_URL = _env("MINERU_URL", "http://192.168.18.61:8000").rstrip("/")
# 每次送给 MinerU 的页数，越小进度越细，越大请求次数越少
MINERU_BATCH_PAGES = int(_env("MINERU_BATCH_PAGES", 10))
MINERU_TIMEOUT = float(_env("MINERU_TIMEOUT", 1800))
# auto：有文字层的页本地提取，扫描页走 MinerU OCR；mineru：所有页都交给 MinerU（慢，版面更准）
PARSE_MODE = _env("PARSE_MODE", "auto")

EMBED_URL = _env("EMBED_URL", "http://192.168.18.61:18084").rstrip("/")
EMBED_BATCH = int(_env("EMBED_BATCH", 16))
EMBED_DIM = int(_env("EMBED_DIM", 1024))
RERANK_ENABLED = _env("RERANK", "1") == "1"

CHUNK_TARGET = int(_env("CHUNK_TARGET", 500))
CHUNK_MAX = int(_env("CHUNK_MAX", 800))

ALLOWED_EXTS = {".pdf", ".doc", ".docx", ".rtf"}
