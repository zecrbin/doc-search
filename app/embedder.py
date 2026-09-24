"""Embedding / Rerank 客户端（服务器上的 info-embedding 服务）。"""
import logging
import time

import httpx

from . import config

log = logging.getLogger(__name__)

_client = httpx.Client(base_url=config.EMBED_URL, timeout=120)
_rerank_down_until = 0.0  # 重排服务不可用时暂停调用，避免每次检索都白等


def _post(path: str, payload: dict, retries: int = 3, timeout=httpx.USE_CLIENT_DEFAULT) -> dict:
    for attempt in range(retries):
        try:
            r = _client.post(path, json=payload, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            client_error = isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500
            if client_error or attempt == retries - 1:
                raise RuntimeError(f"Embedding 服务调用失败：{e}") from e
            time.sleep(2 ** attempt)


def embed_documents(texts: list[str]) -> list[list[float]]:
    out = []
    for i in range(0, len(texts), config.EMBED_BATCH):
        out.extend(_post("/internal/embeddings/documents", {"texts": texts[i:i + config.EMBED_BATCH]})["vectors"])
    return out


def embed_query(text: str) -> list[float]:
    # 检索时用户在等：不重试、短超时，服务挂了尽快失败让综合模式退回关键字检索
    return _post("/internal/embeddings/query", {"texts": [text]}, retries=1,
                 timeout=config.EMBED_QUERY_TIMEOUT)["vectors"][0]


def rerank(query: str, docs: list[tuple[str, str]]) -> dict[str, float] | None:
    """docs: [(id, text)]。服务不可用时返回 None，调用方按原排序处理。"""
    global _rerank_down_until
    if not config.RERANK_ENABLED or not docs or time.time() < _rerank_down_until:
        return None
    try:
        r = _client.post("/internal/rerank", timeout=httpx.Timeout(30, connect=config.EMBED_QUERY_TIMEOUT), json={
            "query": query[:4000],
            "documents": [{"documentId": i, "text": t[:12000]} for i, t in docs[:50]],
        })
        r.raise_for_status()
        return {x["documentId"]: x["score"] for x in r.json()["results"]}
    except Exception as e:
        log.info("重排不可用，5 分钟内跳过：%s", e)
        _rerank_down_until = time.time() + 300
        return None


def health() -> dict:
    try:
        d = _client.get("/health", timeout=5).json()
        return {"embedding": d.get("embedding") == "UP", "reranker": d.get("reranker") == "UP",
                "model": d.get("modelName")}
    except Exception:
        return {"embedding": False, "reranker": False, "model": None}
