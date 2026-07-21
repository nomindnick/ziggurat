"""Rule 8: the draft package is deletable — nothing OUTSIDE ziggurat/draft/ may
statically import it. The mock-draft CLI command imports it lazily (inside the
function body), so the enforcement scans IMPORT-TIME statements: everything that
executes when the module loads (including imports nested in module-level if/try/
with/loop blocks and class bodies), while exempting function bodies (the
sanctioned lazy pattern) and ``if TYPE_CHECKING:`` blocks (never executed at
runtime, so they cannot break ``import ziggurat`` after draft/ is deleted).
"""

import ast
from pathlib import Path

ZIGGURAT = Path(__file__).resolve().parents[1] / "ziggurat"


def _is_type_checking_guard(test: ast.expr) -> bool:
    """True for ``if TYPE_CHECKING:`` / ``if typing.TYPE_CHECKING:`` tests."""
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _import_time_statements(tree: ast.Module):
    """Yield every statement that executes at module import time."""
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue  # deferred until called — the sanctioned lazy pattern
        if isinstance(node, ast.If) and _is_type_checking_guard(node.test):
            stack.extend(node.orelse)  # the else branch still runs at runtime
            continue
        yield node
        for field in ("body", "orelse", "finalbody"):
            stack.extend(getattr(node, field, []))
        for handler in getattr(node, "handlers", []):
            stack.extend(handler.body)


def _imports_draft_at_import_time(path: Path) -> bool:
    """True if any import-time statement pulls in ziggurat.draft."""
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in _import_time_statements(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "ziggurat.draft" or alias.name.startswith("ziggurat.draft."):
                    return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "ziggurat.draft" or module.startswith("ziggurat.draft."):
                return True
            # `from ziggurat import draft`
            if module == "ziggurat" and any(a.name == "draft" for a in node.names):
                return True
    return False


def test_no_module_outside_draft_imports_the_draft_package():
    offenders = []
    for py in ZIGGURAT.rglob("*.py"):
        rel = py.relative_to(ZIGGURAT)
        if rel.parts[0] == "draft":
            continue
        if _imports_draft_at_import_time(py):
            offenders.append(str(rel))
    assert offenders == [], f"import-time imports of ziggurat.draft outside draft/: {offenders}"


def test_scanner_would_catch_a_real_violation(tmp_path):
    # Guard the guard: files that DO import the package at import time are flagged.
    cases = {
        "top.py": "from ziggurat.draft.simulator import run_many\n",
        "wrapped_if.py": "if True:\n    from ziggurat.draft.simulator import run_many\n",
        "wrapped_try.py": "try:\n    import ziggurat.draft.bots\nexcept ImportError:\n    pass\n",
        "class_body.py": "class C:\n    from ziggurat.draft import bots\n",
    }
    for name, src in cases.items():
        bad = tmp_path / name
        bad.write_text(src)
        assert _imports_draft_at_import_time(bad), f"scanner missed {name}"


def test_scanner_allows_sanctioned_patterns(tmp_path):
    # Lazy in-function import (the CLI's pattern) and TYPE_CHECKING-only imports
    # never execute at module load, so deleting draft/ cannot break them.
    cases = {
        "lazy.py": "def f():\n    from ziggurat.draft import run_many\n    return run_many\n",
        "type_checking.py": (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from ziggurat.draft.bots import BoardEntry\n"
        ),
    }
    for name, src in cases.items():
        good = tmp_path / name
        good.write_text(src)
        assert not _imports_draft_at_import_time(good), f"false positive on {name}"
