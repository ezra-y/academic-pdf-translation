"""工作流与工作区契约：路由初值、状态机、作业登记、标准工作区、开工前置。

这些用例原本住在 scripts/self_test.py，按“作业还没开始渲染之前的约定”
聚在一起：先决定走哪条路线，再决定谁能建作业、建在哪里、什么时候允许动手。
断言逐字保留原样（if 条件 + raise AssertionError(中文判据)），
中文说明本身就是判据文档。

单独运行：
    python3 -m pytest -q tests/test_workflow_contracts.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import re  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

from _self_test_helpers import _make_pdf  # noqa: E402

from _common import (  # noqa: E402
    SkillError,
    load_json,
    utc_now,
    write_json,
)
from build_candidate import (  # noqa: E402
    MappingTracker,
    _styles,
    _unit_flowables,
)
from i18n import message  # noqa: E402
from init_job import (  # noqa: E402
    _existing_job_dirs,
    _existing_workspace_job,
    _merge_structure_candidates,
    initialize_job,
)
from preflight_candidate import _preflight_cycle  # noqa: E402
from review_policy import (  # noqa: E402
    PRECISE_KEY_CHECKS,
    validate_post_repair_confirmation,
)
from semantic_markers import (  # noqa: E402
    infer_review_flags,
    validate_terminology,
)
from workspace import (  # noqa: E402
    WORKSPACE_ROOT_NAME,
    create_workspace,
    ensure_workspace_root,
    open_workspace,
    output_pdfs,
    workspace_job_dir,
)


def test_structure_candidates_feed_initial_route() -> None:
    manifest = {
        "page_count": 9,
        "complex_pages": [],
        "route": {
            "recommended": "standard-auto",
            "reasons": ["文本层和页面结构整体规则"],
        },
    }
    structure = {"visual_confirmation_pages": [1, 5, 6, 7, 9]}
    candidates = _merge_structure_candidates(manifest, structure)
    if candidates != [1, 5, 6, 7, 9]:
        raise AssertionError("结构提取的候选复杂页必须写回初始化结果")
    if manifest["route"]["recommended"] != "hybrid-complex-pages":
        raise AssertionError("存在候选复杂页时不得继续推荐纯正文路线")


def test_workflow_contracts() -> None:
    tracker = MappingTracker()
    tracker.note_heading(
        candidate_page=1,
        text="Methods",
        unit_id="heading-unit",
    )
    tracker.resolve_heading(2)
    if len(tracker.orphan_regions) != 1:
        raise AssertionError("标题与首段跨页时必须写入排版阻断证据")
    same_page_tracker = MappingTracker()
    same_page_tracker.note_heading(
        candidate_page=1,
        text="Methods",
        unit_id="heading-unit",
    )
    same_page_tracker.resolve_heading(1)
    if same_page_tracker.orphan_regions:
        raise AssertionError("标题与首段同页时不应误报")

    flow_styles = _styles(
        regular_font="Helvetica",
        bold_font="Helvetica-Bold",
        reference_font="Helvetica",
        body_font_pt=10,
        leading_ratio=1.6,
        reference_font_pt=8.5,
    )
    heading_flowables = _unit_flowables(
        {
            "id": "heading-unit",
            "page": 1,
            "kind": "heading",
            "translation": "Methods",
        },
        flow_styles,
    )
    if not heading_flowables[-1].getKeepWithNext():
        raise AssertionError("标题结束锚点必须继续绑定下一段正文")
    body_flowables = _unit_flowables(
        {
            "id": "body-unit",
            "page": 1,
            "kind": "body",
            "translation": "Body paragraph.",
        },
        flow_styles,
    )
    if body_flowables[-1].getKeepWithNext():
        raise AssertionError("普通正文结束锚点不应强制绑定下一段")
    short_body_flowables = _unit_flowables(
        {
            "id": "short-body-unit",
            "page": 1,
            "kind": "body",
            "translation": "未完的受访者引语",
        },
        flow_styles,
    )
    if short_body_flowables[1].style.name != "body":
        raise AssertionError("明确标为 body 的短片段不得按长度猜成标题")

    inferred = set(
        infer_review_flags(
            (
                "Results suggest that the scale may be associated with "
                "well-being in N = 120 participants."
            ),
            "results",
            "en",
        )
    )
    expected = {
        "semantic-boundary",
        "semantic-high-risk",
        "statistics-or-sample",
        "instrument-item-or-scoring",
    }
    if not expected.issubset(inferred):
        raise AssertionError("高风险语义、统计和量表标记必须自动推断")

    terminology_units = [
        {
            "id": "u1",
            "source": "Sleep quality predicts well-being.",
            "translation": "睡眠质量可以预测幸福感。",
        }
    ]
    terminology = [
        {"source": "Sleep quality", "target": "睡眠质量"}
    ]
    if validate_terminology(terminology, terminology_units):
        raise AssertionError("术语已按登记译法使用时不应报错")
    terminology_units[0]["translation"] = "睡眠状况可以预测幸福感。"
    if not validate_terminology(terminology, terminology_units):
        raise AssertionError("冻结原文中的登记术语被换译时必须报错")

    precise_checks = [
        {
            "category": category,
            "status": "PASS",
            "evidence": f"{category} 已核对",
        }
        for category in PRECISE_KEY_CHECKS
    ]
    confirmation = {
        "mode": "precise",
        "producer_id": "producer",
        "reviewer_id": "reviewer",
        "decision": "PASS",
        "source_sha256": "s" * 64,
        "base_review_candidate_sha256": "b" * 64,
        "candidate_sha256": "c" * 64,
        "changed_pages": [2],
        "same_type_pages": [4],
        "checked_pages": [1, 2, 3, 4],
        "key_content_checks": precise_checks,
        "issues": [],
        "qa_sha256": "q" * 64,
        "completeness_audit_sha256": "a" * 64,
        "comparison_manifest_sha256": "m" * 64,
        "reviewed_at": utc_now(),
    }
    confirmation_errors = validate_post_repair_confirmation(
        confirmation,
        mode="precise",
        producer_id="producer",
        reviewer_id="reviewer",
        source_hash="s" * 64,
        base_candidate_hash="b" * 64,
        candidate_hash="c" * 64,
        page_count=5,
        qa_hash="q" * 64,
        completeness_hash="a" * 64,
        comparison_manifest_hash="m" * 64,
    )
    if confirmation_errors:
        raise AssertionError(
            f"完整返修确认不应报错: {confirmation_errors}"
        )
    incomplete_confirmation = dict(confirmation)
    incomplete_confirmation["checked_pages"] = [2, 4]
    if not any(
        "相邻页" in error
        for error in validate_post_repair_confirmation(
            incomplete_confirmation,
            mode="precise",
            producer_id="producer",
            reviewer_id="reviewer",
            source_hash="s" * 64,
            base_candidate_hash="b" * 64,
            candidate_hash="c" * 64,
            page_count=5,
            qa_hash="q" * 64,
            completeness_hash="a" * 64,
            comparison_manifest_hash="m" * 64,
        )
    ):
        raise AssertionError("返修确认遗漏相邻页时必须报错")

    if re.search(r"[\u3400-\u9fff]", message("fr", "reading_version")):
        raise AssertionError("法语 PDF 可见标签不得混入中文")

    with tempfile.TemporaryDirectory(prefix="preflight-cycle-test-") as tmp:
        job_dir = Path(tmp)
        write_json(
            job_dir / "candidate_provenance.json",
            {"iteration": 0},
        )
        write_json(
            job_dir / "preflight-ledger.json",
            {"schema_version": "1.0", "cycles": []},
        )
        job = {
            "files": {
                "candidate_provenance": "candidate_provenance.json",
                "preflight_ledger": "preflight-ledger.json",
            }
        }
        build_id = "a" * 64
        _, ledger, cycle, attempt, _ = _preflight_cycle(
            job_dir,
            job,
            "academic-pdf-layout",
            "1.0.0",
            build_id,
            "1" * 64,
            "f1",
        )
        if attempt != 1:
            raise AssertionError("新代码构建首次预检应计为第 1 次")
        cycle["runs"].append(
            {
                "attempt": 1,
                "candidate_sha256": "1" * 64,
                "candidate_fingerprint": "f1",
            }
        )
        write_json(job_dir / "preflight-ledger.json", ledger)
        _, ledger, cycle, attempt, _ = _preflight_cycle(
            job_dir,
            job,
            "academic-pdf-layout",
            "1.0.1",
            build_id,
            "2" * 64,
            "f2",
        )
        if attempt != 2:
            raise AssertionError("只改显示版本号不得重置预检次数")
        cycle["runs"].append(
            {
                "attempt": 2,
                "candidate_sha256": "2" * 64,
                "candidate_fingerprint": "f2",
            }
        )
        write_json(job_dir / "preflight-ledger.json", ledger)
        _, _, _, attempt, _ = _preflight_cycle(
            job_dir,
            job,
            "academic-pdf-layout",
            "2.0.0",
            build_id,
            "3" * 64,
            "f3",
        )
        if attempt != 3:
            raise AssertionError("同一代码构建第三份候选必须超过预检上限")
        _, _, _, attempt, _ = _preflight_cycle(
            job_dir,
            job,
            "academic-pdf-layout",
            "2.0.0",
            "b" * 64,
            "4" * 64,
            "f4",
        )
        if attempt != 1:
            raise AssertionError("真实代码构建变化后才能开始新的预检周期")


def test_existing_job_registry_guard() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        existing = root / "nested" / "jobs" / "paper"
        existing.mkdir(parents=True)
        write_json(
            existing / "job.json",
            {
                "source": {
                    "sha256": "a" * 64,
                }
            },
        )
        historical = existing / "history" / "iteration-0001"
        historical.mkdir(parents=True)
        write_json(
            historical / "job.json",
            {
                "source": {
                    "sha256": "a" * 64,
                }
            },
        )
        matches = _existing_job_dirs("a" * 64, root)
        if matches != [existing.resolve()]:
            raise AssertionError("作业索引必须按原文哈希定位嵌套旧作业")
        if _existing_job_dirs(
            "a" * 64,
            root,
            exclude=existing,
        ):
            raise AssertionError("初始化目标目录本身不得被误报为外部重复作业")
        if _existing_job_dirs("b" * 64, root):
            raise AssertionError("不同原文哈希不得被判为重复作业")


def test_standard_workspace_contract() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source_a = root / "Sample Study.pdf"
        source_b = root / "Table Study.pdf"
        source_a.write_bytes(b"%PDF-1.4 workspace-test-a")
        source_b.write_bytes(b"%PDF-1.4 workspace-test-b")
        container = ensure_workspace_root(root / WORKSPACE_ROOT_NAME)
        workspace = create_workspace(
            "示例批次",
            [source_a, source_b],
            container=container,
        )
        visible = sorted(
            path.name
            for path in workspace.root.iterdir()
            if not path.name.startswith(".")
        )
        if visible != ["input", "output"]:
            raise AssertionError("批次内用户只能看到 input 和 output")
        if workspace.jobs != workspace.root / ".work" / "jobs":
            raise AssertionError("过程作业必须进入隐藏的 .work/jobs")
        if not (container / ".gitignore").is_file():
            raise AssertionError("Workspace 必须避免误提交用户 PDF")
        if not re.fullmatch(
            r"\d{8}-\d{6}_2篇_示例批次(?:_\d{2})?",
            workspace.root.name,
        ):
            raise AssertionError("批次名必须包含时间、篇数和标题")
        if len(list(workspace.input.glob("*.pdf"))) != 2:
            raise AssertionError("本批次输入 PDF 必须完整复制到 input")
        if output_pdfs(workspace):
            raise AssertionError("新批次的 output 必须为空")
        manifest = load_json(workspace.manifest)
        if (
            manifest.get("source_count") != 2
            or manifest.get("directories", {}).get("work") != ".work"
        ):
            raise AssertionError("批次清单必须冻结输入数量和隐藏过程目录")
        if open_workspace(workspace.root) != workspace:
            raise AssertionError("批次工作区必须可从隐藏清单恢复")

        job_dir = workspace_job_dir(
            workspace,
            Path("Meaning and Life.pdf"),
            "a" * 64,
        )
        if (
            job_dir.parent != workspace.jobs
            or not job_dir.name.endswith("-" + "a" * 10)
        ):
            raise AssertionError("默认作业目录必须位于 jobs 并绑定原文哈希")
        try:
            workspace_job_dir(
                workspace,
                Path("paper.pdf"),
                "a" * 64,
                job_name="../escape",
            )
        except SkillError:
            pass
        else:
            raise AssertionError("工作区作业名不得逃逸 jobs 目录")

        existing = workspace.jobs / "existing"
        existing.mkdir()
        write_json(
            existing / "job.json",
            {"source": {"sha256": "b" * 64}},
        )
        if _existing_workspace_job("b" * 64, workspace) != existing:
            raise AssertionError("标准入口必须恢复已有原文作业")

        delivered = workspace.output / "Meaning_中文译版.pdf"
        delivered.write_bytes(b"%PDF-1.4 translated")
        if output_pdfs(workspace) != [delivered.resolve()]:
            raise AssertionError("输出清单必须返回正式 PDF 的绝对路径")


def test_input_readiness_blocks_before_render() -> None:
    """输入不就绪时，流水线必须在生成任何候选 PDF 之前停下。"""

    from build_first_candidate import build_first_candidate
    from pre_render_audit import build_input_readiness_audit

    with tempfile.TemporaryDirectory() as raw_root:
        root = Path(raw_root)
        source = root / "readiness-source.pdf"
        _make_pdf(
            source,
            [
                [
                    "Adaptive cache invalidation in distributed systems.",
                    "We report a controlled evaluation across three sites.",
                ],
                [
                    "Results indicate a measurable reduction in stale reads.",
                    "Limitations and future work are discussed below.",
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

        readiness = build_input_readiness_audit(job_dir)
        if readiness["status"] != "BLOCKED":
            raise AssertionError("未翻译的作业必须在输入就绪检查中被拦截")
        if readiness.get("audit_scope") != "input-readiness":
            raise AssertionError("输入就绪检查必须标明检查范围")
        if not (job_dir / "staging" / "input-readiness.json").is_file():
            raise AssertionError("输入就绪检查必须落盘证据")

        report = build_first_candidate(job_dir)
        if report["status"] != "BLOCKED_BEFORE_PREFLIGHT":
            raise AssertionError("输入未就绪时不得进入预检")
        if report.get("blocked_stage") != "input-readiness":
            raise AssertionError("阻断阶段必须明确指向输入就绪检查")
        if report["timing_seconds"]["build"] != 0.0:
            raise AssertionError("输入未就绪时不得计入排版耗时")
        if report.get("build") != {}:
            raise AssertionError(
                "输入未就绪时 build 必须是空对象，保持字段类型稳定，"
                "否则下游 report.get(\"build\", {}).get(...) 会拿到 None"
            )
        produced = list((job_dir / "staging").glob("*.pdf"))
        if produced:
            raise AssertionError(
                "输入未就绪时不得生成任何候选 PDF: "
                + ", ".join(path.name for path in produced)
            )
        if not (job_dir / "generator-layout-log.json").exists():
            return
        raise AssertionError("输入未就绪时不得写出排版日志")
