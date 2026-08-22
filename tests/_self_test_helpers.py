"""自测夹具：从 scripts/self_test.py 迁移过来的公用辅助。

这些辅助只服务测试，不进生产安装包。scripts/self_test.py 里同名的
_font_path / _make_pdf 仍然保留，因为 self_test.py 的端到端 run() 还要用，
而安装包里没有 tests/ 目录，不能反过来依赖它。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import hashlib  # noqa: E402
import re  # noqa: E402
from pathlib import Path  # noqa: E402

from _common import (  # noqa: E402
    import_fitz,
    load_json,
)

_ZH_STUB_WORDS = (  # noqa: SIM905
    "研究 方法 结果 讨论 模型 样本 证据 方差 队列 基线 估计 显著 区间 "
    "回归 构念 效度 信度 参与 流程 测量 产出 处理 对照 数据 分析 指标"
).split()


_ZH_STUB_TOKEN_RE = re.compile(
    r"https?://\S+|\b10\.\d{4,9}/\S+|[A-Za-z][A-Za-z'-]*|\S+|\s+"
)


def _font_path() -> Path:
    candidates = [
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/System/Library/Fonts/Supplemental/Times New Roman.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("自测需要一份可嵌入的拉丁字体")


def _make_pdf(
    path: Path,
    paragraphs: list[list[str]],
    fontsize: float = 9.2,
    leading: float = 14.2,
) -> None:
    fitz = import_fitz()
    font_path = _font_path()
    document = fitz.open()
    for page_lines in paragraphs:
        page = document.new_page(width=595.276, height=841.89)
        page.insert_font(fontname="BodyFont", fontfile=str(font_path))
        y = 80.0
        for line in page_lines:
            page.insert_text(
                (72, y),
                line,
                fontname="BodyFont",
                fontfile=str(font_path),
                fontsize=fontsize,
            )
            y += leading
    document.save(path, garbage=4, deflate=True)
    document.close()


def _zh_stub(source: str) -> str:
    """自测夹具用的确定性伪译文。

    它不是模型输出，只保证两件事：语言是中文，锚点原样保留。
    夹具本身如果写成英文，就会被译文真实性检查拦下——那是检查在生效，
    不是检查过严。
    """

    out: list[str] = []
    pending_space = False
    previous_kept = False
    for match in _ZH_STUB_TOKEN_RE.finditer(source or ""):
        token = match.group(0)
        if token.isspace():
            pending_space = True
            continue
        if token.startswith(("http://", "https://")) or token.startswith("10."):
            kept, rendered = True, token
        elif re.fullmatch(r"[A-Za-z][A-Za-z'-]*", token) and not (
            token.isupper() and len(token) >= 2
        ):
            kept = False
            digest = hashlib.sha256(token.lower().encode("utf-8")).digest()
            rendered = _ZH_STUB_WORDS[digest[0] % len(_ZH_STUB_WORDS)]
            if len(token) > 4:
                rendered += _ZH_STUB_WORDS[digest[1] % len(_ZH_STUB_WORDS)]
        else:
            kept, rendered = True, token
        if pending_space and (kept or previous_kept):
            out.append(" ")
        out.append(rendered)
        pending_space = False
        previous_kept = kept
    return "".join(out).strip() or "（无内容）"


def _batch_unit_ids(job_dir: Path, entry: dict) -> list[str]:
    batch = load_json(job_dir / entry["file"])
    return [str(unit["id"]) for unit in batch["units"]]
