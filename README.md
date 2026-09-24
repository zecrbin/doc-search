# 文档检索

上传 PDF / Word，按关键字或自然语言检索，点击结果在原文中定位高亮。

## 启动

```bash
uv run python -m app.main
```

或双击 `启动.bat`，然后打开 http://localhost:18090 。局域网内其他人可通过 `http://本机IP:18090` 访问。

批量导入整个目录（服务需在运行，后台会依次解析）：

```bash
uv run python -m app.cli import D:\标书
```

## 处理流程

```
上传 → doc/docx 用本机 Word 转 PDF（没有 Word 时用 LibreOffice）
     → 逐页判断：有文字层的页 PyMuPDF 本地提取；扫描页/乱码页送 MinerU OCR
     → 去页眉页脚、识别标题层级、按结构切块（表格单独成块）
     → Embedding（服务器 Qwen3-Embedding-0.6B）
     → SQLite：FTS5（jieba 分词）+ sqlite-vec 向量 + 每块的页码和坐标
```

检索模式：

- **综合**：BM25 + 向量 RRF 融合，原文逐字命中查询词的额外加分；重排服务可用时自动重排；Embedding 服务不可用时退回纯关键字结果
- **精确**：原文必须逐字包含每个查询词（空格分隔多个词），适合查编号、名称、金额；按出现次数排序。直接扫描原文而不走分词索引，片段数很多时（几十万）每次约 1 秒
- **语义**：只按向量相似度

标书之间完全相同的段落会折叠成一条，显示"相同段落还出现在 N 处"。

## 配置

环境变量（前缀 `DOCSEARCH_`），默认值见 `app/config.py`：

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATA_DIR` | `./data` | 数据库和原文件目录，备份拷走这个目录即可 |
| `PORT` | `18090` | 服务端口 |
| `MINERU_URL` | `http://192.168.18.61:8000` | MinerU 服务 |
| `EMBED_URL` | `http://192.168.18.61:18084` | Embedding / Rerank 服务 |
| `EMBED_QUERY_TIMEOUT` | `5` | 检索时查询向量化的超时（秒），超时后综合模式只用关键字结果 |
| `PARSE_MODE` | `auto` | `mineru` 表示所有页都走 MinerU（慢，复杂版面更准） |
| `CHUNK_TARGET` / `CHUNK_MAX` | `500` / `800` | 切块目标/最大字数 |

行业术语、公司名可写入 `data/userdict.txt`（jieba 词典格式，每行一个词），重启后对新入库文档生效，旧文档需"重新解析"。

更换 Embedding 模型后，需要对所有文档"重新解析"（向量维度见 `EMBED_DIM`）。

## 测试

```bash
uv run pytest
```
