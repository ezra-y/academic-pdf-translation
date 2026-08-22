"""交付层的数据模型。

这里只放"事实的形状"，不放判断。判断在 :mod:`gates`。

核心是 :class:`BuildOutcome`：生成器跑完一轮之后，把它自己报告的状态、
产物路径和证据路径原样带出来。之前外层只拿一个 ``Path``，生成器说
BLOCKED 也拦不住交付流程——因为状态在半路被扔掉了。现在状态跟着产物走，
谁也扔不掉。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

#: 生成器会报告的三种已知状态。
BUILD_READY = "READY_TO_REGISTER"
BUILD_NEEDS_REPAIR = "NEEDS_REPAIR"
BUILD_BLOCKED = "BLOCKED_BEFORE_PREFLIGHT"

KNOWN_BUILD_STATUSES = frozenset(
    {BUILD_READY, BUILD_NEEDS_REPAIR, BUILD_BLOCKED}
)


@dataclass
class BuildOutcome:
    """生成器一轮的完整结果。

    字段类型固定：``issues`` 永远是列表，``candidate_path`` 可以是 None
    但不会是空字符串。下游按类型取值，不用层层判空。
    """

    status: str
    candidate_path: Path | None = None
    blocked_stage: str | None = None
    issues: list[Any] = field(default_factory=list)
    preflight_path: Path | None = None
    render_readiness_path: Path | None = None
    candidate_sha256: str | None = None
    renderer_build_id: str = ""
    run_id: str = ""
    attempt_id: str = ""
    #: 这一轮**实际用过**的渲染计划的哈希。返修会重算计划，所以它必须
    #: 跟着每一轮走，不能由调用方在流程开始时读一次就当成全程有效。
    render_plan_sha256: str = ""
    #: 这一轮实际用过的渲染计划快照。合同对账只认它，不认磁盘上
    #: "现在"那一份——磁盘上那份可能已经是下一轮的了。
    render_plan: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("candidate_path", "preflight_path", "render_readiness_path"):
            if data[key] is not None:
                data[key] = str(data[key])
        # 计划正文另存为 attempt 目录里的快照文件，构建记录只留哈希，
        # 免得一份报告里塞进整个计划。
        data.pop("render_plan", None)
        return data

    def report_sha256(self) -> str:
        """整份构建报告的哈希，写进交付结论供事后核对。"""

        canonical = json.dumps(
            self.as_dict(), ensure_ascii=False, sort_keys=True
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_outcome_from_report(
    report: dict[str, Any],
    *,
    run_id: str,
    attempt_id: str,
    render_plan_sha256: str = "",
    render_plan: dict[str, Any] | None = None,
) -> BuildOutcome:
    """把生成器的字典报告翻译成 :class:`BuildOutcome`。

    状态原样透传——这里不做任何"看起来还行"的修饰。候选文件存在时
    顺手算好字节哈希，后面的证据绑定要用。

    ``render_plan_sha256`` 与 ``render_plan`` 由调用方在**这一轮生成
    结束之后**读取，代表这一轮真正用过的那份计划。
    """

    candidate = report.get("candidate_pdf")
    candidate_path = Path(candidate) if candidate else None
    sha256 = None
    if candidate_path is not None and candidate_path.is_file():
        sha256 = file_sha256(candidate_path)

    issues = list(report.get("issues") or [])
    if not issues:
        issues = list(report.get("hard_failures") or [])

    preflight = report.get("preflight")
    readiness = report.get("render_readiness")
    return BuildOutcome(
        status=str(report.get("status") or ""),
        candidate_path=candidate_path,
        blocked_stage=report.get("blocked_stage"),
        issues=issues,
        preflight_path=Path(preflight) if preflight else None,
        render_readiness_path=Path(readiness) if readiness else None,
        candidate_sha256=sha256,
        renderer_build_id=str(report.get("renderer_build_id") or ""),
        run_id=run_id,
        attempt_id=attempt_id,
        render_plan_sha256=render_plan_sha256,
        render_plan=render_plan,
    )
