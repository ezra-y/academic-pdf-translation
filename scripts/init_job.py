from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from _common import (
    SCHEMA_VERSION,
    SkillError,
    load_json,
    resolve_language_profile,
    sha256_file,
    utc_now,
    write_json,
)
from extract_source_structure import extract_source_structure
from pdf_profile import profile_pdf
from prepare_translation_units import (
    build_source_units,
    build_translation_skeleton,
)
from review_policy import (
    post_repair_confirmation_template,
    review_choice_config,
)
from workspace import (
    TranslationWorkspace,
    open_workspace,
    workspace_job_dir,
)


def _review_template(role: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "reviewer_role": role,
        "reviewer_id": None,
        "decision": "PENDING",
        "source_sha256": None,
        "candidate_sha256": None,
        "coverage": [],
        "reviewed_pages": [],
        "issues": [],
        "residual_risks": [],
        "reviewed_at": None,
    }


def _merge_structure_candidates(
    manifest: dict,
    structure: dict,
) -> list[int]:
    page_count = int(manifest.get("page_count") or 0)
    candidates = {
        int(page)
        for page in manifest.get("complex_pages", [])
        if isinstance(page, int) and 1 <= page <= page_count
    }
    candidates.update(
        int(page)
        for page in structure.get("visual_confirmation_pages", [])
        if isinstance(page, int) and 1 <= page <= page_count
    )
    merged = sorted(candidates)
    manifest["complex_pages"] = merged

    route = manifest.setdefault("route", {})
    if merged and route.get("recommended") == "standard-auto":
        route["recommended"] = "hybrid-complex-pages"
        reasons = list(route.get("reasons") or [])
        reason = "结构提取提示存在需目视确认的图表、图片或阅读顺序页面"
        if reason not in reasons:
            reasons.append(reason)
        route["reasons"] = reasons
    return merged


def _existing_job_dirs(
    source_sha256: str,
    registry_root: Path,
    *,
    exclude: Path | None = None,
) -> list[Path]:
    excluded = exclude.resolve() if exclude is not None else None
    matches: list[Path] = []
    for job_path in registry_root.rglob("job.json"):
        relative_parts = job_path.relative_to(registry_root).parts
        if any(
            part in {"history", "staging", "comparisons"}
            for part in relative_parts[:-1]
        ):
            continue
        job_dir = job_path.parent.resolve()
        if excluded is not None and job_dir == excluded:
            continue
        try:
            job = load_json(job_path)
        except (OSError, ValueError, SkillError):
            continue
        source = job.get("source")
        if (
            isinstance(source, dict)
            and source.get("sha256") == source_sha256
        ):
            matches.append(job_dir)
    return sorted(set(matches))


def _existing_workspace_job(
    source_sha256: str,
    workspace: TranslationWorkspace,
) -> Path | None:
    matches = _existing_job_dirs(source_sha256, workspace.jobs)
    if len(matches) > 1:
        paths = "\n".join(f"- {path}" for path in matches)
        raise SkillError(
            "标准工作区中存在多份相同原文作业，请先合并:\n" + paths
        )
    return matches[0] if matches else None


