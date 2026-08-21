from __future__ import annotations

import ast
import json
import re
from pathlib import Path


class BundleCheckError(RuntimeError):
    pass


ALLOWED_FRONTMATTER_KEYS = {
    "name",
    "description",
    "license",
    "allowed-tools",
    "metadata",
}
REQUIRED_INTERFACE_KEYS = {
    "display_name",
    "short_description",
    "default_prompt",
}
BANNED_RUNTIME_IMPORTS = {
    "lmstudio",
    "ollama",
    "torch",
    "transformers",
}
LOCAL_ABSOLUTE_PATH_RE = re.compile(
    r"(?:/(?:Users|home)/[^ \t\r\n\"']+|[A-Za-z]:\\\\[^ \t\r\n\"']+)"
)
FROZEN_UNIT_ID_RE = re.compile(r"\bp\d{4}-u\d{4}\b")
PAGE_SELECTOR_NAMES = {"page", "page_number"}
REQUIRED_DEPENDENCIES = {"PyMuPDF", "Pillow", "reportlab"}


def _skill_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def _frontmatter(skill_md: Path) -> dict[str, str]:
    content = skill_md.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---(?:\n|$)", content, re.DOTALL)
    if not match:
        raise BundleCheckError("SKILL.md 缺少有效 YAML frontmatter")

    values: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if not line.strip() or line[:1].isspace() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition(":")
        if not separator:
            raise BundleCheckError(f"frontmatter 顶层字段格式无效: {line}")
        key = key.strip()
        if key not in ALLOWED_FRONTMATTER_KEYS:
            raise BundleCheckError(f"frontmatter 含未批准字段: {key}")
        values[key] = value.strip().strip("\"'")
    return values


def _check_skill_metadata(root: Path) -> None:
    values = _frontmatter(root / "SKILL.md")
    name = values.get("name", "")
    description = values.get("description", "")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
        raise BundleCheckError("Skill 名称必须为小写 hyphen-case")
    if len(name) > 64:
        raise BundleCheckError("Skill 名称超过 64 个字符")
    if not description:
        raise BundleCheckError("Skill description 不能为空")
    if len(description) > 1024 or "<" in description or ">" in description:
        raise BundleCheckError("Skill description 不符合长度或字符约束")


def _check_interface_metadata(root: Path) -> None:
    path = root / "agents" / "openai.yaml"
    content = path.read_text(encoding="utf-8")
    keys = {
        match.group(1)
        for match in re.finditer(r"^\s{2}([a-z_]+):\s*\S", content, re.MULTILINE)
    }
    missing = REQUIRED_INTERFACE_KEYS - keys
    if missing:
        raise BundleCheckError(
            f"agents/openai.yaml 缺少字段: {', '.join(sorted(missing))}"
        )


def _check_reviewer_agent(root: Path) -> None:
    path = root / "agents" / "independent-reviewer.md"
    content = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---(?:\n|$)", content, re.DOTALL)
    if not match:
        raise BundleCheckError(
            "agents/independent-reviewer.md 缺少有效 frontmatter"
        )
    values: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, separator, value = line.partition(":")
        if separator and not line[:1].isspace():
            values[key.strip()] = value.strip().strip("\"'")
    if values.get("name") != "independent-pdf-reviewer":
        raise BundleCheckError("独立审查 Agent 名称不一致")
    if not values.get("description"):
        raise BundleCheckError("独立审查 Agent description 不能为空")
    if "reviews/independent.json" not in content:
        raise BundleCheckError("独立审查 Agent 缺少输出合同")
    skill_content = (root / "SKILL.md").read_text(encoding="utf-8")
    if "agents/independent-reviewer.md" not in skill_content:
        raise BundleCheckError("SKILL.md 尚未接入独立审查 Agent")


def _check_json_assets(root: Path) -> None:
    assets = sorted((root / "assets").glob("*.json"))
    if not assets:
        raise BundleCheckError("assets 中没有 JSON 契约")
    for path in assets:
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise BundleCheckError(f"JSON 无法解析: {path.name}: {exc}") from exc


