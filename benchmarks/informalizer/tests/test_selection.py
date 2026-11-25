"""Tests of query loading, file selection and the evaluation order."""

import json

from benchmarks.informalizer.queries import load_queries
from benchmarks.informalizer.sample import evaluation_order
from benchmarks.informalizer.select_files import coverage, select, stratify


def test_queries_load_in_both_formats(tmp_path):
    """Queries load in both formats."""
    version_one = {
        "id": "a",
        "query": "sum of squares",
        "bucket": "Natural language",
        "split": "test",
        "answers": [{"name": "X.y", "grade": 3}, {"name": "X.z", "grade": 1}],
    }
    earlier = {
        "id": "b",
        "query": "prove it",
        "query_type": "prose",
        "source": "zulip",
        "split": "development",
        "judgments": {"X.w": 2},
    }
    path = tmp_path / "queries.jsonl"
    path.write_text(
        "\n".join(json.dumps(r) for r in [version_one, earlier, version_one]) + "\n"
    )
    queries = load_queries([tmp_path])
    assert [q.identifier for q in queries] == ["a", "b"]
    assert queries[0].hits() == {"X.y"}
    assert queries[1].bucket == "Real questions"
    assert queries[1].hits() == {"X.w"}


def test_strata_hold_equal_shares_of_declarations():
    """Strata hold equal shares of declarations."""
    counts = {"a": 1, "b": 1, "c": 2, "d": 4, "e": 8}
    strata = stratify(counts, 2)
    assert strata == {"a": 0, "b": 0, "c": 0, "d": 0, "e": 1}


def test_selection_prefers_files_with_answers():
    """Selection prefers files with answers."""
    modules = {
        name: (f"{name}.lean", count)
        for name, count in {"a": 1, "b": 1, "c": 1, "d": 10, "e": 10, "f": 10}.items()
    }
    covered = coverage([], {})
    covered.update({"b": {"q1", "q2"}, "e": {"q3"}})
    selected, strata = select(modules, covered, files=2, strata=2, seed=0)
    assert {item.module for item in selected} == {"b", "e"}
    # The running total is 33, so the cut falls at the midpoint of `e` (18 > 16.5).
    assert [stratum.declarations for stratum in strata] == [13, 20]


def test_evaluation_order_interleaves_strata():
    """Evaluation order interleaves strata."""
    selection = {
        "files": [{"module": "M0", "stratum": 0}, {"module": "M1", "stratum": 1}]
    }
    references = [{"name": f"n{i}", "module": f"M{i % 2}"} for i in range(6)]
    order = evaluation_order(references, selection, seed=3)
    assert [r["module"] for r in order] == ["M0", "M1"] * 3
    assert order == evaluation_order(list(reversed(references)), selection, seed=3)
