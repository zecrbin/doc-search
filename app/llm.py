"""大模型客户端：OpenAI 兼容的 /chat/completions 接口。"""
import logging
import re
import time
from urllib.parse import urlparse

import httpx

from . import config

log = logging.getLogger(__name__)

_client: httpx.Client | None = None
_model: str | None = None


def enabled() -> bool:
    return bool(config.LLM_URL)


def base_url() -> str:
    url = config.LLM_URL
    return url + "/v1" if urlparse(url).path in ("", "/") else url


def _http() -> httpx.Client:
    global _client
    if _client is None:
        headers = {"Authorization": f"Bearer {config.LLM_API_KEY}"} if config.LLM_API_KEY else {}
        # 内网服务，不读代理环境变量（见 embedder）
        _client = httpx.Client(base_url=base_url(), timeout=config.LLM_TIMEOUT, headers=headers, trust_env=False)
    return _client


def model() -> str:
    global _model
    if config.LLM_MODEL:
        return config.LLM_MODEL
    if _model is None:
        r = _http().get("/models", timeout=10)
        r.raise_for_status()
        models = r.json().get("data") or []
        if not models:
            raise RuntimeError("大模型接口 /models 没有返回模型，请配置 DOCSEARCH_LLM_MODEL")
        _model = models[0]["id"]
    return _model


_THINK = re.compile(r"<think>.*?</think>", re.S)


def clean(text: str) -> str:
    """去掉思考过程和代码块包裹。"""
    text = _THINK.sub("", text or "")
    if "</think>" in text:  # 有的模板把 <think> 放在提示里，输出只有结尾
        text = text.rsplit("</think>", 1)[1]
    text = text.strip()
    text = re.sub(r"^```(?:markdown|md)?\s*\n", "", text)
    text = re.sub(r"\n```\s*$", "", text)
    return text.strip()


def chat(system: str, user: str, max_tokens: int | None = None, retries: int = 2) -> str:
    payload = {
        "model": model(),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.2,
        "max_tokens": max_tokens or config.LLM_MAX_TOKENS,
        "stream": False,
    }
    if config.LLM_NO_THINK:
        # vLLM / SGLang / llama.cpp 支持；不支持的服务会忽略，输出里的思考过程再由 clean() 去掉
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    for attempt in range(retries + 1):
        try:
            r = _http().post("/chat/completions", json=payload)
            r.raise_for_status()
            choice = r.json()["choices"][0]
            text = clean(choice["message"].get("content") or "")
            if not text:
                raise RuntimeError("大模型返回了空内容")
            if choice.get("finish_reason") == "length":
                log.warning("大模型输出被截断（max_tokens=%s）", payload["max_tokens"])
            return text
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            status = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else None
            if status is not None and status < 500 or attempt == retries:
                detail = e.response.text[:300] if status is not None else str(e)
                raise RuntimeError(f"大模型调用失败：{detail}") from e
            time.sleep(3 * (attempt + 1))
    raise AssertionError("unreachable")


def health() -> dict:
    if not enabled():
        return {"enabled": False, "up": False, "model": None}
    try:
        _http().get("/models", timeout=5).raise_for_status()
        return {"enabled": True, "up": True, "model": model()}
    except Exception:
        return {"enabled": True, "up": False, "model": config.LLM_MODEL or None}
