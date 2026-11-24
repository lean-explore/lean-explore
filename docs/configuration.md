# Configuration

This page lists every environment variable LeanExplore reads, where files
live on disk, and how to override defaults. Configuration is centralized in
`src/lean_explore/config.py`.

## Environment variables

### Authentication

The public API, CLI search command, and stdio MCP API backend do not require
credentials. `LEANEXPLORE_API_KEY` is deprecated and ignored when present.

### Paths

| Variable | Default | Purpose |
|---|---|---|
| `LEAN_EXPLORE_CACHE_DIR` | `~/.lean_explore/cache` | Where `lean-explore data fetch` downloads data. Read by the local backend. |
| `LEAN_EXPLORE_DATA_DIR` | `<repo-root>/data` | Local extraction output directory. Only relevant if you run the extraction pipeline yourself. |
| `LEAN_EXPLORE_PACKAGES_ROOT` | `<repo-root>/lean` | Root for per-package Lean workspaces. Only relevant for extraction. |
| `LEAN_EXPLORE_VERSION` | contents of `~/.lean_explore/active_version`, or `v4.24.0` | Pin a specific cached version. |

### Local backend tuning

| Variable | Default | Purpose |
|---|---|---|
| `LEAN_EXPLORE_EMBEDDING_BATCH_SIZE` | `8` | Batch size used when embedding the query with the sentence-transformer. Raise on GPUs with more VRAM. |
| `LEAN_EXPLORE_RERANKER_BATCH_SIZE` | `16` on CUDA, `32` on CPU | Batch size for the cross-encoder reranker. |
| `LEAN_EXPLORE_FAISS_NPROBE` | `64` | Inverted lists searched per query in the FAISS index. `0` searches all of them, which is an exact search. |
| `LEAN_EXPLORE_RRF_CONSTANT` | `0` | Constant added to every rank when the name and semantic rankings are fused. |
| `LEAN_EXPLORE_SEMANTIC_WEIGHT` | `1` | Weight of the semantic ranking in that fusion. |
| `LEAN_EXPLORE_SEMANTIC_SHARE` | `0` | If positive, the share of the 500-candidate pool that goes to the top semantic candidates first, followed by name candidates; rank fusion is then not used. |

`SearchEngine` also takes these first-stage settings as the `fusion` (`FusionSettings`) and `faiss_nprobe` arguments, which override the environment.

### Extraction pipeline (only for contributors rebuilding the index)

| Variable | Default | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | (none) | Used by the informalization step to call OpenRouter. |

## On-disk layout

### Cache directory

Populated by `lean-explore data fetch`:

```
~/.lean_explore/
├── active_version                       # Text file: currently active version
└── cache/
    └── <VERSION>/                       # e.g. 20260127_103630
        ├── lean_explore.db              # SQLite database
        ├── informalization_faiss.index  # FAISS semantic index
        ├── informalization_faiss_ids_map.json
        ├── bm25_ids_map.json
        ├── bm25_name_raw/               # BM25 index (raw tokenization)
        │   ├── data.csc.index.npy
        │   ├── indices.csc.index.npy
        │   ├── indptr.csc.index.npy
        │   ├── nonoccurrence_array.index.npy
        │   ├── params.index.json
        │   └── vocab.index.json
        └── bm25_name_spaced/            # BM25 index (spaced tokenization)
            └── (same six files)
```

Change this location with `LEAN_EXPLORE_CACHE_DIR`.

### Model cache

First-run local-backend models are cached under `~/.cache/huggingface/` by
`sentence-transformers` and `transformers`. This is controlled by
Hugging Face, not LeanExplore. See `HF_HOME` / `TRANSFORMERS_CACHE` if you
need to move it.

## Programmatic access

All of the above resolves through the `Config` class:

```python
from lean_explore.config import Config

Config.CACHE_DIRECTORY      # ~/.lean_explore/cache
Config.ACTIVE_VERSION       # current version string
Config.ACTIVE_CACHE_PATH    # ~/.lean_explore/cache/<version>
Config.DATABASE_PATH        # .../lean_explore.db
Config.DATABASE_URL         # sqlite+aiosqlite:///.../lean_explore.db
Config.FAISS_INDEX_PATH
Config.FAISS_IDS_MAP_PATH
Config.BM25_RAW_PATH
Config.BM25_SPACED_PATH
Config.BM25_IDS_MAP_PATH
Config.API_BASE_URL         # https://www.leanexplore.com/api/v2
Config.R2_ASSETS_BASE_URL   # data download base URL
```

`ACTIVE_VERSION` is resolved, in order:

1. `LEAN_EXPLORE_VERSION` environment variable, if set.
2. Contents of `~/.lean_explore/active_version` (written by `data fetch`).
3. Fallback: `v4.24.0`.

## Common recipes

**Move the cache to a shared drive.**

```bash
export LEAN_EXPLORE_CACHE_DIR=/mnt/shared/lean-explore-cache
lean-explore data fetch
```

**Pin a specific data version across a team.**

```bash
export LEAN_EXPLORE_VERSION=20260127_103630
```

**Tune for a beefier GPU.**

```bash
export LEAN_EXPLORE_EMBEDDING_BATCH_SIZE=64
export LEAN_EXPLORE_RERANKER_BATCH_SIZE=128
lean-explore mcp serve --backend local
```

**Run the MCP server under a non-default config in Claude Desktop.**

```json
{
  "mcpServers": {
    "lean-explore": {
      "command": "lean-explore",
      "args": ["mcp", "serve", "--backend", "local"],
      "env": {
        "LEAN_EXPLORE_CACHE_DIR": "/Users/me/lean-cache",
        "LEAN_EXPLORE_EMBEDDING_BATCH_SIZE": "32"
      }
    }
  }
}
```

## See also

- [Getting Started](./getting-started.md)
- [Local Search Backend](./local-backend.md)
- [MCP Server](./mcp-server.md)
