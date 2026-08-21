"""确定性的假翻译器，只用于审查复现与测试。

它不是模型。它把英文单词逐个换成固定的中文伪词，同时原样保留数字、
引文编号、统计量、缩写、DOI 和 URL。目的是造出“语言正确、锚点齐全”的
译文，用来验证流程本身，而不是验证翻译质量。
"""

from __future__ import annotations

import hashlib
import re

SYLLABLES = (
    "研究 方法 结果 讨论 模型 样本 证据 方差 队列 基线 估计 显著 区间 "
    "回归 构念 效度 信度 参与 流程 测量 产出 处理 对照 数据 分析 指标 "
    "假设 差异 相关 影响 机制 结构 层级 阈值 覆盖 校验 记录 报告"
).split()

TOKEN_RE = re.compile(r"https?://\S+|\b10\.\d{4,9}/\S+|[A-Za-z][A-Za-z'-]*|\S+|\s+")


def _is_acronym(token: str) -> bool:
    return token.isupper() and len(token) >= 2


def _word(token: str) -> str:
    digest = hashlib.sha256(token.lower().encode("utf-8")).digest()
    first = SYLLABLES[digest[0] % len(SYLLABLES)]
    if len(token) <= 4:
        return first
    return first + SYLLABLES[digest[1] % len(SYLLABLES)]


def fake_translate(source: str) -> str:
    out: list[str] = []
    pending_space = False
    previous_kept = False
    for match in TOKEN_RE.finditer(source or ""):
        token = match.group(0)
        if token.isspace():
            pending_space = True
            continue
        if token.startswith(("http://", "https://")) or token.startswith("10."):
            kept = True
            rendered = token
        elif re.fullmatch(r"[A-Za-z][A-Za-z'-]*", token) and not _is_acronym(token):
            kept = False
            rendered = _word(token)
        else:
            kept = True
            rendered = token
        # 原样保留的片段两侧必须保住空格，否则 "95% CI" 会被压成 "95%CI"，
        # 统计量锚点就检测不出来了。
        if pending_space and (kept or previous_kept):
            out.append(" ")
        out.append(rendered)
        pending_space = False
        previous_kept = kept
    return "".join(out).strip() or "（无内容）"
