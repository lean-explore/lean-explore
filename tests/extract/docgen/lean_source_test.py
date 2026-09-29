"""Tests for locating and reading Lean source text."""

import logging
from pathlib import Path

import pytest

from lean_explore.extract.docgen.lean_source import (
    SourceTextReader,
    build_package_cache,
    construct_source_link,
    extract_source_text,
    read_lean_toolchain_version,
    read_source_lines,
)
from tests.extract.docgen.fixtures import MATHLIB_URL, mathlib_package_dir, write_file

LEAN4_URL = "https://github.com/leanprover/lean4/blob/v4.29.0/"


@pytest.fixture
def fake_home(temp_directory, monkeypatch):
    """Point ``Path.home()`` at a temporary directory."""
    home = temp_directory / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


def _install_toolchain(home: Path, version: str) -> Path:
    """Create an elan toolchain source directory and return its src/lean."""
    path = home / ".elan" / "toolchains" / f"leanprover--lean4---{version}"
    lean_source = path / "src" / "lean"
    lean_source.mkdir(parents=True)
    return lean_source


class TestBuildPackageCache:
    """Tests for package cache building."""

    def test_collects_workspace_packages(self, temp_directory):
        """Test building package cache from workspace .lake/packages directories."""
        lean_root = temp_directory / "lean"
        packages = lean_root / "mathlib" / ".lake" / "packages"
        for name in ["mathlib4", "Qq", "batteries"]:
            (packages / name).mkdir(parents=True)
        (packages / "stray-file").write_text("not a package")

        cache = build_package_cache(lean_root)

        assert cache == {
            "mathlib4": packages / "mathlib4",
            "qq": packages / "Qq",  # Lowercased key
            "batteries": packages / "batteries",
        }

    def test_empty_directory(self, temp_directory):
        """Test building cache from directory with no packages."""
        lean_root = temp_directory / "lean"
        lean_root.mkdir()

        assert build_package_cache(lean_root) == {}

    def test_workspace_scoping(self, temp_directory):
        """Test that naming a workspace excludes other workspaces' packages."""
        lean_root = temp_directory / "lean"
        (lean_root / "mathlib" / ".lake" / "packages" / "batteries").mkdir(parents=True)
        (lean_root / "flt" / ".lake" / "packages" / "flt-deps").mkdir(parents=True)

        assert set(build_package_cache(lean_root, "flt")) == {"flt-deps"}
        assert set(build_package_cache(lean_root)) == {"batteries", "flt-deps"}

    def test_adds_installed_toolchain(self, temp_directory, fake_home):
        """Test that the workspace's elan toolchain is cached as lean4."""
        lean_root = temp_directory / "lean"
        write_file(lean_root / "flt" / "lean-toolchain", "leanprover/lean4:v4.24.0\n")
        lean_source = _install_toolchain(fake_home, "v4.24.0")

        assert build_package_cache(lean_root, "flt") == {"lean4": lean_source}

    def test_skips_uninstalled_toolchain(self, temp_directory, fake_home):
        """Test that the first workspace with an installed toolchain wins."""
        lean_root = temp_directory / "lean"
        write_file(lean_root / "mathlib" / "lean-toolchain", "leanprover/lean4:v1\n")
        write_file(lean_root / "flt" / "lean-toolchain", "leanprover/lean4:v2\n")
        lean_source = _install_toolchain(fake_home, "v2")

        assert build_package_cache(lean_root)["lean4"] == lean_source
        assert "lean4" not in build_package_cache(lean_root, "mathlib")


