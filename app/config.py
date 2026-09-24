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
# 检索时查询向量化的超时（秒），入库时的批量向量化不受此限制
EMBED_QUERY_TIMEOUT = float(_env("EMBED_QUERY_TIMEOUT", 5))
RERANK_ENABLED = _env("RERANK", "1") == "1"
# 语义检索（综合/语义模式）。默认关闭：只用精确匹配，入库不依赖 Embedding 服务
SEMANTIC = _env("SEMANTIC", "0") == "1"

# 大模型（OpenAI 兼容接口：vLLM / SGLang / LMDeploy / Ollama / llama.cpp 等），用于生成文档概述；不配置则不生成
# 地址只写到端口时自动补 /v1，例如 http://192.168.18.61:8000 → http://192.168.18.61:8000/v1
LLM_URL = _env("LLM_URL", "").rstrip("/")
LLM_MODEL = _env("LLM_MODEL", "")  # 留空则用接口 /models 返回的第一个模型
LLM_API_KEY = _env("LLM_API_KEY", "")
LLM_TIMEOUT = float(_env("LLM_TIMEOUT", 600))  # 单次调用超时（秒），量化模型在长文本上可能要几分钟
LLM_CHUNK_CHARS = int(_env("LLM_CHUNK_CHARS", 12000))  # 每次送给大模型的原文字数，按模型上下文长度调
LLM_MAX_TOKENS = int(_env("LLM_MAX_TOKENS", 2048))  # 每次最多生成多少 token
LLM_NO_THINK = _env("LLM_NO_THINK", "1") == "1"  # Qwen3 等思考模型关闭思考，快很多

CHUNK_TARGET = int(_env("CHUNK_TARGET", 500))
CHUNK_MAX = int(_env("CHUNK_MAX", 800))

ALLOWED_EXTS = {".pdf", ".doc", ".docx", ".rtf"}
