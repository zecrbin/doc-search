"""中文分词：索引和查询用同一套切分，保证 FTS 能对上。"""
import hashlib
import logging
import re

import jieba

from . import config

jieba.setLogLevel(logging.WARNING)

_WORD = re.compile(r"\w", re.UNICODE)


def init():
    if config.USER_DICT.exists():
        jieba.load_userdict(str(config.USER_DICT))
    jieba.initialize()


def _cut(text: str) -> list[str]:
    return [t for t in jieba.cut_for_search(text.lower()) if _WORD.search(t)]


def index_tokens(text: str) -> str:
    return " ".join(_cut(text))


def query_tokens(query: str) -> list[str]:
    return list(dict.fromkeys(t.strip() for t in _cut(query) if t.strip()))


def fts_query(tokens: list[str], op: str) -> str:
    return f" {op} ".join('"' + t.replace('"', '""') + '"' for t in tokens)


def text_hash(text: str) -> str:
    return hashlib.md5(re.sub(r"\s+", "", text).encode("utf-8")).hexdigest()
