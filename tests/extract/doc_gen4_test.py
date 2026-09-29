"""Tests for doc-gen4 workspace orchestration.

Lake is never invoked: ``subprocess`` and ``time`` are replaced with fakes
that record commands and sleeps.
"""

from types import SimpleNamespace

import pytest

from lean_explore.extract import doc_gen4
from lean_explore.extract.doc_gen4 import (
    _clear_workspace_cache,
    _docgen_facet,
    _get_library_names,
    _run_lake_build_target,
    _run_lake_for_package,
    _run_lake_update_with_retry,
    _setup_workspace,
    _uses_sqlite_docgen,
    run_doc_gen4,
)
from lean_explore.extract.package_registry import PACKAGE_REGISTRY

SQLITE_TOOLCHAIN = "leanprover/lean4:v4.29.0-rc2"
LEGACY_TOOLCHAIN = "leanprover/lean4:v4.29.0-rc1"


class FakeSubprocess:
    """Stand-in for the ``subprocess`` module recording every command."""

    PIPE = STDOUT = None

    def __init__(self, run_codes=(), popen_codes=()):
        """Queue return codes for successive ``run`` / ``Popen`` calls."""
        self.run_codes = list(run_codes)
        self.popen_codes = list(popen_codes)
        self.commands: list[list[str]] = []

    def run(self, command, **kwargs):
        """Record a ``subprocess.run`` call."""
        self.commands.append(command)
        code = self.run_codes.pop(0) if self.run_codes else 0
        return SimpleNamespace(returncode=code, stdout="out", stderr="err")

    def Popen(self, command, **kwargs):  # noqa: N802 - mirrors subprocess API
        """Record a ``subprocess.Popen`` call."""
        self.commands.append(command)
        code = self.popen_codes.pop(0) if self.popen_codes else 0
        return SimpleNamespace(stdout=iter(["line\n"]), wait=lambda: code)


@pytest.fixture
def fake_subprocess(monkeypatch) -> FakeSubprocess:
    """Install a FakeSubprocess that always succeeds."""
    fake = FakeSubprocess()
    monkeypatch.setattr(doc_gen4, "subprocess", fake)
    return fake


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    """Record retry delays instead of sleeping."""
    delays: list[float] = []
    monkeypatch.setattr(doc_gen4, "time", SimpleNamespace(sleep=delays.append))
    return delays


@pytest.fixture
def workspaces(tmp_path, monkeypatch):
    """Point package workspaces at a temporary directory."""
    monkeypatch.setattr(doc_gen4, "WORKSPACES_ROOT", tmp_path)
    return tmp_path


def _make_workspace(root, name, toolchain=None):
    workspace = root / name
    workspace.mkdir(parents=True)
    if toolchain is not None:
        (workspace / "lean-toolchain").write_text(toolchain + "\n")
    return workspace


class TestDocGen4VersionDetection:
    """Tests for doc-gen4 format detection by Lean version."""

    @pytest.mark.parametrize(
        ("toolchain", "expected"),
        [
            (SQLITE_TOOLCHAIN, True),
            ("leanprover/lean4:v4.29.0-rc10", True),
            ("leanprover/lean4:v4.29.0", True),
            ("leanprover/lean4:v4.30.0-rc1", True),
            (LEGACY_TOOLCHAIN, False),
            ("leanprover/lean4:v4.28.0", False),
        ],
    )
    def test_sqlite_cutoff_at_v4_29_0_rc2(self, toolchain, expected):
        """SQLite output starts with v4.29.0-rc2."""
        assert _uses_sqlite_docgen(toolchain) is expected

    def test_docgen_facet_from_toolchain(self, tmp_path):
        """The facet follows the pinned toolchain, defaulting to :docs."""
        assert _docgen_facet(_make_workspace(tmp_path, "none")) == "docs"
        assert _docgen_facet(_make_workspace(tmp_path, "empty", "")) == "docs"
        legacy = _make_workspace(tmp_path, "legacy", LEGACY_TOOLCHAIN)
        assert _docgen_facet(legacy) == "docs"
        sqlite = _make_workspace(tmp_path, "sqlite", SQLITE_TOOLCHAIN)
        assert _docgen_facet(sqlite) == "docInfo"


class TestWorkspaceHelpers:
    """Tests for small workspace helpers."""

    def test_clear_workspace_cache(self, tmp_path):
        """Manifest and .lake are removed; other files are kept."""
        (tmp_path / "lake-manifest.json").write_text("{}")
        (tmp_path / ".lake" / "build").mkdir(parents=True)
        (tmp_path / "lakefile.lean").write_text("")

        _clear_workspace_cache(tmp_path)
        _clear_workspace_cache(tmp_path)  # idempotent

        assert [p.name for p in tmp_path.iterdir()] == ["lakefile.lean"]

    def test_library_names(self):
        """Known packages map to custom wrappers; others get a default."""
        assert _get_library_names("mathlib") == ["MathExtract"]
        assert _get_library_names("formal-conjectures") == [
            "FormalConjectures",
            "FormalConjecturesForMathlib",
        ]
        assert _get_library_names("foo") == ["FooExtract"]

    def test_setup_workspace(self, workspaces, monkeypatch):
        """Setup pins doc-gen4 to the toolchain and writes lean-toolchain."""
        workspace = _make_workspace(workspaces, "flt")
        (workspace / "lakefile.lean").write_text(
            'require «doc-gen4» from git\n  "https://github.com/leanprover/doc-gen4"'
        )
        monkeypatch.setattr(
            doc_gen4,
            "get_package_toolchain",
            lambda config: ("leanprover/lean4:v4.27.0", "main"),
        )

        assert _setup_workspace(PACKAGE_REGISTRY["flt"]) == (
            "leanprover/lean4:v4.27.0",
            "main",
        )
        assert '@ "v4.27.0"' in (workspace / "lakefile.lean").read_text()
        assert (workspace / "lean-toolchain").read_text() == (
            "leanprover/lean4:v4.27.0\n"
        )


