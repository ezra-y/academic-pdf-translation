"""pytest 入口：把既有的包检查与全套自测接入标准测试命令。

真正的断言仍然全部住在 scripts/self_test.py 里，这里只做转接，
避免出现两套互相分叉的测试。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_bundle import check_bundle  # noqa: E402
from self_test import run as run_self_test  # noqa: E402


def test_bundle_contract() -> None:
    assert check_bundle()["status"] == "PASS"


def test_full_self_test() -> None:
    run_self_test()
