"""Tests for the context files of a source package: toolchain and dependencies."""

import pytest

from lean_explore.extract.packages.config import PackageConfig
from lean_explore.extract.source.context import (
    context_source_files,
    toolchain_source_directory,
)
from lean_explore.extract.source.reader import parse_package_sources

BASE = "https://github.com/owner/pkg/blob/abc123/"

CORE_NOTATION = """prelude
class HAdd (α β γ) where
  hAdd : α → β → γ
inductive Eq : α → α → Prop
infixl:65 " + " => HAdd.hAdd
infix:50 " = " => Eq
"""

DEPENDENCY = """structure Dep.Point where
  x : Nat
def Dep.Point.norm (p : Dep.Point) : Nat := p.x
def Dep.Reals : Type := Nat
notation "ℝ'" => Dep.Reals
"""

PACKAGE = """import Dep.Basic
theorem Pkg.add_comm (a b : ℝ') : a + b = b + a := sorry
theorem Pkg.norm_eq (p : Dep.Point) : p.norm = p.norm := sorry
"""


def _write(root, files):
    """Write files below a directory and return their paths."""
    paths = []
    for relative_path, text in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        paths.append(path)
    return paths


@pytest.fixture
def elan_home(tmp_path, monkeypatch):
    """An elan directory with the sources of one installed toolchain."""
    home = tmp_path / "elan"
    monkeypatch.setenv("ELAN_HOME", str(home))
    sources = home / "toolchains" / "leanprover--lean4---v4.36.0" / "src" / "lean"
    _write(sources, {"Init.lean": CORE_NOTATION})
    return home


class TestToolchainSourceDirectory:
    """The toolchain a package names is looked up where elan installs it."""

    def test_installed_toolchain(self, tmp_path, elan_home):
        """The directory name follows elan's spelling of the toolchain."""
        _write(tmp_path / "pkg", {"lean-toolchain": "leanprover/lean4:v4.36.0\n"})
        directory = toolchain_source_directory(tmp_path / "pkg")
        assert directory == (
            elan_home / "toolchains" / "leanprover--lean4---v4.36.0" / "src" / "lean"
        )

    def test_missing_toolchain_or_file(self, tmp_path, elan_home):
        """No toolchain file, an empty one or one not installed gives nothing."""
        assert toolchain_source_directory(tmp_path / "missing") is None
        _write(tmp_path / "empty", {"lean-toolchain": "\n"})
        assert toolchain_source_directory(tmp_path / "empty") is None
        _write(tmp_path / "other", {"lean-toolchain": "leanprover/lean4:v4.1.0"})
        assert toolchain_source_directory(tmp_path / "other") is None


class TestContextSourceFiles:
    """Context files come from the toolchain and from the dependencies."""

    def test_toolchain_and_dependencies(self, tmp_path, elan_home):
        """Built and source dependencies both contribute their Lean files."""
        lean_root = tmp_path / "lean"
        _write(
            lean_root / "pkg" / "source",
            {"lean-toolchain": "leanprover/lean4:v4.36.0", "Pkg.lean": "def a := 1\n"},
        )
        built = lean_root / "built" / ".lake" / "packages" / "dep"
        _write(built, {"Dep/Basic.lean": DEPENDENCY, "README.md": "x"})
        _write(lean_root / "plain" / "source", {"Plain.lean": "def b := 1\n"})
        config = PackageConfig(
            name="pkg",
            git_url="u",
            module_prefixes=["Pkg"],
            depends_on=["built", "plain", "absent"],
        )
        files = context_source_files(lean_root, config)
        relative = [str(path.relative_to(tmp_path)) for path in files]
        assert relative == [
            "elan/toolchains/leanprover--lean4---v4.36.0/src/lean/Init.lean",
            "lean/built/.lake/packages/dep/Dep/Basic.lean",
            "lean/plain/source/Plain.lean",
        ]

    def test_dependencies_of_dependencies(self, tmp_path, monkeypatch):
        """A registered dependency adds its own dependencies and subdirectory."""
        lean_root = tmp_path / "lean"
        _write(lean_root / "core" / "source", {"src/Init.lean": "", "tests/T.lean": ""})
        _write(lean_root / "middle" / "source", {"Middle.lean": ""})
        registry = {
            "core": PackageConfig(
                name="core", git_url="u", module_prefixes=[], source_subdirectory="src"
            ),
            "middle": PackageConfig(
                name="middle", git_url="u", module_prefixes=[], depends_on=["core"]
            ),
        }
        monkeypatch.setattr(
            "lean_explore.extract.packages.registry.PACKAGE_REGISTRY", registry
        )
        config = PackageConfig(
            name="pkg", git_url="u", module_prefixes=["Pkg"], depends_on=["middle"]
        )

        files = context_source_files(lean_root, config)

        assert [str(path.relative_to(lean_root)) for path in files] == [
            "middle/source/Middle.lean",
            "core/source/src/Init.lean",
        ]

    def test_nothing_present(self, tmp_path):
        """A package without toolchain or dependency sources has no context."""
        config = PackageConfig(name="pkg", git_url="u", module_prefixes=["Pkg"])
        assert context_source_files(tmp_path, config) == []


class TestContextInReferences:
    """Context files supply notations and the declarations of fields."""

    @pytest.fixture
    def layout(self, tmp_path):
        """A package with a dependency and a toolchain beside it."""
        pytest.importorskip("lean_source_parser")
        _write(tmp_path / "pkg", {"Pkg.lean": PACKAGE})
        context = _write(
            tmp_path / "context",
            {"lean/Init.lean": CORE_NOTATION, "dep/Dep/Basic.lean": DEPENDENCY},
        )
        return tmp_path / "pkg", context

    def _dependencies(self, root, known, context_paths):
        rows = parse_package_sources(
            root,
            BASE,
            lambda module: True,
            known_names=known,
            context_paths=context_paths,
        )
        return {row.name: row.dependencies for row in rows}

    def test_notation_and_fields_resolve_through_context(self, layout):
        """Tokens and fields refer to the known names they stand for."""
        root, context = layout
        known = ["Eq", "HAdd.hAdd", "Dep.Reals", "Dep.Point", "Dep.Point.norm"]
        found = self._dependencies(root, known, context)
        assert found["Pkg.add_comm"] == ["Dep.Reals", "HAdd.hAdd", "Eq"]
        assert found["Pkg.norm_eq"] == ["Dep.Point", "Dep.Point.norm", "Eq"]

    def test_names_that_are_not_stored_are_dropped(self, layout):
        """A constant found through notation is kept only if it is known."""
        root, context = layout
        found = self._dependencies(root, ["Eq", "Dep.Point"], context)
        assert found["Pkg.add_comm"] == ["Eq"]
        assert found["Pkg.norm_eq"] == ["Dep.Point", "Eq"]

    def test_without_context_tokens_stand_for_nothing(self, layout):
        """The same package without context files has identifiers only."""
        root, _ = layout
        known = ["Eq", "HAdd.hAdd", "Dep.Reals", "Dep.Point", "Dep.Point.norm"]
        found = self._dependencies(root, known, [])
        assert found["Pkg.add_comm"] is None
        assert found["Pkg.norm_eq"] == ["Dep.Point", "Dep.Point.norm"]