class TestReadSourceLines:
    """Tests for reading line ranges from source files."""

    def test_reads_inclusive_range(self, temp_directory):
        """Test reading specific lines from a source file."""
        source_file = write_file(temp_directory / "test.lean", "l1\nl2\nl3\nl4\nl5\n")

        assert read_source_lines(source_file, 2, 4) == "l2\nl3\nl4\n"

    def test_out_of_bounds(self, temp_directory):
        """Test that reading out-of-bounds lines raises an error."""
        source_file = write_file(temp_directory / "test.lean", "line 1\nline 2\n")

        with pytest.raises(ValueError, match="out of bounds"):
            read_source_lines(source_file, 1, 10)

    def test_extends_attribute_to_declaration(self, temp_directory):
        """Test that an attribute-only range is extended to its declaration."""
        source_file = write_file(
            temp_directory / "test.lean",
            "@[simp]\n@[norm_cast]\ntheorem foo : True := trivial\n-- after\n",
        )

        assert read_source_lines(source_file, 1, 1) == (
            "@[simp]\n@[norm_cast]\ntheorem foo : True := trivial"
        )

    def test_attribute_at_end_of_file(self, temp_directory):
        """Test that an attribute with no following declaration reads to EOF."""
        source_file = write_file(temp_directory / "test.lean", "x\n@[simp]\n\n")

        assert read_source_lines(source_file, 2, 2) == "@[simp]"


class TestExtractSourceText:
    """Tests for resolving GitHub source links to local files."""

    def test_from_package(self, temp_directory):
        """Test extracting source text using package cache."""
        lean_root = temp_directory / "lean"
        text = "def length : List α → Nat\n  | [] => 0\n  | _ :: xs => 1\n"
        write_file(mathlib_package_dir(lean_root) / "Mathlib/Data/List.lean", text)
        link = MATHLIB_URL + "Mathlib/Data/List.lean#L1-L3"

        result = extract_source_text(link, lean_root, build_package_cache(lean_root))

        assert result == text

    def test_from_lean_root(self, temp_directory):
        """Test extracting source text from lean root directory."""
        lean_root = temp_directory / "lean"
        write_file(lean_root / "MyProject/Basic.lean", "theorem t : True := trivial\n")
        link = "https://github.com/me/myproject/blob/main/MyProject/Basic.lean#L1-L1"

        assert extract_source_text(link, lean_root, {}) == (
            "theorem t : True := trivial\n"
        )

    @pytest.mark.parametrize(
        ("repository", "cache_key"),
        [("mathlib4", "mathlib"), ("formal-conjectures", "formalconjectures")],
    )
    def test_package_name_variants(self, temp_directory, repository, cache_key):
        """Test trailing digits and dashes are stripped to find the package."""
        package_dir = temp_directory / "pkg"
        write_file(package_dir / "A.lean", "def a := 1\n")
        link = f"https://github.com/org/{repository}/blob/main/A.lean#L1-L1"

        result = extract_source_text(link, temp_directory, {cache_key: package_dir})

        assert result == "def a := 1\n"

    def test_falls_back_to_any_package(self, temp_directory):
        """Test that other cached packages are searched as a last resort."""
        other_dir = temp_directory / "other"
        write_file(other_dir / "B.lean", "def b := 2\n")
        link = "https://github.com/org/unknown/blob/main/B.lean#L1-L1"

        assert extract_source_text(link, temp_directory, {"x": other_dir}) == (
            "def b := 2\n"
        )

    @pytest.mark.parametrize(
        ("repository_path", "local_path"),
        [
            ("src/lean/Init/Core.lean", "lean/Init/Core.lean"),
            ("src/lake/Lake/Build.lean", "lake/Lake/Build.lean"),
            ("src/Std/Sat.lean", "lean/Std/Sat.lean"),
        ],
    )
    def test_lean4_toolchain_paths(self, temp_directory, repository_path, local_path):
        """Test toolchain repository paths map into the elan src layout."""
        toolchain_src = temp_directory / "toolchain" / "src"
        write_file(toolchain_src / local_path, "def core := 0\n")
        link = f"{LEAN4_URL}{repository_path}#L1-L1"
        package_cache = {"lean4": toolchain_src / "lean"}

        result = extract_source_text(link, temp_directory, package_cache)

        assert result == "def core := 0\n"

    def test_invalid_link(self, temp_directory):
        """Test that invalid source links raise an error."""
        with pytest.raises(ValueError, match="Could not parse source link"):
            extract_source_text("https://example.com/nope", temp_directory, {})

    def test_file_not_found(self, temp_directory):
        """Test that missing source files raise an error."""
        link = "https://github.com/user/repo/blob/main/NonExistent.lean#L1-L1"

        with pytest.raises(FileNotFoundError, match="NonExistent.lean"):
            extract_source_text(link, temp_directory, {})