def initialize_job(
    source: Path,
    job_dir: Path,
    target_language: str,
    source_language: str,
    zotero_required: bool,
    review: str = "balanced",
    registry_root: Path | None = None,
    producer_id: str | None = None,
    workspace: TranslationWorkspace | None = None,
) -> dict:
    if not source.is_file():
        raise SkillError(f"原文不存在: {source}")
    source = source.resolve()
    job_dir = job_dir.resolve()
    source_hash = sha256_file(source)
    if workspace is not None:
        if job_dir.parent != workspace.jobs:
            raise SkillError(
                "批次作业必须直接位于 workspace/.work/jobs 下"
            )
        if registry_root is None:
            registry_root = workspace.jobs
        elif registry_root.resolve() != workspace.jobs:
            raise SkillError(
                "使用批次工作区时，作业索引根目录必须是 .work/jobs"
            )
    canonical_language, profile = resolve_language_profile(target_language)
    try:
        review_mode, max_review_rounds, max_repair_rounds = (
            review_choice_config(review)
        )
    except ValueError as exc:
        raise SkillError(str(exc)) from exc
    if producer_id is not None and not producer_id.strip():
        raise SkillError("producer_id 不能是空字符串")
    if review_mode in {"independent", "precise"} and not (
        isinstance(producer_id, str) and producer_id.strip()
    ):
        raise SkillError("平衡档或精细档必须提供 --producer-id")
    if registry_root is not None:
        registry_root = registry_root.resolve()
        if not registry_root.is_dir():
            raise SkillError(f"作业索引根目录不存在: {registry_root}")
        existing = _existing_job_dirs(
            source_hash,
            registry_root,
            exclude=job_dir,
        )
        if existing:
            paths = "\n".join(f"- {path}" for path in existing)
            raise SkillError(
                "同一原文已经存在作业，请恢复现有作业，不要重复初始化:\n"
                + paths
            )
    if job_dir.exists() and any(job_dir.iterdir()):
        raise SkillError(f"作业目录不是空目录: {job_dir}")
    job_dir.mkdir(parents=True, exist_ok=True)
    for relative in (
        "reviews",
        "renders/source",
        "renders/candidate",
        "comparisons",
        "staging",
    ):
        (job_dir / relative).mkdir(parents=True, exist_ok=True)

    job_source = job_dir / "source.pdf"
    if source != job_source:
        shutil.copy2(source, job_source)

    manifest = profile_pdf(job_source)
    structure = extract_source_structure(job_source)
    heuristic_candidate_pages = _merge_structure_candidates(
        manifest,
        structure,
    )
    write_json(job_dir / "source_manifest.json", manifest)
    write_json(job_dir / "source_structure.json", structure)
    detected_source = manifest["source_language_estimate"]
    source_language = detected_source if source_language == "auto" else source_language
    files = {
        "source_manifest": "source_manifest.json",
        "source_structure": "source_structure.json",
        "source_units": "source_units.json",
        "translation": "translation.json",
        "retained_source": "retained_source.json",
        "figure_inventory": "figure_inventory.json",
        "complex_content_payload": "complex_content.json",
        "layout_overrides": "layout_overrides.json",
        "render_readiness": "staging/render-readiness.json",
        "preflight_ledger": "staging/preflight-ledger.json",
        "candidate": "candidate.pdf",
        "candidate_page_map": "candidate-page-map.json",
        "candidate_provenance": "candidate_provenance.json",
        "qa": "qa.json",
        "independent_review": "reviews/independent.json",
        "post_repair_confirmation": "reviews/post-repair.json",
        "review_rounds": "reviews/rounds.json",
        "run_metrics": "run-metrics.json",
        "work_checkpoint": "work_checkpoint.json",
        "finalization": "finalization.json",
    }
    job = {
        "schema_version": SCHEMA_VERSION,
        "job_id": f"{source.stem}-{source_hash[:10]}",
        "created_at": utc_now(),
        "status": "initialized",
        "source": {
            "original_path": str(source),
            "job_path": "source.pdf",
            "sha256": source_hash,
            "page_count": manifest["page_count"],
        },
        "translation": {
            "source_language": source_language,
            "target_language": canonical_language,
            "mapping_mode": "frozen-source-units-v1",
        },
        "route": {
            "recommended": manifest["route"]["recommended"],
            "selected": None,
            "decision_reason": "",
            "complex_content": {
                "classification_confirmed": False,
                "review_scope": "all-source-pages",
                "heuristic_candidate_pages": heuristic_candidate_pages,
                "confirmed_pages": [],
                "notes": "",
            },
        },
        "review": {
            "mode": review_mode,
            "choice_recorded": True,
            "producer_id": producer_id.strip() if producer_id else None,
            "max_review_rounds": max_review_rounds,
            "max_repair_rounds": max_repair_rounds,
        },
        "quality": {
            "profile": canonical_language,
            "profile_basis": profile["basis"],
            "body_font_min_pt": profile["body_font_min_pt"],
            "body_font_target_pt": profile["body_font_target_pt"],
            "body_font_preferred_pt": profile["body_font_preferred_pt"],
            "leading_target": profile["leading_target"],
            "leading_preferred": profile["leading_preferred"],
            "leading_exception_min": profile["leading_exception_min"],
            "table_font_min_pt": profile.get("table_font_min_pt", 7.0),
            "typography_search": profile.get("typography_search"),
            "body_width_retention_min": profile.get(
                "body_width_retention_min", 0.72
            ),
            "body_width_loss_trigger": profile.get(
                "body_width_loss_trigger", 0.12
            ),
            "font_candidates": profile["font_candidates"],
            "selected_fonts": [],
        },
        "files": files,
        "integration": {
            "zotero_required": zotero_required,
        },
    }
    if workspace is not None:
        workspace_metadata = workspace.job_metadata()
        workspace_metadata["job"] = str(job_dir)
        job["workspace"] = workspace_metadata
    write_json(job_dir / "job.json", job)
    source_units = build_source_units(structure)
    source_units_path = job_dir / files["source_units"]
    write_json(source_units_path, source_units)
    write_json(
        job_dir / files["translation"],
        build_translation_skeleton(
            source_units,
            source_language=source_language,
            target_language=canonical_language,
            source_units_sha256=sha256_file(source_units_path),
        ),
    )
    write_json(
        job_dir / files["retained_source"],
        {
            "schema_version": SCHEMA_VERSION,
            "items": [],
            "regions": [],
        },
    )
    write_json(
        job_dir / files["figure_inventory"],
        {
            "schema_version": SCHEMA_VERSION,
            "inventory_complete": False,
            "candidate_sha256": None,
            "scope_note": "",
            "items": [],
        },
    )
    write_json(
        job_dir / files["complex_content_payload"],
        {
            "schema_version": SCHEMA_VERSION,
            "classification_complete": False,
            "items": [],
        },
    )
    write_json(
        job_dir / files["layout_overrides"],
        {
            "schema_version": SCHEMA_VERSION,
            "body_regions": [],
            "non_body_regions": [],
            "leading_exceptions": [],
            "page_overrides": [],
        },
    )
    write_json(
        job_dir / files["candidate_page_map"],
        {
            "schema_version": SCHEMA_VERSION,
            "generated_at": None,
            "mapping_mode": "flow-unit-anchors-v1",
            "layout_policy": "continuous-reading",
            "complete": False,
            "source_sha256": source_hash,
            "translation_sha256": None,
            "candidate_sha256": None,
            "source_page_count": manifest["page_count"],
            "candidate_page_count": 0,
            "source_pages": [],
            "candidate_pages": [],
            "units": [],
            "complex_items": [],
        },
    )
    write_json(
        job_dir / files["candidate_provenance"],
        {
            "schema_version": SCHEMA_VERSION,
            "iteration": 0,
            "registered_at": None,
            "renderer": None,
            "renderer_version": None,
            "renderer_build_id": None,
            "producer_id": None,
            "original_candidate_path": None,
            "candidate_sha256": None,
            "translation_sha256": None,
            "layout_overrides_sha256": None,
            "candidate_page_map_sha256": None,
            "supersedes_candidate_sha256": None,
            "notes": None,
        },
    )
    write_json(
        job_dir / files["preflight_ledger"],
        {
            "schema_version": SCHEMA_VERSION,
            "cycles": [],
        },
    )
    write_json(
        job_dir / files["independent_review"],
        _review_template("independent"),
    )
    write_json(
        job_dir / files["review_rounds"],
        {
            "schema_version": SCHEMA_VERSION,
            "rounds": [],
        },
    )
    write_json(
        job_dir / files["post_repair_confirmation"],
        post_repair_confirmation_template(source_hash),
    )
    write_json(
        job_dir / files["run_metrics"],
        {
            "schema_version": SCHEMA_VERSION,
            "job_id": job["job_id"],
            "events": [],
        },
    )
    write_json(
        job_dir / files["work_checkpoint"],
        {
            "schema_version": SCHEMA_VERSION,
            "job": job_dir.name,
            "source_page_count": manifest["page_count"],
            "completed_pages": [],
            "completed_page_count": 0,
            "last_completed_page": None,
            "next_page": 1,
            "checkpoint_interval_pages": 5,
            "phase": "translation",
            "status": "not_started",
            "blocking_issue": None,
            "note": "作业已初始化，尚未开始翻译。",
            "updated_at": utc_now(),
        },
    )
    write_json(
        job_dir / files["finalization"],
        {
            "schema_version": SCHEMA_VERSION,
            "review_mode": review_mode,
            "formal_pdf": None,
            "sha256": None,
            "zotero": {
                "parent_item": None,
                "source_attachment": None,
                "translation_attachment": None,
                "source_index_check": False,
                "translation_index_check": False,
            },
        },
    )
    return job


