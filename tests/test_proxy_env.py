"""服务器/Docker 配了代理环境变量（例如 ALL_PROXY=socks5://...）时，服务照常启动，内网调用不走代理。"""
import importlib

from fastapi.testclient import TestClient


def test_socks_proxy_env_does_not_break_startup(monkeypatch):
    for k in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "HTTP_PROXY"):
        monkeypatch.setenv(k, "socks5://127.0.0.1:1")
    from app import config, embedder, main
    monkeypatch.setattr(config, "MINERU_URL", "http://127.0.0.1:1")  # 立即连不上，不用等超时
    importlib.reload(embedder)  # 模块加载时就会创建 httpx 客户端，原来这里直接 ImportError
    try:
        r = TestClient(main.app).get("/api/health").json()
        assert r["mineru"] is False  # 连不上 MinerU 只是显示红点，不会因为代理报错
    finally:
        monkeypatch.undo()
        importlib.reload(embedder)