def _check_python_sources(root: Path) -> None:
    scripts = sorted((root / "scripts").glob("*.py"))
    if not scripts:
        raise BundleCheckError("scripts 中没有 Python 入口")
    for path in scripts:
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            raise BundleCheckError(f"Python 语法错误: {path.name}: {exc}") from exc
        for node in ast.walk(tree):
            if (
                path.name != "self_test.py"
                and isinstance(node, ast.Constant)
                and isinstance(node.value, str)
            ):
                if LOCAL_ABSOLUTE_PATH_RE.search(node.value):
                    raise BundleCheckError(
                        f"{path.name} 含本机绝对路径，生产脚本必须从参数或作业数据读取"
                    )
                if FROZEN_UNIT_ID_RE.search(node.value):
                    raise BundleCheckError(
                        f"{path.name} 含固定翻译单元 ID，生产脚本不得绑定单篇论文"
                    )
            if path.name != "self_test.py" and isinstance(node, ast.Compare):
                expressions = [node.left, *node.comparators]
                has_page_name = any(
                    isinstance(expression, ast.Name)
                    and expression.id in PAGE_SELECTOR_NAMES
                    for expression in expressions
                )
                has_fixed_page = any(
                    isinstance(expression, ast.Constant)
                    and isinstance(expression.value, int)
                    and not isinstance(expression.value, bool)
                    and expression.value > 0
                    for expression in expressions
                )
                has_direct_equality = any(
                    isinstance(operator, (ast.Eq, ast.NotEq))
                    for operator in node.ops
                )
                if has_page_name and has_fixed_page and has_direct_equality:
                    raise BundleCheckError(
                        f"{path.name}:{node.lineno} 按固定页码分支，"
                        "生产逻辑必须从作业数据或页面类型读取"
                    )
            imported: list[str] = []
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = [node.module]
            for module in imported:
                root_module = module.split(".", 1)[0].lower()
                if root_module in BANNED_RUNTIME_IMPORTS:
                    raise BundleCheckError(
                        f"{path.name} 导入了被禁止的运行时: {root_module}"
                    )


def _check_requirements(root: Path) -> None:
    path = root / "requirements.txt"
    dependencies = {
        re.split(r"[<>=!~\[]", line.strip(), maxsplit=1)[0]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    missing = REQUIRED_DEPENDENCIES - dependencies
    if missing:
        raise BundleCheckError(
            "requirements.txt 缺少运行依赖: "
            + ", ".join(sorted(missing))
        )


def _check_module_reachability(root: Path) -> None:
    """每个模块要么是命令行入口，要么被生产代码引用。

    只被 self_test.py 引用、又没有命令行入口的模块，就是"文档说该用、
    实际没人调用"的死抽象。这类模块会让文档和真实调用链慢慢分叉。
    """

    scripts_dir = root / "scripts"
    modules = {path.stem: path for path in sorted(scripts_dir.glob("*.py"))}
    imported_by: dict[str, set[str]] = {name: set() for name in modules}
    entry_points: set[str] = set()

    for name, path in modules.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        has_main = any(
            isinstance(node, ast.FunctionDef) and node.name == "main"
            for node in tree.body
        )
        runs_standalone = any(
            isinstance(node, ast.If)
            and ast.dump(node.test).find("__main__") >= 0
            for node in tree.body
        )
        if has_main and runs_standalone:
            entry_points.add(name)
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [alias.name.split(".", 1)[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                targets = [node.module.split(".", 1)[0]]
            for target in targets:
                if target in modules and target != name:
                    imported_by[target].add(name)

    unreachable = sorted(
        name
        for name in modules
        if name not in entry_points
        and not (imported_by[name] - {"self_test"})
    )
    if unreachable:
        raise BundleCheckError(
            "以下模块既不是命令行入口，也没有被生产代码引用，"
            "属于失效抽象: " + ", ".join(unreachable)
        )


def check_bundle(root: Path | None = None) -> dict[str, int | str]:
    skill_root = (root or _skill_dir()).resolve()
    required = [
        skill_root / ".gitignore",
        skill_root / "LICENSE",
        skill_root / "README.md",
        skill_root / "README_EN.md",
        skill_root / "SKILL.md",
        skill_root / "agents" / "independent-reviewer.md",
        skill_root / "agents" / "openai.yaml",
        skill_root / "assets" / "job.schema.json",
        skill_root / "assets" / "language-profiles.json",
        skill_root / "assets" / "workspace.schema.json",
        skill_root
        / "assets"
        / "examples"
        / "comparison-japanese-to-english-jglue.png",
        skill_root
        / "assets"
        / "examples"
        / "comparison-localized-screenshot.png",
        skill_root
        / "assets"
        / "examples"
        / "comparison-quadrant-model.png",
        skill_root
        / "assets"
        / "examples"
        / "comparison-structured-table.png",
        skill_root / "references" / "workspace.md",
        skill_root / "Workspace" / ".gitignore",
        skill_root / "Workspace" / "README.md",
        skill_root / "requirements.txt",
    ]
    missing = [str(path.relative_to(skill_root)) for path in required if not path.is_file()]
    if missing:
        raise BundleCheckError(f"Skill 缺少必要文件: {', '.join(missing)}")

    _check_skill_metadata(skill_root)
    _check_interface_metadata(skill_root)
    _check_reviewer_agent(skill_root)
    _check_json_assets(skill_root)
    _check_python_sources(skill_root)
    _check_requirements(skill_root)
    _check_module_reachability(skill_root)
    return {
        "status": "PASS",
        "python_files": len(list((skill_root / "scripts").glob("*.py"))),
        "json_assets": len(list((skill_root / "assets").glob("*.json"))),
    }


def main() -> int:
    try:
        report = check_bundle()
    except Exception as exc:
        print(f"BUNDLE CHECK FAIL: {exc}")
        return 1
    print(
        "BUNDLE CHECK PASS "
        f"({report['python_files']} Python files, "
        f"{report['json_assets']} JSON assets)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
