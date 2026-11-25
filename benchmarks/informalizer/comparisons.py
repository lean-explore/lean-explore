"""Which runs are compared, and how differences between them are tested.

Run labels have the form ``<model>-<effort>/<arm>``, for example
``deepseek-low/per-file``. By default every run is compared with the published
descriptions, the per-file arm with the current arm of the same model and
effort, and the low effort with the default effort of the same model and arm.
"""

import numpy as np


def default_pairs(labels: list[str]) -> list[str]:
    """The comparisons to report for a set of run labels, as ``first:second``."""
    present = set(labels)
    pairs = [f"{label}:published" for label in labels if label != "published"]
    for label in labels:
        if label.endswith("/per-file"):
            current = label.removesuffix("/per-file") + "/current"
            if current in present:
                pairs.append(f"{label}:{current}")
    for label in labels:
        model_effort, _, arm = label.partition("/")
        if model_effort.endswith("-low"):
            default = model_effort.removesuffix("-low") + "-default/" + arm
            if default in present:
                pairs.append(f"{label}:{default}")
    return pairs


def bootstrap_interval(differences: np.ndarray, seed: int = 0) -> tuple[float, float]:
    """Paired bootstrap 95% interval of a mean difference."""
    generator = np.random.default_rng(seed)
    samples = generator.integers(0, len(differences), (10_000, len(differences)))
    means = differences[samples].mean(axis=1)
    return float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))
