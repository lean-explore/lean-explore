# Extraction Pipeline

The extraction pipeline is how LeanExplore *builds* the dataset that the
search engine queries. For everyday use you do not run it; `lean-explore
data fetch` downloads a prebuilt version. This page is for contributors who
want to regenerate the data (e.g., against a newer Mathlib) or run a custom
build locally.

The pipeline lives in `lean_explore.extract` and is invoked as a Python
module:

```bash
python -m lean_explore.extract [OPTIONS]
```

## What the pipeline does

Four stages, run in order:

1. **doc-gen4** (optional, `--run-doc-gen4`): runs Lake + doc-gen4 against
   each package workspace in `lean/<package>/` to produce structured
   declaration data (BMP files). This is the slow step: it builds Lean
   packages from source.
2. **Parse** (`--parse-docs`): reads the doc-gen4 output and writes one
   row per declaration into a fresh SQLite database under a new
   `YYYYMMDD_HHMMSS` directory in `LEAN_EXPLORE_DATA_DIR`.
3. **Informalize** (`--informalize`): calls an LLM (through OpenRouter) to
   generate a natural-language description for each declaration.
4. **Embed** (`--embeddings`): runs each informalization through the
   sentence-transformer to produce vector embeddings.
5. **Index** (`--index`): builds the FAISS semantic index and the two BM25
   indices over the declaration names.

Running with no flags runs all stages. Passing any stage flag switches to
"only run the stages I asked for" mode, which is how you resume after a
failure, or iterate on a single step.

## Prerequisites

### Python install

```bash
pip install lean-explore[extract]
```

This adds PyTorch, `sentence-transformers`, and `networkx` on top of the
base dependencies.

### Lean toolchain

