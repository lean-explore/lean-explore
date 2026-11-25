"""Compare search quality of the runs with dense retrieval over the whole index.

Every declaration outside the benchmark files keeps its published embedding.
The declarations of the benchmark files are embedded again from each run's
descriptions, with the production embedding model, and the queries of
lean-explore-bench whose answers lie in those files are searched against the
result. The published descriptions are embedded again the same way, so that
every arm is compared on vectors from the same local model run.

This scores the dense channel only. Production adds name search, a dependency
boost and a reranker on top; the dense channel is the one that reads the
descriptions, and the best single channel on the benchmark.

Metrics follow lean-explore-bench: Success@k counts answers of grade 2 or 3,
nDCG@10 uses the grade as a linear gain. Differences between runs come with a
paired bootstrap 95% interval over queries.

Usage:
    python -m benchmarks.informalizer.score_search --database lean_explore.db \
        --inputs inputs --queries bench/queries \
        --run per-file=runs/deepseek/per-file --run current=runs/deepseek/current \
        --compare per-file:current --out results/search
"""

import argparse
import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from benchmarks.informalizer.comparisons import bootstrap_interval, default_pairs
from benchmarks.informalizer.corpus import connect
from benchmarks.informalizer.inputs import load_descriptions, load_targets
from benchmarks.informalizer.queries import BenchmarkQuery, load_queries

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "Qwen/Qwen3-Embedding-0.6B"
"""The embedding model of the extraction pipeline."""

MAX_SEQUENCE_LENGTH = 512
"""The document length limit of the extraction pipeline."""

DEPTH = 10


@dataclass(frozen=True)
class Index:
    """Published embeddings of every declaration outside the benchmark files."""

    names: list[str]
    vectors: np.ndarray


def normalize(vectors: np.ndarray) -> np.ndarray:
    """Scale every row to unit length."""
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.maximum(norms, 1e-12)


def load_index(database: Path, excluded: set[str]) -> Index:
    """Read the published embeddings, leaving out the excluded names."""
    connection = connect(database)
    query = (
        "SELECT name, informalization_embedding FROM declarations "
        "WHERE informalization_embedding IS NOT NULL"
    )
    (count,) = connection.execute(f"SELECT COUNT(*) FROM ({query})").fetchone()
    names: list[str] = []
    vectors: np.ndarray | None = None
    for name, blob in connection.execute(query):
        if name in excluded:
            continue
        vector = np.frombuffer(blob, dtype="<f4")
        if vectors is None:
            vectors = np.empty((count, vector.size), dtype=np.float32)
        vectors[len(names)] = vector
        names.append(name)
    assert vectors is not None, "the database has no embeddings"
    return Index(names=names, vectors=normalize(vectors[: len(names)]))


def top_documents(
    query_vectors: np.ndarray, names: list[str], vectors: np.ndarray
) -> list[list[tuple[float, str]]]:
    """Return the best ``DEPTH`` (score, name) pairs of each query."""
    best: list[list[tuple[float, str]]] = [[] for _ in range(len(query_vectors))]
    for start in range(0, len(names), 50_000):
        scores = query_vectors @ vectors[start : start + 50_000].T
        depth = min(DEPTH, scores.shape[1])
        top = np.argpartition(-scores, depth - 1, axis=1)[:, :depth]
        for row, columns in enumerate(top):
            found = [(float(scores[row, c]), names[start + c]) for c in columns]
            best[row] = sorted(best[row] + found, reverse=True)[:DEPTH]
    return best


def ranking_scores(ranked: list[str], query: BenchmarkQuery) -> dict[str, float]:
    """Success@1, Success@10, nDCG@10 and reciprocal rank of one ranking."""
    hits = query.hits()
    gains = [query.answers.get(name, 0) for name in ranked[:DEPTH]]
    dcg = sum(gain / math.log2(rank + 2) for rank, gain in enumerate(gains))
    ideal_gains = sorted(query.answers.values(), reverse=True)[:DEPTH]
    ideal = sum(gain / math.log2(rank + 2) for rank, gain in enumerate(ideal_gains))
    first = next((rank for rank, name in enumerate(ranked) if name in hits), None)
    return {
        "success@1": float(bool(ranked) and ranked[0] in hits),
        "success@10": float(first is not None and first < DEPTH),
        "ndcg@10": dcg / ideal if ideal else 0.0,
        "mrr@10": 1.0 / (first + 1) if first is not None else 0.0,
    }


class Embedder:
    """The production embedding model, documents without prompt, queries with."""

    def __init__(self, model_name: str, device: str | None, batch_size: int):
        """Load the model in float32.

        In float32 the published descriptions embed to their published vectors
        with a cosine of at least 0.9998; in bfloat16, the default on some
        devices, only 0.998, which can reorder close candidates.
        """
        from sentence_transformers import SentenceTransformer  # Heavy; load lazily.

        self.model = SentenceTransformer(
            model_name, device=device, model_kwargs={"dtype": "float32"}
        )
        self.model.max_seq_length = MAX_SEQUENCE_LENGTH
        self.batch_size = batch_size

    def documents(self, texts: list[str]) -> np.ndarray:
        """Embed descriptions."""
        vectors = self.model.encode(
            texts, batch_size=self.batch_size, convert_to_numpy=True
        )
        return normalize(np.asarray(vectors, dtype=np.float32))

    def queries(self, texts: list[str]) -> np.ndarray:
        """Embed search queries with the model's query prompt."""
        vectors = self.model.encode(
            texts,
            batch_size=self.batch_size,
            convert_to_numpy=True,
            prompt_name="query",
        )
        return normalize(np.asarray(vectors, dtype=np.float32))


