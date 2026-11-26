"""The register is generated from pytest, and every row has tests behind it."""
from __future__ import annotations

import ast
from pathlib import Path

from tests.evidence_plugin import REQUIREMENTS, resolve_control

TESTS = Path(__file__).resolve().parent


def evidence_markers():
    """(file, test name, row, control) for every @pytest.mark.evidence(...) in tests/."""
    for f in sorted(TESTS.glob("test_*.py")):
        for node in ast.walk(ast.parse(f.read_text())):
            if not isinstance(node, ast.FunctionDef):
                continue
            for d in node.decorator_list:
                if isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "evidence":
                    row = d.args[0].value
                    kw = next((k.value for k in d.keywords if k.arg == "control"), None)
                    control = kw.value if isinstance(kw, ast.Constant) else None   # names: module constants
                    yield f.name, node.name, row, control


def test_every_register_row_has_a_linked_test():
    linked = {}
    for fname, test, row, control in evidence_markers():
        assert row in REQUIREMENTS, f"{fname}::{test} points to unknown row {row}"
        linked.setdefault(row, []).append(test)
        if control:
            assert resolve_control(control)
    missing = [r for r in REQUIREMENTS if r not in linked]
    assert not missing, f"register rows without a test: {missing}"


def test_register_rows_are_not_written_by_hand():
    page = TESTS.parent / "site" / "index.html"
    if page.exists():
        html = page.read_text()
        for rid, req in REQUIREMENTS.items():
            assert req["title"] not in html, f"{rid} title hard-coded in the page; it must come from evidence.json"