def main() -> int:
    parser = argparse.ArgumentParser(description="初始化一个可审计的学术 PDF 译制作业")
    parser.add_argument("source_pdf", type=Path)
    parser.add_argument(
        "job_dir",
        nargs="?",
        type=Path,
        help="兼容入口；新项目优先使用 --workspace",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        help="本次翻译请求的批次工作区；作业写入隐藏的 .work/jobs",
    )
    parser.add_argument(
        "--job-name",
        help="可选；批次隐藏目录中的单篇作业名",
    )
    parser.add_argument("--target-language", default="zh-Hans")
    parser.add_argument("--source-language", default="auto")
    parser.add_argument(
        "--job-root",
        type=Path,
        help="可选；初始化前按原文哈希搜索该目录下全部现有作业",
    )
    parser.add_argument("--no-zotero", action="store_true")
    parser.add_argument(
        "--producer-id",
        help="制作智能体或制作人的稳定 ID；平衡和精细档验收前必须填写",
    )
    parser.add_argument(
        "--review",
        choices=("fast", "balanced", "precise", "on", "off"),
        default="balanced",
        help=(
            "fast 只做基础检查；balanced 复审一次并集中返修一次；"
            "precise 仍只完整复审一次，但加强返修页核对。"
            "on/off 为兼容别名"
        ),
    )
    args = parser.parse_args()
    try:
        workspace = None
        if args.workspace is not None:
            if args.job_dir is not None:
                raise SkillError("不能同时提供 job_dir 和 --workspace")
            if args.job_root is not None:
                raise SkillError(
                    "使用 --workspace 时无需再提供 --job-root"
                )
            if not args.source_pdf.is_file():
                raise SkillError(f"原文不存在: {args.source_pdf}")
            workspace = open_workspace(args.workspace)
            source_hash = sha256_file(args.source_pdf)
            existing_job = _existing_workspace_job(
                source_hash,
                workspace,
            )
            if existing_job is not None:
                existing = load_json(existing_job / "job.json")
                print(f"批次工作区: {workspace.root}")
                print(f"发现已有作业，继续使用: {existing_job}")
                print(f"当前状态: {existing.get('status', 'unknown')}")
                print(
                    "目标语言: "
                    + str(
                        existing.get("translation", {}).get(
                            "target_language",
                            "unknown",
                        )
                    )
                )
                return 0
            job_dir = workspace_job_dir(
                workspace,
                args.source_pdf,
                source_hash,
                job_name=args.job_name,
            )
            registry_root = workspace.jobs
        else:
            if args.job_dir is None:
                raise SkillError("请提供 job_dir，或改用 --workspace")
            if args.job_name is not None:
                raise SkillError("--job-name 只能与 --workspace 一起使用")
            job_dir = args.job_dir
            registry_root = args.job_root

        job = initialize_job(
            args.source_pdf,
            job_dir,
            args.target_language,
            args.source_language,
            not args.no_zotero,
            args.review,
            registry_root,
            args.producer_id,
            workspace,
        )
        if workspace is not None:
            print(f"批次工作区: {workspace.root}")
        print(f"作业已初始化: {job_dir.resolve()}")
        print(f"建议路线: {job['route']['recommended']}")
        print(f"目标语言: {job['translation']['target_language']}")
        print(
            "质量档位: "
            + {
                "none": "快速",
                "independent": "平衡（推荐）",
                "precise": "精细",
            }[job["review"]["mode"]]
        )
        return 0
    except SkillError as exc:
        print(f"错误: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
