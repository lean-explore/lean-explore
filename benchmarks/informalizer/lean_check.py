"""Check statements in Lean with the reference implementation of BEq+.

BEq+ ("Reliable Evaluation and Benchmarks for Statement Autoformalization",
EMNLP 2025) decides whether two theorem statements are equivalent by proving
each from the other in Lean. On ProofNetVerif its precision against human
labels is 98.0 and its recall 48.3: an equivalence it finds is almost always
real, and it misses about half, so it ranks systems but understates accuracy.

The benchmark does not copy BEq+. It downloads ``examples/beq_plus.py`` of
LeanInteract at ``REFERENCE_COMMIT``, checks the file's SHA-256 and imports it
unchanged, so the check is exactly the published one. That commit includes the
authors' later fix for statements provable by assumption alone. Importing the
file needs its own dependencies: ``datasets``, ``rich``, ``scikit-learn`` and
``tqdm``.
"""

import hashlib
import importlib.util
import urllib.request
from pathlib import Path
from types import ModuleType

REFERENCE_COMMIT = "768e18ebcb5c7ec34d308768a785389eeac37798"
"""LeanInteract commit the reference implementation is read from."""

REFERENCE_SHA256 = "3981daf6b342b9959c183a55df53818f57cf83dba8c67376105ec2d76d9a80c6"
"""SHA-256 of ``examples/beq_plus.py`` at ``REFERENCE_COMMIT``."""

REFERENCE_URL = (
    "https://raw.githubusercontent.com/augustepoiroux/LeanInteract/"
    "{commit}/examples/beq_plus.py"
)

HEADER = "import Mathlib\nopen BigOperators Real Nat Topology"
"""The header every statement is elaborated under."""

HEADER_TIMEOUT = 900
"""Seconds allowed for the first import of Mathlib in a new server."""


def reference_path(directory: Path) -> Path:
    """Download the reference file into ``directory`` once and verify it.

    Raises:
        RuntimeError: If the file on disk does not match ``REFERENCE_SHA256``.
    """
    path = directory / f"beq_plus-{REFERENCE_COMMIT[:12]}.py"
    if not path.exists():
        directory.mkdir(parents=True, exist_ok=True)
        url = REFERENCE_URL.format(commit=REFERENCE_COMMIT)
        with urllib.request.urlopen(url, timeout=60) as response:
            path.write_bytes(response.read())
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != REFERENCE_SHA256:
        raise RuntimeError(f"{path} is not the pinned BEq+ reference: {digest}")
    return path


def load_reference(path: Path) -> ModuleType:
    """Import the verified reference file as a module."""
    spec = importlib.util.spec_from_file_location("beq_plus_reference", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LeanWorker:
    """One Lean server with Mathlib imported, and the reference BEq+."""

    def __init__(self, mathlib: Path, reference: Path, timeout: int):
        """Start the server and elaborate the header once.

        Args:
            mathlib: Mathlib checkout at the pinned commit, with its cache.
            reference: Verified path from ``reference_path``.
            timeout: Seconds allowed for each Lean command after the first.
        """
        from lean_interact import AutoLeanServer, Command, LeanREPLConfig
        from lean_interact.project import LocalProject

        self.reference = load_reference(reference)
        project = LocalProject(directory=str(mathlib), auto_build=False)
        self.server = AutoLeanServer(LeanREPLConfig(project=project))
        self.timeout = timeout
        # Later commands start with the same header and reuse its elaboration.
        self.server.run(Command(cmd=HEADER), timeout=HEADER_TIMEOUT)

    def typechecks(self, theorem: str) -> bool:
        """True if a theorem statement elaborates under the header."""
        from lean_interact import Command
        from lean_interact.interface import LeanError

        try:
            response = self.server.run(
                Command(cmd=f"{HEADER}\n\n{theorem}"), timeout=self.timeout
            )
        except TimeoutError:
            return False
        return not isinstance(response, LeanError) and response.lean_code_is_valid()

    def equivalent(self, first: str, second: str) -> bool:
        """BEq+ on two theorem statements."""
        return bool(
            self.reference.beq_plus(first, second, HEADER, self.server, self.timeout)
        )
