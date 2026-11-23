# Reproducing the parser checks

The Rust parser reads source without compiling it. Keep three measurements separate:

- File coverage: every selected `.lean` file was read, with no traversal or read errors.
- Source declaration accuracy: names and positions match an independent reference. State which modules and declaration kinds the reference covers.
- Generated constants: names inferred by the parser, or names exported from a built Lean environment. These have different provenance.

A 100% source-declaration result does not mean every constant produced by Lean's elaborator was recovered. The parser's `approximate` flag records unresolved naming or inherited-field context.

## Imported source context

Supply dependency sources when structures inherit fields defined outside the input tree:

```sh
lean-source --root "$MATHLIB" "$MATHLIB/Mathlib" \
  --context "$LEAN_SOURCE/Init" --context "$BATTERIES/Batteries" \
  --text -o mathlib.jsonl
```

Context files participate in structure and generated-name resolution. They are not included in the output. The CLI accepts files or directories and reports their file and byte counts separately in `--stats` mode. It rejects `--context` together with `--no-project`.

Python takes explicit file paths:

```python
from pathlib import Path
import lean_source_parser as parser

context = list(Path(lean_source, "Init").rglob("*.lean"))
context += list(Path(batteries, "Batteries").rglob("*.lean"))
declarations = parser.parse_files(paths, root=mathlib, context_paths=context)
```

A missing context file is an error. Use sources matching the project's dependency versions. Without a known parent, an untyped field under `extends` remains an approximate candidate.

## Built Mathlib reference

Run `DumpRanges.lean` inside a matching built Lake environment, then compare its JSON Lines output with the parser export:

```sh
lake env lean --run /path/to/src/lean_explore/parser/validation/DumpRanges.lean ranges.jsonl Mathlib Mathlib
python /path/to/src/lean_explore/parser/validation/compare.py ranges.jsonl mathlib.jsonl "$MATHLIB" \
  --report comparison --json comparison-metrics.json --require-authored-exact
```

The comparison writes machine-readable metrics and fails when the authored-declaration gate is not exact. Generated declarations are reported separately. Precision is measured only in modules represented by the reference.

Cached `.olean` ranges can be stale even when the source checkout is correct. `refresh_reference_module.py` recompiles one unchanged source module under its original name in a separate artifact overlay. It records the source hash, compilation command and old/new reference rows. Use a fresh `--work` directory for each refresh. Preserve that provenance with the results; do not edit reference positions to make a comparison pass.

## Anthropic FLT reference

Use Anthropic's `tools/docs-site/gen/extract.py` to produce its main-theorem reference, then run:

```sh
python validation/compare_flt.py reference/thm.jsonl declarations.jsonl --report flt-comparison
```

This checks each reference theorem's name, kind and exact signature text. It covers the main theorem reference, not every declaration in the repository. Signature comparisons ignore trailing whitespace only.

## Generated `simps` names

Explicit `simps` projection names can be resolved from source when their directly annotated structure and `initialize_simps_projections` configuration are known. This includes `as_prefix` names such as `coe_copy`. Inferred result types and recursive projection expansion need elaboration.

For a project that is already built, export Lean's generated names as a separate semantic sidecar:

```sh
python validation/export_generated.py ranges.jsonl "$MATHLIB" -o simps.generated.jsonl
```

The sidecar preserves the kernel name, range and selection from Lean. Each record includes `provenance: "lean-environment"` and a source hash. The metadata file records the reference hash, counts, constants without ranges and invalid source positions that could not be attributed. The default attributes are `simps` and `simps!`; `--attributes` selects others.

The environment must match the sources. Hashes make the inputs traceable; they do not establish that the build matches them. Constants with no usable source range cannot be assigned to an attribute by this exporter. A successful export is not an independent accuracy test of the Rust parser.

## Timing and memory

Build the CLI and the in-memory runner with the same release profile:

```sh
cargo build --release -p lean-source-cli --example project_bench
cargo build --release -p lean-source-cli
python validation/benchmark_corpora.py \
  --corpora corpora.json --binary target/release/lean-source \
  --memory-runner target/release/examples/project_bench --out measurements
```

The corpus manifest is a JSON list. Each entry needs `name`, `root` and `source`; optional `context` entries supply dependency sources. Record repository commits and use `expected_files`, `expected_bytes`, `expected_context_files` and `expected_context_bytes` to reject an incomplete or contaminated input tree.

The runner uses 24 threads, one warm-up and five measured disk runs, then one warm-up and seven measured in-memory parses. Disk runs include discovery and reading, but no JSON export. In-memory runs preload the same bytes and include project context resolution. Declaration export adds signature text; command export is measured separately. Each process has a peak RSS measurement from `/usr/bin/time`; the in-memory peak includes retained input buffers and repeated parses.

Use the same filesystem for all corpora and pause other CPU or disk workloads. These are warm-cache measurements; the runner does not flush the operating system's file cache. Save raw runs, binary hashes and export hashes with the summary. JSON output sizes are logical byte sizes, not allocated disk space.
