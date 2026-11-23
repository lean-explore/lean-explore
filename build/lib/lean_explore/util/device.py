"""Torch device selection shared by the local model clients."""

import torch


def select_device(allow_mps: bool = True) -> str:
    """Select the best available torch device.

    Args:
        allow_mps: Whether Apple's Metal (MPS) backend may be chosen when CUDA
            is unavailable.

    Returns:
        "cuda" if available, otherwise "mps" when allowed and available,
        otherwise "cpu".
    """
    if torch.cuda.is_available():
        return "cuda"
    if allow_mps and torch.backends.mps.is_available():
        return "mps"
    return "cpu"