class TestSourceTextReader:
    """Tests for the error-counting source reader."""

    def test_reads_source(self, temp_directory):
        """Test that successful reads return text and count no errors."""
        write_file(temp_directory / "A.lean", "def a := 1\n")
        reader = SourceTextReader(temp_directory, {})

        text = reader.read("https://github.com/o/r/blob/m/A.lean#L1-L1", "a")

        assert text == "def a := 1\n"
        assert reader.errors == 0

    def test_counts_and_logs_failures(self, temp_directory, caplog):
        """Test failures return None, log the first ten, and are summarized."""
        reader = SourceTextReader(temp_directory, {})

        with caplog.at_level(logging.DEBUG, logger="lean_explore.extract"):
            results = [reader.read("bad link", f"decl{i}") for i in range(12)]
            reader.log_summary()

        assert results == [None] * 12
        assert reader.errors == 12
        assert caplog.text.count("Could not extract source for") == 10
        assert "Could not extract source text for 12 declarations" in caplog.text

    def test_summary_silent_without_errors(self, temp_directory, caplog):
        """Test that no warning is logged when every read succeeded."""
        with caplog.at_level(logging.DEBUG, logger="lean_explore.extract"):
            SourceTextReader(temp_directory, {}).log_summary()

        assert caplog.text == ""


class TestReadLeanToolchainVersion:
    """Tests for reading a workspace's lean-toolchain version."""

    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            ("leanprover/lean4:v4.29.0-rc6\n", "v4.29.0-rc6"),
            ("leanprover/lean4:v4.29.0\n", "v4.29.0"),
            ("leanprover/lean4:nightly\n", None),
        ],
    )
    def test_parses_version(self, temp_directory, content, expected):
        """Test reading release, release-candidate, and unversioned toolchains."""
        write_file(temp_directory / "lean-toolchain", content)

        assert read_lean_toolchain_version(temp_directory) == expected

    def test_missing_file(self, temp_directory):
        """Test returns None when lean-toolchain doesn't exist."""
        assert read_lean_toolchain_version(temp_directory) is None

    def test_unreadable_file(self, temp_directory):
        """Test returns None when lean-toolchain cannot be read."""
        (temp_directory / "lean-toolchain").mkdir()

        assert read_lean_toolchain_version(temp_directory) is None


class TestConstructSourceLink:
    """Tests for building source links from api-docs.db data."""

    @pytest.mark.parametrize(
        ("module", "version", "expected_prefix"),
        [
            ("Init.Data.Nat.Basic", "v4.29.0-rc6", "v4.29.0-rc6/src/lean/Init/Data"),
            ("Lean.Elab", "v4.29.0", "v4.29.0/src/lean/Lean"),
            ("Std.Sat", None, "master/src/lean/Std"),
            ("Lake.Config.Monad", "v4.29.0-rc6", "v4.29.0-rc6/src/lake/Lake/Config"),
        ],
    )
    def test_core_fallback(self, module, version, expected_prefix):
        """Test core and Lake modules link into the lean4 repository."""
        result = construct_source_link(module, None, 12, 15, lean_version=version)

        assert result.startswith(
            f"https://github.com/leanprover/lean4/blob/{expected_prefix}"
        )
        assert result.endswith(".lean#L12-L15")

    def test_prefers_source_url(self):
        """Test that a non-None source_url is used directly."""
        url = MATHLIB_URL + "Foo.lean"

        assert construct_source_link("Init.Foo", url, 1, 10) == f"{url}#L1-L10"

    def test_non_core_without_url(self):
        """Test that non-core modules without a source URL have no link."""
        assert construct_source_link("Aesop.Frontend", None, 1, 2) is None
