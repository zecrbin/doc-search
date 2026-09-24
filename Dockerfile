# 文档检索服务镜像：Python 3.12（Debian 13，自带 SQLite 3.46；全文索引需要 3.43+）+ LibreOffice（Linux 上没有 Word，doc/docx/rtf 用它转 PDF）+ 中文字体
FROM python:3.12-slim-trixie

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# 国内服务器可换 Debian 镜像源：--build-arg APT_MIRROR=https://mirrors.aliyun.com
ARG APT_MIRROR=
# tzdata：上传/完成时间按北京时间记录；fonts-noto-cjk：Word 转 PDF 时中文不会变成方框
RUN if [ -n "$APT_MIRROR" ]; then sed -i "s|http://deb.debian.org|$APT_MIRROR|g" /etc/apt/sources.list.d/debian.sources; fi \
 && apt-get update \
 && apt-get install -y --no-install-recommends libreoffice-writer-nogui fonts-noto-cjk fontconfig tzdata \
 && rm -rf /var/lib/apt/lists/*

# uv 用 pip 从 PyPI 装（国内访问 ghcr.io 常失败）；需要镜像源时构建加 --build-arg PIP_INDEX_URL=...
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" uv==0.8.17

WORKDIR /app
# 先只装依赖，改代码后重新构建不用重装
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev
COPY app ./app
COPY web ./web

ENV PATH="/app/.venv/bin:$PATH" \
    DOCSEARCH_DATA_DIR=/data \
    DOCSEARCH_HOST=0.0.0.0 \
    DOCSEARCH_PORT=18090
VOLUME /data
EXPOSE 18090

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18090/api/documents?page_size=1', timeout=4)"

# 只能单进程：解析队列由进程内的后台线程处理，多个 worker 会重复处理同一个文件
CMD ["python", "-m", "app.main"]