class TestLakeCommands:
    """Tests for Lake subprocess wrappers."""

    def test_build_target_success(self, fake_subprocess, tmp_path):
        """A successful build returns True."""
        assert _run_lake_build_target(tmp_path, "p", "Lib:docs", {}) is True
        assert fake_subprocess.commands == [["lake", "build", "Lib:docs"]]

    def test_build_target_failure_raises(self, fake_subprocess, tmp_path):
        """A failed build raises unless failure is allowed."""
        fake_subprocess.popen_codes = [1]
        with pytest.raises(RuntimeError, match="lake build failed for p"):
            _run_lake_build_target(tmp_path, "p", "Lib:docs", {})

    def test_build_target_failure_allowed(self, fake_subprocess, tmp_path):
        """With allow_failure a failed build returns False."""
        fake_subprocess.popen_codes = [1]
        assert not _run_lake_build_target(tmp_path, "p", "L", {}, allow_failure=True)

    def test_update_retries_with_backoff(self, fake_subprocess, sleeps, tmp_path):
        """Transient failures are retried with doubling delays."""
        fake_subprocess.run_codes = [128, 128, 0]
        _run_lake_update_with_retry(tmp_path, "p", {}, verbose=True)
        assert sleeps == [30.0, 60.0]
        assert fake_subprocess.commands == [["lake", "update"]] * 3

    def test_update_gives_up(self, fake_subprocess, sleeps, tmp_path):
        """After max_retries the failure is raised."""
        fake_subprocess.run_codes = [1, 1, 1]
        with pytest.raises(RuntimeError, match="lake update failed for p"):
            _run_lake_update_with_retry(tmp_path, "p", {}, max_retries=2)
        assert sleeps == [30.0, 60.0]


class TestRunLakeForPackage:
    """Tests for the per-package Lake command sequence."""

    def test_command_sequence(self, fake_subprocess, workspaces):
        """Update, fetch cache, then build the SQLite :docInfo facet."""
        _make_workspace(workspaces, "physlean", SQLITE_TOOLCHAIN)

        _run_lake_for_package("physlean")

        assert fake_subprocess.commands == [
            ["lake", "update"],
            ["lake", "exe", "cache", "get"],
            ["lake", "build", "PhysExtract:docInfo"],
        ]

    def test_failures_after_update_are_tolerated(self, fake_subprocess, workspaces):
        """Cache and build failures do not abort (stale docs are reused)."""
        _make_workspace(workspaces, "formal-conjectures", LEGACY_TOOLCHAIN)
        fake_subprocess.run_codes = [0, 1]
        fake_subprocess.popen_codes = [1, 1]

        _run_lake_for_package("formal-conjectures", verbose=True)

        assert fake_subprocess.commands[2:] == [
            ["lake", "build", "FormalConjectures:docs"],
            ["lake", "build", "FormalConjecturesForMathlib:docs"],
        ]


@pytest.fixture
def recorded_run(monkeypatch) -> list:
    """Record setup, cache clears, and Lake runs performed by run_doc_gen4."""
    calls: list = []
    toolchains = {"mathlib": LEGACY_TOOLCHAIN, "flt": SQLITE_TOOLCHAIN}

    def setup(config):
        calls.append(("setup", config.name))
        return toolchains[config.name], "main"

    monkeypatch.setattr(doc_gen4, "_setup_workspace", setup)
    monkeypatch.setattr(
        doc_gen4, "_clear_workspace_cache", lambda path: calls.append(("clear", path))
    )
    monkeypatch.setattr(
        doc_gen4,
        "_run_lake_for_package",
        lambda name, verbose: calls.append(("lake", name)),
    )
    monkeypatch.setattr(doc_gen4, "WORKSPACES_ROOT", doc_gen4.Path("ws"))
    return calls


class TestRunDocGen4:
    """Tests for per-package setup and fresh-cache handling."""

    async def test_fresh_clears_only_legacy_workspaces(self, recorded_run):
        """Legacy BMP workspaces are cleared; SQLite workspaces are not."""
        await run_doc_gen4(["mathlib", "flt"], fresh=True)

        assert recorded_run == [
            ("setup", "mathlib"),
            ("clear", doc_gen4.Path("ws/mathlib")),
            ("lake", "mathlib"),
            ("setup", "flt"),
            ("lake", "flt"),
        ]

    async def test_not_fresh_keeps_cache(self, recorded_run):
        """Without fresh the workspace is set up but never cleared."""
        await run_doc_gen4(["mathlib"])
        assert recorded_run == [("setup", "mathlib"), ("lake", "mathlib")]

    async def test_fresh_without_setup_always_clears(self, recorded_run):
        """Without setup the toolchain is unknown, so the cache is cleared."""
        await run_doc_gen4(["flt"], setup=False, fresh=True)
        assert recorded_run == [("clear", doc_gen4.Path("ws/flt")), ("lake", "flt")]

    async def test_defaults_to_extraction_order(self, recorded_run):
        """All registry packages are processed, mathlib first."""
        await run_doc_gen4(setup=False)
        lake_runs = [name for kind, name in recorded_run if kind == "lake"]
        assert lake_runs[0] == "mathlib"
        assert sorted(lake_runs) == sorted(PACKAGE_REGISTRY)

    async def test_unknown_package(self, recorded_run):
        """Unknown packages are rejected."""
        with pytest.raises(ValueError, match="Unknown package: nope"):
            await run_doc_gen4(["nope"])
