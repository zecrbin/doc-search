# 文档检索

上传 PDF / Word，按原文精确检索，点击结果在原文中高亮关键字。

## 启动

```bash
uv run python -m app.main
```

或双击 `启动.bat`，然后打开 http://localhost:18090 。局域网内其他人可通过 `http://本机IP:18090` 访问。

页面顶部两个标签：

- **检索**：在全部文件的正文和文件名里逐字查找（不区分大小写）。结果按文件分组，每个文件显示命中几段、几处、每处在第几页。结果完整统计、不设上限：每个文件先列 5 段，点"展开"加载其余全部段落。
  - 多个词用空格分隔：每个词都要出现在同一段里，或者出现在文件名里（如"扫描件 质保期"）。
  - 点击文件或其中一段，右侧打开原文，高亮这个文件里的所有命中；"上一处 / 下一处"（F3 / Shift+F3）逐个跳转，左侧同步标出当前段落。
  - 扫描件按 MinerU OCR 的行坐标定位关键字（扫描件里的表格只能框出整张表）。
  - 搜索框右侧 × 或 Esc 清除当前搜索；点搜索框显示搜索历史（存在各自浏览器里，最多 30 条），可用方向键选择。
- **文件库**：上传文件（点击或拖拽），查看每个文件的解析进度、成功/失败、失败原因、上传/完成时间和耗时。分页显示，可按状态和文件名筛选；勾选后批量删除或重新解析，"选中全部"可一次选中筛选结果里的所有文件（跨页）。

批量导入整个目录（服务需在运行，后台会依次解析）：

```bash
uv run python -m app.cli import D:\标书
```

## 服务器部署（Docker）

镜像里带了 LibreOffice 和中文字体（Linux 上没有 Word，doc/docx/rtf 用 LibreOffice 转 PDF）。

```bash
# 1. 构建镜像（在项目目录）；国内网络加上镜像源参数
docker compose build
#   或：docker build -t doc-search:latest \
#         --build-arg APT_MIRROR=https://mirrors.aliyun.com \
#         --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple .

# 2. 按需修改 docker-compose.yml 里的 MinerU 地址，然后启动
docker compose up -d

# 3. 浏览器打开 http://服务器IP:18090
docker compose logs -f        # 看日志
```

- **数据**都在 `./data`（挂载到容器的 `/data`），删掉/升级容器不影响；备份就拷这个目录。
- **从 Windows 迁移**：停掉 Windows 上的服务，把整个 `data` 目录拷到服务器项目目录下的 `data`，再 `docker compose up -d` 即可，不用重新解析。
- **服务器不能上网**：在能上网的机器上构建，`docker save doc-search:latest | gzip > doc-search.tar.gz`，拷到服务器 `docker load < doc-search.tar.gz`，再 `docker compose up -d`（compose 文件和 `data` 目录一起拷过去）。
- **升级**：拉取新代码后 `docker compose build && docker compose up -d`。
- **Word 版式**：LibreOffice 排版和 Word 略有差别，个别文档的页码可能和 Word 里看到的差一两页。把 Windows 的中文字体（`C:\Windows\Fonts` 里的宋体、黑体、仿宋等）放到 `./fonts`，并打开 docker-compose.yml 里 fonts 那行挂载，能明显更接近 Word；之后对 Word 文档"重新解析"。
- 只能跑**单进程**（不要开多个 uvicorn worker、也不要多个容器共用一个 `data`）：解析队列由进程内的后台线程处理。
- 不用 Docker 也可以：装 `uv`、`libreoffice-writer`、中文字体（如 `fonts-noto-cjk`）后 `uv run python -m app.main`。需要 SQLite 3.43 以上，`uv` 自带的 Python 满足；Debian 12 / Ubuntu 22.04 等系统自带的 Python 不满足。

## 处理流程

```
上传 → doc/docx 用本机 Word 转 PDF（没有 Word 时用 LibreOffice）
     → 逐页判断：有文字层的页 PyMuPDF 本地提取；扫描页/乱码页送 MinerU OCR
     → 去页眉页脚、识别标题层级、按结构切块（表格单独成块）
     → SQLite：片段原文 + 页码和坐标
```

检索时直接在片段原文里逐字匹配，全部命中都统计；片段数很多时（30 万段）每次约 1 秒，查"的"这种几乎每段都有的字约 3 秒。点击结果时在 PDF 上逐字取坐标定位关键字（跨行也能找到）；扫描页没有文字层，用入库时 MinerU 返回的行坐标（`data/parsed/<id>.ocr.json`），行内按字宽估算每个字的位置。

## 配置

环境变量（前缀 `DOCSEARCH_`），默认值见 `app/config.py`：

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATA_DIR` | `./data` | 数据库和原文件目录，备份拷走这个目录即可 |
| `PORT` | `18090` | 服务端口 |
| `MINERU_URL` | `http://192.168.18.61:8000` | MinerU 服务 |
| `SEMANTIC` | `0` | `1` 开启语义检索（接口里的 hybrid / semantic 模式），入库时会调用 Embedding 服务 |
| `EMBED_URL` | `http://192.168.18.61:18084` | Embedding / Rerank 服务（仅 `SEMANTIC=1` 时使用） |
| `EMBED_QUERY_TIMEOUT` | `5` | 检索时查询向量化的超时（秒），超时后综合模式只用关键字结果 |
| `PARSE_MODE` | `auto` | `mineru` 表示所有页都走 MinerU（慢，复杂版面更准） |
| `CHUNK_TARGET` / `CHUNK_MAX` | `500` / `800` | 切块目标/最大字数 |

升级后旧文档里横向（旋转）页面的高亮可能错位、扫描件只能框出整段，在文件库对这些文档点"重新解析"即可（扫描件会重新走一遍 OCR）。

开启 `SEMANTIC` 或更换 Embedding 模型后，需要"全部重新解析"生成向量（向量维度见 `EMBED_DIM`）。

## 测试

```bash
uv run pytest
```
