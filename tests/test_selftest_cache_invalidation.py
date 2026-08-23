"""缓存失效口径：什么变了要重算，什么变了可以复用。

两支都在问同一个问题：缓存键包含哪些输入。审阅页缓存必须跟着可见
变化走，昂贵审计的缓存键则不能只因为字体换了就整体作废。
断言逐字保留原样，中文说明本身就是判据文档。

单独运行：
    python3 -m pytest -q tests/test_selftest_cache_invalidation.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

from _self_test_helpers import _font_path, _make_pdf  # noqa: E402

from _common import (  # noqa: E402
    import_fitz,
    load_json,
    write_json,
)
from init_job import initialize_job  # noqa: E402
from validate_job import validate_job  # noqa: E402


def test_review_page_cache_tracks_visual_change() -> None:
    """审查图单页缓存：指纹必须与实际像素判断一致，未变的页不得重画。"""

    from make_review_sheet import (
        _page_fingerprint,
        _prune_page_cache,
        _render_page,
        _render_page_cached,
    )

    fitz = import_fitz()
    with tempfile.TemporaryDirectory() as raw_root:
        root = Path(raw_root)
        first = root / "first.pdf"
        second = root / "second.pdf"
        _make_pdf(
            first,
            [
                ["Page one baseline content."],
                ["Page two baseline content."],
            ],
        )
        _make_pdf(
            second,
            [
                ["Page one baseline content."],
                ["Page two has been rewritten."],
            ],
        )

        document_a = fitz.open(first)
        document_b = fitz.open(second)
        fingerprints_a = [
            _page_fingerprint(page, 96) for page in document_a
        ]
        fingerprints_b = [
            _page_fingerprint(page, 96) for page in document_b
        ]
        changed_by_fingerprint = {
            index + 1
            for index, (left, right) in enumerate(
                zip(fingerprints_a, fingerprints_b)  # noqa: B905
            )
            if left != right
        }
        changed_by_pixels = set()
        for index in range(document_a.page_count):
            image_a = _render_page(document_a[index], 72)
            image_b = _render_page(document_b[index], 72)
            if image_a.tobytes() != image_b.tobytes():
                changed_by_pixels.add(index + 1)
            image_a.close()
            image_b.close()
        if changed_by_fingerprint != changed_by_pixels:
            raise AssertionError(
                "单页指纹与实际像素判断不一致，缓存会让审查图显示过期图像: "
                f"指纹 {sorted(changed_by_fingerprint)} vs "
                f"像素 {sorted(changed_by_pixels)}"
            )
        if changed_by_pixels != {2}:
            raise AssertionError("自测夹具应当只有第二页发生变化")

        if _page_fingerprint(document_a[0], 96) == _page_fingerprint(
            document_a[0],
            150,
        ):
            raise AssertionError("不同 DPI 必须是不同的缓存条目")

        cache_dir = root / "cache"
        cache_dir.mkdir()
        used: set[str] = set()
        image = _render_page_cached(document_a[0], 96, cache_dir, used)
        image.close()
        cached_files = list(cache_dir.glob("*.png"))
        if len(cached_files) != 1:
            raise AssertionError("单页渲染必须落盘为一个缓存条目")
        second_image = _render_page_cached(document_a[0], 96, cache_dir, used)
        second_image.close()
        if len(list(cache_dir.glob("*.png"))) != 1:
            raise AssertionError("同一页重复渲染不得产生第二个缓存条目")

        stale = cache_dir / "deadbeef.png"
        stale.write_bytes(b"stale")
        if _prune_page_cache(cache_dir, used) != 1:
            raise AssertionError("清理必须移除本轮未使用的缓存条目")
        if not cached_files[0].is_file():
            raise AssertionError("本轮用到的缓存条目不得被清理")

        document_a.close()
        document_b.close()


def test_expensive_audit_cache_key_ignores_only_fonts() -> None:
    """昂贵审计缓存的前提：换掉 selected_fonts 不改变阶段校验和完整性审计。

    `build_candidate` 会把冻结字体解析成实际字体文件后写回 job.json。缓存键
    刻意排除这个字段；一旦哪天这两项检查开始依赖它，本用例必须先失败。
    """

    from audit_translation_completeness import build_completeness_audit
    from pre_render_audit import _font_independent_key

    with tempfile.TemporaryDirectory() as raw_root:
        root = Path(raw_root)
        source = root / "font-key-source.pdf"
        _make_pdf(
            source,
            [
                [
                    "Adaptive cache invalidation across distributed sites.",
                    "We report a controlled evaluation with 42 participants.",
                ],
            ],
        )
        job_dir = root / "job"
        initialize_job(
            source,
            job_dir,
            "zh-Hans",
            "en",
            False,
            producer_id="self-test-producer",
        )
        job = load_json(job_dir / "job.json")
        job["quality"]["selected_fonts"] = [str(_font_path())]
        write_json(job_dir / "job.json", job)

        before_key = _font_independent_key(job_dir, load_json(job_dir / "job.json"))
        before_validation = validate_job(
            job_dir,
            "translated",
            status_override="translated",
        )
        before_audit = build_completeness_audit(
            job_dir,
            include_candidate=False,
        )

        job = load_json(job_dir / "job.json")
        job["quality"]["selected_fonts"] = [str(_font_path())] * 3
        write_json(job_dir / "job.json", job)

        after_key = _font_independent_key(job_dir, load_json(job_dir / "job.json"))
        after_validation = validate_job(
            job_dir,
            "translated",
            status_override="translated",
        )
        after_audit = build_completeness_audit(
            job_dir,
            include_candidate=False,
        )

        if before_key != after_key:
            raise AssertionError(
                "缓存键不应因 selected_fonts 变化而改变"
            )
        if before_validation["errors"] != after_validation["errors"]:
            raise AssertionError(
                "translated 阶段校验开始依赖 selected_fonts，缓存键必须同步收紧"
            )
        if before_validation["warnings"] != after_validation["warnings"]:
            raise AssertionError(
                "translated 阶段校验的警告开始依赖 selected_fonts，"
                "缓存键必须同步收紧"
            )
        for field in ("decision", "repair_pages", "review_pages"):
            if before_audit.get(field) != after_audit.get(field):
                raise AssertionError(
                    f"完整性审计的 {field} 开始依赖 selected_fonts，"
                    "缓存键必须同步收紧"
                )

        job = load_json(job_dir / "job.json")
        job["translation"]["target_language"] = "ja"
        write_json(job_dir / "job.json", job)
        if _font_independent_key(
            job_dir,
            load_json(job_dir / "job.json"),
        ) == after_key:
            raise AssertionError("除字体外的作业字段变化必须让缓存键失效")