You only need this if you plan to run `--run-doc-gen4`. Install
[`elan`](https://github.com/leanprover/elan) (the Lean version manager),
which provides `lake`. The pipeline fetches the correct Lean toolchain per
package from GitHub.

### Environment variables

| Variable | Required for | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | informalize stage | LLM calls go through [OpenRouter](https://openrouter.ai). |
| `LEAN_EXPLORE_DATA_DIR` | optional | Where extractions are written. Default: `<repo-root>/data`. |
| `LEAN_EXPLORE_PACKAGES_ROOT` | optional | Root for per-package Lean workspaces. Default: `<repo-root>/lean`. |
| `LEAN_EXPLORE_EMBEDDING_BATCH_SIZE` | optional | Overrides the embedding batch size. |

See [Configuration](./configuration.md) for the full list.

## Usage

### Full rebuild from scratch

```bash
export OPENROUTER_API_KEY=sk-or-...
python -m lean_explore.extract --run-doc-gen4 --fresh
```

- `--run-doc-gen4` runs the Lake/doc-gen4 step for every registered package.
- `--fresh` clears cached Lake dependencies, forcing the latest compatible
  versions to be resolved. Use this for nightly-style updates.

Expect this to take a while (hours on a cold machine), dominated by the
Lake build.

### Re-run a single stage on the latest extraction

Each stage flag accepts `--<stage>/--no-<stage>`. After a fresh `parse-docs`
creates a new timestamped directory, subsequent stages target the most
recent extraction automatically.

```bash
# Just rebuild embeddings + indices against the latest extraction
python -m lean_explore.extract --no-parse-docs --no-informalize --embeddings --index

# Only rebuild the FAISS/BM25 indices
python -m lean_explore.extract --index
```

### Smoke-test on a small slice

```bash
python -m lean_explore.extract \
  --informalize-limit 50 \
  --embedding-limit 50
```

## Selected flags

Run `python -m lean_explore.extract --help` for the full list. The most
commonly used flags:

| Flag | Default | Purpose |
|---|---|---|
| `--run-doc-gen4` | off | Regenerate Lean documentation with Lake + doc-gen4. |
| `--fresh` | off | Clear Lake caches before building. |
| `--parse-docs / --no-parse-docs` | on* | Parse doc-gen4 output into the database. |
| `--informalize / --no-informalize` | on* | Generate natural-language descriptions. |
| `--embeddings / --no-embeddings` | on* | Generate embeddings. |
| `--index / --no-index` | on* | Build FAISS + BM25 indices. |
| `--informalize-model` | `google/gemini-3-flash-preview` | OpenRouter model id. |
| `--informalize-max-concurrent` | `10` | Parallel informalization requests. |
| `--informalize-limit` | none | Cap informalization count (for testing). |
| `--embedding-model` | `Qwen/Qwen3-Embedding-0.6B` | Sentence-transformer model. |
| `--embedding-batch-size` | `250` | Lower this if you hit OOM. |
| `--embedding-max-seq-length` | `512` | Lower this to reduce memory. |
| `--embedding-server-url` | none | Delegate embeddings to a separate process (e.g., `http://localhost:5001`) to keep GPU memory free elsewhere. |
| `--verbose` | off | Verbose logging. |

*When you pass any stage flag explicitly, stages you did not mention default
to `off`.

## Output layout

Every run writes to a new timestamped directory under
`LEAN_EXPLORE_DATA_DIR`:

```
<LEAN_EXPLORE_DATA_DIR>/
└── 20260127_103630/
    ├── lean_explore.db
    ├── informalization_faiss.index
    ├── informalization_faiss_ids_map.json
    ├── bm25_ids_map.json
    ├── bm25_name_raw/
    └── bm25_name_spaced/
```

This is the same layout that `lean-explore data fetch` populates into the
cache directory. To make the local backend use your extraction instead of a
downloaded toolchain, construct a `SearchEngine` with `use_local_data=True`:

```python
from lean_explore.search import SearchEngine, Service

engine = SearchEngine(use_local_data=True)
service = Service(engine=engine)
```

`SearchEngine` with `use_local_data=True` resolves the latest complete
extraction directory automatically.

## Packages extracted

The registry lives in `src/lean_explore/extract/packages/registry.py`:

- `mathlib`: also supplies `Batteries`, `Init`, `Lean`, `Std` from its
  transitive dependencies.
- `physlean`
- `flt`
- `formal-conjectures`
- `cslib`

Each package entry specifies its git URL, version strategy (`LATEST` or
`TAGGED`), and dependencies. Adding a new package means adding a
`PackageConfig` to that registry and creating a corresponding workspace
under `lean/<name>/`.

## Packages without a build

A package whose `PackageConfig` sets `extraction=ExtractionMethod.SOURCE` is not built. `--run-doc-gen4` clones its repository into `lean/<name>/source/` at the ref its version strategy selects, and the parse step reads its Lean files with the build-free parser in `parser/`. Install the parser's Python bindings first:

```bash
pip install ./src/lean_explore/parser/crates/lean-source-py
```

The rows have the same name, module, line range, source text, docstring and source link format as doc-gen4 rows. The parser does not elaborate, so they differ in three ways:

- Dependencies are approximate. They are the identifiers written in the signature that resolve to a declaration by Lean's lookup rules: the declaration's namespaces, the root, the `open` and `export` commands in effect, and declarations of packages extracted earlier in the run. A name is only visible if it is declared earlier in the file or in an imported module. Types of the section variables a declaration uses are included, as are structure parents. References made through notation (`ℝ`, `+`, `∈`), through field access on a term (`s.card`) or by elaboration (instances, coercions) are missing, and where Lean would choose between candidates by type, none is recorded. `tests/extract/source/cases/` holds small packages with the references Lean reports for them, and `cases_test.py` lists the ones source extraction is known to get wrong.
- Anonymous instances are skipped, because Lean names them from the elaborated type.
- Names that attributes derive by rule, such as `to_additive` without a written name, are guesses, so they are left out unless `parse_package_sources` is called with `include_approximate=True`. Names generated by macros and docstrings given inside attributes are missing.

On FLT at commit ee47fd2a, the source path stores 3,069 declarations where doc-gen4 stored 3,834. All 3,064 names they share have identical source text and source links.

## See also

- [Configuration](./configuration.md): environment variables and cache/data
  paths.
- [Local Search Backend](./local-backend.md): how to consume your
  extraction output with `SearchEngine`.
- [CONTRIBUTING.md](../CONTRIBUTING.md): general contributor guide.
