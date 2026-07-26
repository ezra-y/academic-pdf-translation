from __future__ import annotations

import hashlib
from pathlib import Path


RENDERER_INPUTS = (
    "scripts/_common.py",
    "scripts/build_candidate.py",
    "scripts/candidate_page_map.py",
    "scripts/cjk_markup.py",
    "scripts/i18n.py",
    "scripts/reportlab_layout.py",
    "scripts/retained_source.py",
    "scripts/set_complex_payload.py",
    "scripts/typography_fit.py",
    "assets/language-profiles.json",
)


def renderer_build_id(skill_root: Path | None = None) -> str:
    root = (
        skill_root.resolve()
        if skill_root is not None
        else Path(__file__).resolve().parent.parent
    )
    digest = hashlib.sha256()
    for relative in RENDERER_INPUTS:
        path = root / relative
        if not path.is_file():
            raise FileNotFoundError(f"生成器身份输入不存在: {relative}")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
