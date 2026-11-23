"""Integration checks for parser uncertainty and package-local projection context."""

import pytest

from lean_explore.extract.source.reader import parse_package_sources

BASE = "https://github.com/owner/pkg/blob/abc123/"


@pytest.fixture
def projection_package(tmp_path):
    """Provide separate configuration and use sites within a source package."""
    pytest.importorskip("lean_source_parser")
    root = tmp_path / "pkg"
    package = root / "Pkg"
    package.mkdir(parents=True)
    (package / "AConfig.lean").write_text(
        "import Mathlib.Tactic.Simps.Basic\n"
        "namespace Pkg\n"
        "structure Carrier where\n  val : Nat\n"
        "initialize_simps_projections Carrier (val → coe, as_prefix coe)\n"
        "end Pkg\n",
        encoding="utf-8",
    )
    (package / "ZUse.lean").write_text(
        "import Pkg.AConfig\n"
        "namespace Pkg\n"
        "@[simps coe]\n"
        "def make : Carrier := Carrier.mk 1\n"
        "@[simps coe]\n"
        "def inferred := Carrier.mk 2\n"
        "end Pkg\n",
        encoding="utf-8",
    )
    return root


def _package_module(module):
    return module == "Pkg" or module.startswith("Pkg.")


def test_exact_simps_name_uses_configuration_from_another_package_file(
    projection_package,
):
    """Project context resolves prefix names before the pipeline stores rows."""
    rows = {
        row.name: row
        for row in parse_package_sources(projection_package, BASE, _package_module)
    }
    assert set(rows) == {"Pkg.Carrier", "Pkg.make", "Pkg.coe_make", "Pkg.inferred"}
    generated = rows["Pkg.coe_make"]
    assert generated.module == "Pkg.ZUse"
    assert generated.source_text == ("@[simps coe]\ndef make : Carrier := Carrier.mk 1")
    assert generated.source_link == BASE + "Pkg/ZUse.lean#L3-L3"
    assert generated.dependencies is None


def test_unresolved_simps_name_requires_explicit_approximate_opt_in(projection_package):
    """An inferred result type must not introduce a guessed name by default."""
    default_rows = parse_package_sources(projection_package, BASE, _package_module)
    opted_in_rows = parse_package_sources(
        projection_package, BASE, _package_module, include_approximate=True
    )
    default_names = {row.name for row in default_rows}
    opted_in_names = {row.name for row in opted_in_rows}
    assert opted_in_names - default_names == {"Pkg.inferred_coe"}
    assert "Pkg.coe_make" in default_names