def rank_arm(
    descriptions: dict[str, str],
    embedder: Embedder,
    query_vectors: np.ndarray,
    outside: list[list[tuple[float, str]]],
) -> list[list[str]]:
    """Rank the index with one arm's descriptions of the benchmark files."""
    names = sorted(descriptions)
    vectors = embedder.documents([descriptions[name] for name in names])
    inside = top_documents(query_vectors, names, vectors)
    return [
        [name for _, name in sorted(first + second, reverse=True)[:DEPTH]]
        for first, second in zip(outside, inside, strict=True)
    ]


def summarize(scores: list[dict[str, float]], queries: list[BenchmarkQuery]) -> dict:
    """Mean of every metric, overall and per bucket."""

    def means(rows: list[dict[str, float]]) -> dict[str, float]:
        return {key: float(np.mean([row[key] for row in rows])) for key in rows[0]}

    buckets = sorted({query.bucket for query in queries})
    return {
        "queries": len(scores),
        "overall": means(scores),
        "buckets": {
            bucket: means([s for s, q in zip(scores, queries) if q.bucket == bucket])
            for bucket in buckets
        },
    }


def compare(first: list[dict], second: list[dict]) -> dict:
    """Mean paired difference of every metric, first minus second."""
    result = {}
    for metric in first[0]:
        differences = np.array([a[metric] - b[metric] for a, b in zip(first, second)])
        low, high = bootstrap_interval(differences)
        result[metric] = {
            "difference": float(differences.mean()),
            "interval": [low, high],
        }
    return result


def markdown(result: dict) -> str:
    """Render the summary as Markdown tables."""
    lines = [
        f"Queries with an answer in the benchmark files: {result['queries']}",
        "",
        "| Arm | Described | Success@1 | Success@10 | nDCG@10 | MRR@10 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label, arm in result["arms"].items():
        overall = arm["summary"]["overall"]
        lines.append(
            f"| {label} | {arm['described']} | {overall['success@1']:.3f} | "
            f"{overall['success@10']:.3f} | {overall['ndcg@10']:.3f} | "
            f"{overall['mrr@10']:.3f} |"
        )
    lines += ["", "| Comparison | Metric | Difference | 95% interval |"]
    lines.append("| --- | --- | ---: | --- |")
    for label, metrics in result["comparisons"].items():
        for metric, value in metrics.items():
            low, high = value["interval"]
            lines.append(
                f"| {label} | {metric} | {value['difference']:+.3f} | "
                f"[{low:+.3f}, {high:+.3f}] |"
            )
    return "\n".join(lines) + "\n"


def _arguments() -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--queries", type=Path, nargs="+", required=True)
    parser.add_argument("--run", action="append", default=[], help="label=directory")
    parser.add_argument("--compare", action="append", default=[], help="label:label")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--embedding-model", default=EMBEDDING_MODEL)
    parser.add_argument("--device", default=None)
    parser.add_argument("--batch-size", type=int, default=32)
    return parser.parse_args()


def arm_descriptions(arguments: argparse.Namespace) -> dict[str, dict[str, str]]:
    """The published descriptions and those of every run, by arm label."""
    targets = load_targets(arguments.inputs)
    arms = {"published": {t["name"]: t["published"] for t in targets if t["published"]}}
    names = {target["name"] for target in targets}
    for spec in arguments.run:
        label, directory = spec.split("=", 1)
        described = load_descriptions(Path(directory) / "descriptions.jsonl")
        arms[label] = {name: text for name, text in described.items() if name in names}
    return arms


def main() -> None:
    """Embed, search and score every arm."""
    arguments = _arguments()
    arguments.out.mkdir(parents=True, exist_ok=True)
    arms = arm_descriptions(arguments)
    targets = {target["name"] for target in load_targets(arguments.inputs)}
    queries = [q for q in load_queries(arguments.queries) if q.hits() & targets]
    logger.info("%d queries have an answer in the benchmark files", len(queries))
    index = load_index(arguments.database, excluded=targets)
    embedder = Embedder(
        arguments.embedding_model, arguments.device, arguments.batch_size
    )
    query_vectors = embedder.queries([query.text for query in queries])
    outside = top_documents(query_vectors, index.names, index.vectors)
    scores: dict[str, list[dict[str, float]]] = {}
    result: dict = {"queries": len(queries), "arms": {}, "comparisons": {}}
    with (arguments.out / "rankings.jsonl").open("w", encoding="utf-8") as stream:
        for label, descriptions in arms.items():
            rankings = rank_arm(descriptions, embedder, query_vectors, outside)
            scores[label] = [ranking_scores(r, q) for r, q in zip(rankings, queries)]
            result["arms"][label] = {
                "described": len(descriptions),
                "summary": summarize(scores[label], queries),
            }
            for query, ranking in zip(queries, rankings):
                record = {"arm": label, "id": query.identifier, "documents": ranking}
                stream.write(json.dumps(record) + "\n")
    pairs = arguments.compare or default_pairs(list(arms))
    for pair in pairs:
        first, second = pair.split(":")
        result["comparisons"][pair] = compare(scores[first], scores[second])
    (arguments.out / "search.json").write_text(json.dumps(result, indent=1) + "\n")
    (arguments.out / "search.md").write_text(markdown(result), encoding="utf-8")
    logger.info("\n%s", markdown(result))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
