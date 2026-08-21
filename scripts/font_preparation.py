"""字体准备：在生成 PDF 之前把目标语言字体解析成具体文件。

字体解析原来住在 `build_candidate` 里，只有真正开始排版时才会执行。
但输入就绪检查跑在排版之前，全新作业的 `selected_fonts` 是空的，于是
每一次都被 `SELECTED_FONTS_MISSING` 拦下，自动解析永远没机会运行——
用户必须手工编辑 job.json 才能进入统一入口。

现在解析是独立一步：初始化时做一次，统一入口在检查之前再确认一次。
解析结果连同文件哈希写进 job.json，字体文件被换掉或删掉时哈希对不上，
会自动重新解析。
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from _common import SkillError, load_json, sha256_file, write_json

FONT_DIRS = (
    Path("/System/Library/Fonts"),
    Path("/System/Library/Fonts/Supplemental"),
    Path("/Library/Fonts"),
    Path.home() / "Library/Fonts",
)

def _normalized_font_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


FONT_STYLE_SUFFIXES = {
    "",
    "regular",
    "book",
    "roman",
    "medium",
    "light",
    "bold",
    "semibold",
    "demibold",
    "heavy",
    "italic",
    "oblique",
    "bolditalic",
    "boldoblique",
    "semibolditalic",
}


def _font_request_match_score(requested: str, stem: str) -> int:
    requested_token = _normalized_font_token(requested)
    stem_token = _normalized_font_token(stem)
    if not requested_token or not stem_token.startswith(requested_token):
        return 0
    suffix = stem_token[len(requested_token) :]
    if suffix not in FONT_STYLE_SUFFIXES:
        return 0
    if not suffix:
        return 100
    if suffix in {"regular", "book", "roman", "medium"}:
        return 90
    return 70


def _font_family_token(path: Path) -> str:
    token = _normalized_font_token(path.stem)
    for suffix in sorted(
        FONT_STYLE_SUFFIXES - {""},
        key=len,
        reverse=True,
    ):
        if token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def _font_files() -> list[Path]:
    files: list[Path] = []
    for directory in FONT_DIRS:
        if not directory.is_dir():
            continue
        files.extend(
            path
            for path in directory.rglob("*")
            if path.suffix.casefold() in {".ttf", ".ttc", ".otf"}
        )
    return files


def _resolve_fonts(job: dict[str, Any]) -> tuple[Path, Path]:
    selected = job.get("quality", {}).get("selected_fonts", [])
    explicit = [
        Path(value).expanduser().resolve()
        for value in selected
        if isinstance(value, str) and Path(value).expanduser().is_file()
    ]
    if explicit:
        return explicit[0], explicit[1] if len(explicit) > 1 else explicit[0]

    requested = [
        str(value)
        for value in selected
        if isinstance(value, str) and value.strip()
    ]
    requested.extend(
        str(value)
        for value in job.get("quality", {}).get("font_candidates", [])
        if isinstance(value, str) and value.strip()
    )
    aliases = {
        "microsoftyahei": ("msyh", "yahei"),
        "sourcehansanssc": ("sourcehansans", "sourcesans"),
        "notosanscjksc": ("notosanscjk",),
        "pingfangsc": ("pingfang",),
        "stheiti": ("stheiti",),
        "arialunicodems": ("arialunicode",),
    }
    available = _font_files()
    normalized = [
        (_normalized_font_token(path.stem), path)
        for path in available
    ]
    matches: list[Path] = []
    for name in requested:
        token = _normalized_font_token(name)
        candidates = (token,) + aliases.get(token, ())
        scored = [
            (
                _font_request_match_score(candidate, stem)
                - alias_index,
                path,
            )
            for stem, path in normalized
            for alias_index, candidate in enumerate(candidates)
            if _font_request_match_score(candidate, stem)
        ]
        match = (
            max(scored, key=lambda item: (item[0], str(item[1])))[1]
            if scored
            else None
        )
        if match and match not in matches:
            matches.append(match)
    if not matches:
        fallback = Path("/System/Library/Fonts/STHeiti Medium.ttc")
        if fallback.is_file():
            matches.append(fallback)
    if not matches:
        raise SkillError(
            "无法解析目标语言字体。请在 job.json.quality.selected_fonts "
            "中写入可读取的字体文件路径。"
        )
    regular = matches[0]
    family = _font_family_token(regular)
    bold_names = {
        f"{family}bold",
        f"{family}semibold",
        f"{family}demibold",
        f"{family}heavy",
    }
    bold = next(
        (
            path
            for path in matches[1:] + available
            if _normalized_font_token(path.stem) in bold_names
        ),
        regular,
    )
    return regular, bold


def _resolve_reference_font(regular_font: Path) -> Path:
    candidates = (
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
    )
    return next(
        (path.resolve() for path in candidates if path.is_file()),
        regular_font,
    )




def resolve_job_fonts(job: dict[str, Any]) -> list[Path]:
    """按作业的字体候选解析出正文、粗体和题录三个实际字体文件。"""

    regular, bold = _resolve_fonts(job)
    return [regular, bold, _resolve_reference_font(regular)]


def font_evidence(paths: list[Path]) -> list[dict[str, str]]:
    return [
        {"path": str(path), "sha256": sha256_file(path, use_cache=False)}
        for path in paths
    ]


def fonts_are_current(job: dict[str, Any]) -> bool:
    """已冻结的字体是否仍然可用：文件在，且内容哈希没变。"""

    quality = job.get("quality", {})
    selected = quality.get("selected_fonts")
    evidence = quality.get("selected_font_evidence")
    if not isinstance(selected, list) or not selected:
        return False
    if not isinstance(evidence, list) or len(evidence) != len(selected):
        return False
    for value, record in zip(selected, evidence, strict=True):
        if not isinstance(value, str) or not isinstance(record, dict):
            return False
        path = Path(value).expanduser()
        if not path.is_file():
            return False
        if str(record.get("path") or "") != str(value):
            return False
        if record.get("sha256") != sha256_file(path, use_cache=False):
            return False
    return True


def prepare_job_fonts(
    job_dir: Path,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """解析并冻结字体，把绝对路径与文件哈希写回 job.json。

    已经冻结且仍然有效时不重复解析；文件变了就重新选。
    """

    job_dir = Path(job_dir).resolve()
    job_path = job_dir / "job.json"
    job = load_json(job_path)
    if not force and fonts_are_current(job):
        quality = job["quality"]
        return {
            "status": "unchanged",
            "selected_fonts": list(quality["selected_fonts"]),
            "selected_font_evidence": list(quality["selected_font_evidence"]),
        }

    paths = resolve_job_fonts(job)
    evidence = font_evidence(paths)
    quality = job.setdefault("quality", {})
    previous = quality.get("selected_fonts")
    quality["selected_fonts"] = [str(path) for path in paths]
    quality["selected_font_evidence"] = evidence
    write_json(job_path, job)
    return {
        "status": "reselected" if previous else "selected",
        "selected_fonts": list(quality["selected_fonts"]),
        "selected_font_evidence": evidence,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="解析并冻结作业使用的实际字体文件"
    )
    parser.add_argument("job_dir", type=Path)
    parser.add_argument(
        "--force",
        action="store_true",
        help="忽略现有冻结结果，重新解析",
    )
    args = parser.parse_args()
    try:
        report = prepare_job_fonts(args.job_dir, force=args.force)
    except SkillError as exc:
        print(f"错误: {exc}")
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
