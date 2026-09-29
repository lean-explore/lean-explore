"""Tests for shared torch device selection."""

from unittest.mock import patch

import pytest

from lean_explore.util.device import select_device


@pytest.mark.parametrize(
    ("cuda", "mps", "allow_mps", "expected"),
    [
        (True, True, True, "cuda"),
        (True, True, False, "cuda"),
        (False, True, True, "mps"),
        (False, True, False, "cpu"),
        (False, False, True, "cpu"),
    ],
)
def test_select_device(cuda, mps, allow_mps, expected):
    """CUDA wins, MPS only when allowed, CPU otherwise."""
    with patch("lean_explore.util.device.torch") as mock_torch:
        mock_torch.cuda.is_available.return_value = cuda
        mock_torch.backends.mps.is_available.return_value = mps
        assert select_device(allow_mps=allow_mps) == expected
