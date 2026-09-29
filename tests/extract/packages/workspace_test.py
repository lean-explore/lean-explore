"""Tests for package configuration, registry, and package utilities."""

import pytest

from lean_explore.extract.packages import workspace
from lean_explore.extract.packages.config import PackageConfig, VersionStrategy
from lean_explore.extract.packages.registry import PACKAGE_REGISTRY
from lean_explore.extract.packages.workspace import (
    get_extraction_order,
    get_package_toolchain,
    update_lakefile_docgen_version,
)

DOCGEN_URL = '"https://github.com/leanprover/doc-gen4"'


def _config(strategy: VersionStrategy) -> PackageConfig:
    return PackageConfig(
        name="pkg",
        git_url="https://github.com/o/pkg",
        module_prefixes=["Pkg"],
        version_strategy=strategy,
    )


@pytest.fixture
def toolchains(monkeypatch) -> dict:
    """Stub GitHub lookups; maps ref -> toolchain, missing refs fail."""
    by_ref: dict = {}
    fetched: list = []

    def fetch(git_url, ref):
        fetched.append(ref)
        if ref not in by_ref:
            raise RuntimeError(f"no toolchain at {ref}")
        return by_ref[ref]

    monkeypatch.setattr(workspace, "fetch_lean_toolchain", fetch)
    monkeypatch.setattr(workspace, "fetch_latest_tag", lambda url: "v2.0.0")
    by_ref["fetched"] = fetched
    return by_ref


class TestPackageConfig:
    """Tests for PackageConfig and the registry."""

    def test_defaults(self):
        """Packages default to the tagged strategy with no dependencies."""
        config = PackageConfig(name="p", git_url="u", module_prefixes=["P"])
        assert config.version_strategy is VersionStrategy.TAGGED
        assert config.depends_on == []

    @pytest.mark.parametrize(
        ("module", "included"),
        [("Lean", True), ("Lean.Elab", True), ("LeanSearchClient", False)],
    )
    def test_should_include_module(self, module, included):
        """Prefixes match whole module path components only."""
        config = PackageConfig(name="p", git_url="u", module_prefixes=["Lean"])
        assert config.should_include_module(module) is included

    def test_registry_dependencies_exist(self):
        """Every declared dependency is itself a registered package."""
        for config in PACKAGE_REGISTRY.values():
            assert set(config.depends_on) <= set(PACKAGE_REGISTRY)

    def test_physlean_uses_upstream_module_roots(self):
        """PhysLean filters on the upstream module roots."""
        assert PACKAGE_REGISTRY["physlean"].module_prefixes == [
            "Physlib",
            "QuantumInfo",
        ]


class TestExtractionOrder:
    """Tests for dependency ordering."""

    def test_dependencies_come_first(self):
        """Each package appears after all of its dependencies."""
        order = get_extraction_order()
        assert sorted(order) == sorted(PACKAGE_REGISTRY)
        for name, config in PACKAGE_REGISTRY.items():
            for dep in config.depends_on:
                assert order.index(dep) < order.index(name)

    def test_transitive_and_unknown_dependencies(self, monkeypatch):
        """Transitive deps are ordered; unregistered deps are skipped."""
        registry = {
            "c": PackageConfig("c", "u", ["C"], depends_on=["b", "external"]),
            "b": PackageConfig("b", "u", ["B"], depends_on=["a"]),
            "a": PackageConfig("a", "u", ["A"]),
        }
        monkeypatch.setattr(workspace, "PACKAGE_REGISTRY", registry)
        assert get_extraction_order() == ["a", "b", "c"]


class TestGetPackageToolchain:
    """Tests for toolchain resolution by version strategy."""

    def test_latest_uses_main(self, toolchains):
        """LATEST packages read the toolchain from main."""
        toolchains["main"] = "lean4:v4.1.0"
        assert get_package_toolchain(_config(VersionStrategy.LATEST)) == (
            "lean4:v4.1.0",
            "main",
        )

    def test_latest_falls_back_to_master(self, toolchains):
        """If main has no toolchain, master is tried."""
        toolchains["master"] = "lean4:v4.0.0"
        assert get_package_toolchain(_config(VersionStrategy.LATEST)) == (
            "lean4:v4.0.0",
            "master",
        )
        assert toolchains["fetched"] == ["main", "master"]

    def test_latest_without_default_branch(self, toolchains):
        """Neither main nor master having a toolchain is an error."""
        with pytest.raises(RuntimeError, match="main or master for pkg"):
            get_package_toolchain(_config(VersionStrategy.LATEST))

    def test_tagged_uses_latest_tag(self, toolchains):
        """TAGGED packages read the toolchain at the latest tag."""
        toolchains["v2.0.0"] = "lean4:v4.2.0"
        assert get_package_toolchain(_config(VersionStrategy.TAGGED)) == (
            "lean4:v4.2.0",
            "v2.0.0",
        )


class TestUpdateLakefileDocgenVersion:
    """Tests for pinning doc-gen4 in a lakefile."""

    @pytest.mark.parametrize(
        "require",
        [
            f"require «doc-gen4» from git\n  {DOCGEN_URL}",
            f'require «doc-gen4» from git {DOCGEN_URL} @ "v4.0.0"',
        ],
    )
    def test_pins_version(self, tmp_path, require):
        """Unpinned and previously pinned requires are rewritten."""
        lakefile = tmp_path / "lakefile.lean"
        lakefile.write_text(f"import Lake\n{require}\nrequire mathlib\n")

        update_lakefile_docgen_version(lakefile, "v4.27.0")

        assert lakefile.read_text() == (
            "import Lake\nrequire «doc-gen4» from git\n"
            f'  {DOCGEN_URL} @ "v4.27.0"\nrequire mathlib\n'
        )

    def test_without_docgen_require_is_untouched(self, tmp_path):
        """Lakefiles without a doc-gen4 require are not rewritten."""
        lakefile = tmp_path / "lakefile.lean"
        lakefile.write_text("import Lake\n")
        before = lakefile.stat().st_mtime_ns

        update_lakefile_docgen_version(lakefile, "v4.27.0")

        assert lakefile.read_text() == "import Lake\n"
        assert lakefile.stat().st_mtime_ns == before
