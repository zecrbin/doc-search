@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 文档检索服务启动中，浏览器访问 http://localhost:18090
start "" http://localhost:18090
uv run python -m app.main
pause
