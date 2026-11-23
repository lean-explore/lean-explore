"""Measure parser timings and peak RSS for pinned source corpora on one filesystem.

Run only after correctness checks and while other benchmarks/builds are idle.
Outputs JSON, raw process logs, and declaration/command exports. Linux/WSL required.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import subprocess
import time
from pathlib import Path

STATS = re.compile(
    r"files (\d+) bytes (\d+) declarations (\d+) threads (\d+) "
    r"walk ([\d.]+) ms parse ([\d.]+) ms \((\d+) MB/s\)"
)


def measured(command: list[str], target: Path) -> tuple[dict, str]:
    """Run a process and save elapsed time, peak RSS, stdout and stderr."""
    resource_file = target.with_suffix(".resource.json")
    wrapped = [
        "/usr/bin/time",
        "-f",
        '{"elapsed_s":%e,"user_s":%U,"system_s":%S,"peak_rss_kib":%M,"major_faults":%F,"minor_faults":%R}',
        "-o",
        str(resource_file),
        *command,
    ]
    start = time.perf_counter()
    result = subprocess.run(wrapped, capture_output=True, text=True)
    elapsed = time.perf_counter() - start
    target.with_suffix(".stdout").write_text(result.stdout)
    target.with_suffix(".stderr").write_text(result.stderr)
    if result.returncode:
        raise RuntimeError(
            f"{target.name} exited {result.returncode}: {result.stderr[-4000:]}"
        )
    resource = json.loads(resource_file.read_text())
    resource["wall_s"] = elapsed
    return resource, result.stderr


def sha256(path: Path) -> str:
    """Hash an export without retaining it in memory."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_corpus(
    corpus: dict,
    executable: Path,
    memory_runner: Path,
    out: Path,
    runs: int,
    exports: bool,
) -> dict:
    """Measure one corpus using identical inputs for disk and memory runs."""
    out.mkdir(parents=True, exist_ok=True)
    root = Path(corpus["root"])
    source = Path(corpus["source"])
    context_args = [
        item for path in corpus.get("context", []) for item in ["--context", path]
    ]
    arguments = [
        str(executable),
        "--jobs",
        "24",
        "--root",
        str(root),
        str(source),
        *context_args,
    ]
    result = {
        "corpus": corpus,
        "binary_sha256": sha256(executable),
        "memory_runner_sha256": sha256(memory_runner),
        "disk_runs": [],
    }
    warm, stderr = measured(arguments + ["--stats"], out / "disk-warmup")
    result["disk_warmup"] = warm
    for i in range(runs):
        measurement, stderr = measured(arguments + ["--stats"], out / f"disk-{i}")
        measurement.update(context_files=0, context_bytes=0)
        match = STATS.fullmatch(stderr.strip().splitlines()[-1])
        diagnostics = stderr.strip().splitlines()[:-1]
        for line in diagnostics:
            context_match = re.fullmatch(r"context files (\d+) bytes (\d+)", line)
            if not context_match:
                raise RuntimeError(f"Unexpected diagnostics: {line}")
            measurement.update(
                context_files=int(context_match[1]), context_bytes=int(context_match[2])
            )
        if not match:
            raise RuntimeError(f"Unexpected parser diagnostics: {stderr}")
        nf, nb, nd, nt, walk, parse, _ = match.groups()
        measurement.update(
            files=int(nf),
            bytes=int(nb),
            declarations=int(nd),
            threads=int(nt),
            walk_ms=float(walk),
            parse_including_read_ms=float(parse),
        )
        for key in ["files", "bytes", "context_files", "context_bytes"]:
            expected = corpus.get(f"expected_{key}")
            if expected is not None and measurement[key] != expected:
                raise RuntimeError(
                    f"{corpus['name']} {key}: expected {expected}, "
                    f"got {measurement[key]}"
                )
        result["disk_runs"].append(measurement)
        print(
            f"{corpus['name']} disk {i + 1}/{runs}: "
            f"{measurement['wall_s']:.3f}s, peak {measurement['peak_rss_kib']} KiB",
            flush=True,
        )
    resource, _ = measured(
        [str(memory_runner), str(source), "8", "24", *corpus.get("context", [])],
        out / "memory",
    )
    memory = [
        json.loads(line) for line in (out / "memory.stdout").read_text().splitlines()
    ]
    result["memory_resource"] = resource
    result["memory_runs"] = memory
    parse_runs = [r for r in memory if r["phase"] == "parse" and not r["warmup"]]
    counts = {
        (
            r["files"],
            r["bytes"],
            r["declarations"],
            r["context_files"],
            r["context_bytes"],
        )
        for r in result["disk_runs"] + parse_runs
    }
    if len(counts) != 1:
        raise RuntimeError(f"Inconsistent counts: {counts}")
    result["summary"] = {
        "files": result["disk_runs"][0]["files"],
        "bytes": result["disk_runs"][0]["bytes"],
        "declarations": result["disk_runs"][0]["declarations"],
        "context_files": result["disk_runs"][0]["context_files"],
        "context_bytes": result["disk_runs"][0]["context_bytes"],
        "mode": "project",
        "threads": 24,
        "disk_median_s": statistics.median(r["wall_s"] for r in result["disk_runs"]),
        "disk_min_s": min(r["wall_s"] for r in result["disk_runs"]),
        "disk_max_s": max(r["wall_s"] for r in result["disk_runs"]),
        "disk_peak_rss_kib": max(r["peak_rss_kib"] for r in result["disk_runs"]),
        "memory_median_ms": statistics.median(r["parse_ms"] for r in parse_runs),
        "memory_min_ms": min(r["parse_ms"] for r in parse_runs),
        "memory_max_ms": max(r["parse_ms"] for r in parse_runs),
        "memory_peak_rss_kib": resource["peak_rss_kib"],
    }
    if exports:
        result["exports"] = {}
        for mode in ["declarations", "commands"]:
            path = out / f"{mode}.jsonl"
            flags = ["--text"] if mode == "declarations" else ["--commands"]
            measurement, stderr = measured(
                arguments + flags + ["-o", str(path)], out / f"export-{mode}"
            )
            if stderr.strip():
                raise RuntimeError(stderr)
            measurement.update(bytes=path.stat().st_size, sha256=sha256(path))
            result["exports"][mode] = measurement
            print(
                f"{corpus['name']} {mode} export: {measurement['wall_s']:.3f}s, "
                f"peak {measurement['peak_rss_kib']} KiB",
                flush=True,
            )
    (out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> None:
    """Read a corpus manifest and run benchmarks sequentially."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpora", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--memory-runner", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--no-exports", action="store_true")
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")
    corpora = json.loads(args.corpora.read_text(encoding="utf-8-sig"))
    args.out.mkdir(parents=True, exist_ok=True)
    results = []
    for corpus in corpora:
        results.append(
            run_corpus(
                corpus,
                args.binary,
                args.memory_runner,
                args.out / corpus["name"],
                args.runs,
                not args.no_exports,
            )
        )
        (args.out / "results.json").write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()
