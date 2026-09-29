"""Tests for the size limit checker script."""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "check_sizes.py"
spec = importlib.util.spec_from_file_location("check_sizes", SCRIPT)
check_sizes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_sizes)


def _function(name: str, code_lines: int) -> str:
    """Source for a function with a docstring, a comment, and N code lines."""
    body = "".join(f"    x{i} = {i}\n" for i in range(code_lines - 1))
    return (
        f"def {name}():\n"
        '    """Docstring\n\n    spanning lines.\n    """\n'
        "    # a comment\n\n"
        f"{body}    return 0\n"
    )


def test_counts_only_code_lines(tmp_path: Path):
    """Docstrings, comments, and blank lines do not count toward the limit."""
    source = tmp_path / "module.py"
    source.write_text(_function("small", 40) + "\n\n" + _function("large", 41))

    found = check_sizes.oversized_functions(source)

    assert [(name, count) for _, name, count in found] == [("large", 41)]


def test_nested_and_async_functions(tmp_path: Path):
    """Async functions and methods are checked like plain functions."""
    source = tmp_path / "module.py"
    source.write_text(
        "class A:\n"
        + "".join(
            "    " + line + "\n"
            for line in ("async " + _function("method", 45)).splitlines()
        )
    )

    [(_, name, count)] = check_sizes.oversized_functions(source)
    assert (name, count) == ("method", 45)


@pytest.mark.parametrize(("strict", "status"), [(False, 0), (True, 1)])
def test_main_exit_status(
    tmp_path: Path, monkeypatch, capsys, strict: bool, status: int
):
    """Violations are reported, and only fail the run in strict mode."""
    (tmp_path / "long.py").write_text("x = 1\n" * 501)
    argv = ["check_sizes.py", str(tmp_path)] + (["--strict"] if strict else [])
    monkeypatch.setattr(sys, "argv", argv)

    assert check_sizes.main() == status
    assert "501 lines (max 500)" in capsys.readouterr().out
