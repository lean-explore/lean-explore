"""Tests for lazy attribute loading in ``lean_explore.util``."""

import os
import subprocess
import sys

import pytest

import lean_explore.util as util


@pytest.mark.parametrize(
    ("name", "module"),
    [
        ("EmbeddingClient", "lean_explore.util.embedding_client"),
        ("RerankerClient", "lean_explore.util.reranker_client"),
        ("OpenRouterClient", "lean_explore.util.openrouter_client"),
        ("setup_logging", "lean_explore.util.logging"),
    ],
)
def test_lazy_attributes_resolve_to_real_objects(name, module):
    """Each exported name resolves to the object defined in its submodule."""
    obj = getattr(util, name)
    assert obj is getattr(sys.modules[module], name)


def test_unknown_attribute_raises_attribute_error():
    """Unknown names raise AttributeError, so hasattr() works."""
    with pytest.raises(AttributeError, match="no attribute 'Nope'"):
        util.Nope  # noqa: B018
    assert not hasattr(util, "Nope")


def test_all_lists_the_lazy_exports():
    """__all__ matches the names the lazy loader serves."""
    assert sorted(util.__all__) == sorted(
        ["EmbeddingClient", "RerankerClient", "OpenRouterClient", "setup_logging"]
    )


def test_import_does_not_load_torch():
    """Importing the package (and setup_logging) must not import torch."""
    code = (
        "import sys, lean_explore.util as u; u.setup_logging; "
        "print('torch' in sys.modules, 'sentence_transformers' in sys.modules)"
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        check=True,
        timeout=60,
    )
    assert result.stdout.strip() == "False False"
