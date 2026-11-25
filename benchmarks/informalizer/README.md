# Informalization benchmark

This benchmark compares two ways of writing the natural-language descriptions that LeanExplore indexes: the current pipeline, one request per declaration, and `lean-informal`, a Rust planner that turns each Lean file into a few requests. Both run on the same 100 Mathlib files with two models at two reasoning efforts. Cost and description quality are measured on the same declarations.

## Run the benchmark

1. Install the Python package, the source parser's bindings and a Rust toolchain:

   ```sh
   pip install -e ".[extract]"
   pip install maturin
   maturin develop --release -m src/lean_explore/parser/crates/lean-source-py/Cargo.toml
   ```

2. Download the published database of data version `20260714_172516` (2.2 GB). Do not use `lean-explore data fetch` for this: it deletes every other cached version.

   ```sh
   curl -LO https://pub-48b75babc4664808b15520033423c765.r2.dev/assets/20260714_172516/lean_explore.db
   ```

3. Download version 1 of lean-explore-bench, which needs access to the dataset:

   ```sh
   hf download lean-explore/lean-explore-bench --repo-type dataset --local-dir bench
   ```

4. Prepare Lean for the round trip. Skip this step to run everything except the Lean checks.

   ```sh
   git init mathlib4 && cd mathlib4
   git fetch --depth 1 https://github.com/leanprover-community/mathlib4.git aae61ae084c21995ee248964a81e3750ad0db2db
   git checkout FETCH_HEAD
   lake exe cache get
   cd ..
   pip install lean-interact==0.11.6 datasets rich scikit-learn tqdm
   export ELAN_TOOLCHAIN=leanprover/lean4:v4.32.0
   ```

5. Run the dry runs and read the cost estimate the script prints. No request is sent.

   ```sh
   DATABASE=lean_explore.db QUERIES=bench/queries MATHLIB=mathlib4 \
     benchmarks/informalizer/run.sh
   ```

6. Run the benchmark. Set `DEVICE=cuda` to embed on a GPU and `WORKERS` to the number of Lean servers the machine's memory allows (default 4). After an interruption, run the same command again: every request is journaled and none is sent twice.

   ```sh
   CONFIRM=yes OPENROUTER_API_KEY=... DATABASE=lean_explore.db QUERIES=bench/queries \
     MATHLIB=mathlib4 DEVICE=cuda benchmarks/informalizer/run.sh
   ```

7. Grade the review sheet. Open `benchmarks/informalizer/work/results/review/review.html` and fill in `grades.csv` in the same directory: `faithful` is 0 when the description is wrong or misses something that changes the meaning, 1 when it is loose but a mathematician would read it correctly, 2 when it states exactly the declaration; `clear` is 0 to 2.

8. Calibrate the judge against the grades:

   ```sh
   python -m benchmarks.informalizer.calibrate \
     --review benchmarks/informalizer/work/results/review \
     --judge benchmarks/informalizer/work/results/judge \
     --out benchmarks/informalizer/work/results/calibration
   ```

9. Read the results in `benchmarks/informalizer/work/results`: `cost/cost.md`, `search/search.md`, `judge/judge.md`, `round-trip/round_trip.md` and `calibration/calibration.json`.

Each current run sends 10,510 requests and each per-file run about 400. The Lean checks take about 10 seconds each, so the 2,700 checks of the round trip take about two hours on four servers.

## What is compared

| Arm | Requests |
| --- | --- |
| `current` | one per declaration, built by production's `build_prompt` from `src/lean_explore/extract/informalize`: the statement without its proof and the published descriptions of its dependencies |
| `per-file` | `lean-informal plan` cuts each file into runs of declarations; a request holds the scope the file sets up (`namespace`, `section`, `open`, `variable`, local notation), the statements of the run without proofs, and the dependency descriptions those statements use most, ranked by how many of them use each and then by how rare it is in the corpus |

In `per-file`, the instructions are sent once per request instead of once per declaration, and names an attribute generates (`to_additive`, `simps`, ...) are asked for in the same request as their parent. A plain `alias` copies its target's description, and a `reassoc` or `elementwise` lemma copies its parent's with one fixed sentence; neither needs a request. `lean-informal assemble` checks each answer against the names its request listed. Names the planner cannot find in the source, and names an answer leaves out, are sent with the current prompt; their cost counts toward `per-file`.

| Role | Model |
| --- | --- |
| Generation | `deepseek/deepseek-v4.1-flash` at temperature 0.3, and `openai/gpt-6.1-terra`, which takes no temperature; each with the provider's default reasoning and with `low` |
| Reference, not regenerated | the published descriptions, written by `google/gemini-3-flash-preview` |
| Judge and back-translation | `anthropic/claude-sonnet-5.5`, a family that wrote none of the compared descriptions |
| Search embeddings | `Qwen/Qwen3-Embedding-0.6B`, production's model |

Run labels have the form `<model>-<effort>/<arm>`, for example `terra-low/per-file`. Every scorer compares each run with the published descriptions, `per-file` with `current` at the same model and effort, and `low` with the default at the same model and arm.

The inputs are pinned to data version `20260714_172516`, whose Mathlib declarations come from Mathlib `aae61ae084c21995ee248964a81e3750ad0db2db`; every source in the database matches its file at the recorded lines. `files.json` lists the 100 files. Mathlib's files are split into five strata of increasing size, each holding a fifth of its 302,546 declarations, and twenty files come from each stratum, preferring files that hold answers to benchmark queries. The files hold 10,510 declarations, and costs are weighted back to all of Mathlib by stratum. The selection used the build of lean-explore-bench of 12 November 2026; `select_files` repeats it with any query file.

## How quality is measured

Every measure judges a description against the Lean declaration it describes, and each follows the method of a published benchmark. They are reported together because each has a known weakness.

| Measure | Method | Source |
| --- | --- | --- |
| Search | Success@1, Success@10 and nDCG@10 over the lean-explore-bench queries whose answers lie in the 100 files | lean-explore-bench |
| Judge | the statement in its scope is split into its hypotheses and conclusion, each part is rated match, minor or major against the description, three votes per rating with the majority kept | LeanScorer, in "Mathesis" (arXiv 2506.07047); majority voting from "Formal-Informal Mathematical Alignment through and beyond Human Annotations" (CIKM 2026) |
| Round trip | a model that sees only the description writes a Lean theorem; BEq+ proves it equivalent in both directions to the Mathlib constant, read as `type_of% @Name` so that the hypotheses of `variable` lines are included | "Reliable Evaluation and Benchmarks for Statement Autoformalization" (EMNLP 2025) |
| Human review | 60 declarations graded blind, the published text and the default-effort runs side by side; `calibrate` reports the judge's precision, recall, F1 and kappa on them | ProofNetVerif, from the same EMNLP 2025 paper |

Differences come with paired bootstrap 95% intervals over queries or declarations.

- Search scores the dense channel only. The descriptions of the 100 files are embedded again with production's model, every other declaration keeps its published vector, and name search, the dependency boost and the reranker are not run.
- BEq+ finds about half of the true equivalences (recall 48.3, precision 98.0 on ProofNetVerif) and covers theorems only, so its rates rank the runs but understate their accuracy. The round trip downloads the reference implementation of BEq+ from LeanInteract at commit `768e18ebcb5c7ec34d308768a785389eeac37798`, checks its SHA-256 and runs it unchanged.
- LeanScorer reported an F1 of 0.92 on competition problems. Its agreement on Mathlib descriptions is what `calibrate` measures.

## Cost

The dry runs count the tokens of the exact prompts with the Qwen3 tokenizer and estimate the answers from the published descriptions. Prices are OpenRouter's list prices of 25 November 2026, in dollars per million input and output tokens: 0.14 and 0.42 for DeepSeek, 1 and 5 for Terra, 2 and 10 for Sonnet. Reasoning tokens are billed as output and are unknown before a run, so each run is priced with none, with as many as the visible output, and with three times as many.

| Run, for one effort | Requests | No reasoning | Reasoning = output | Reasoning = 3 × output |
| --- | ---: | ---: | ---: | ---: |
| DeepSeek, current | 10,510 | $1.54 | $1.89 | $2.60 |
| DeepSeek, per-file | 396 | $0.56 | $0.95 | $1.73 |
| Terra, current | 10,510 | $12.67 | $16.89 | $25.32 |
| Terra, per-file | 396 | $5.87 | $10.53 | $19.84 |

| Evaluation | Requests | Cost |
| --- | ---: | ---: |
| Judge, 500 declarations × 9 arms × 3 votes, plus one decomposition each | 14,000 | $28.15 |
| Round trip, 300 theorems × 9 arms | 2,700 | $3.31 |

Assuming `low` spends between none and as many reasoning tokens as output, and the default between one and three times as many, the eight generation runs and the evaluation cost about $82 to $111 in total. DeepSeek caches repeated prompt prefixes, which bills the repeated instructions at its cache price: without reasoning, $1.10 for current and $0.52 for per-file. OpenAI caches only prefixes of at least 1,024 tokens, which these prompts do not reach. Retries and fallbacks add a few percent, and `score_cost` reports the measured cost of each run from OpenRouter's own figures. Embedding and the Lean checks run locally.

## Files

| File | Role |
| --- | --- |
| `run.sh` | runs every stage |
| `select_files.py`, `files.json` | the choice of the 100 files |
| `export_inputs.py`, `corpus.py` | targets, dependency context and sources from the pinned data |
| `arm_current.py`, `arm_per_file.py`, `client.py`, `prompts/batch.txt` | the two arms and the journaled OpenRouter client |
| `dry_run.py`, `estimate_cost.py` | token counts and the cost estimate |
| `score_cost.py`, `score_search.py`, `comparisons.py` | measured cost, search quality and paired comparisons |
| `sample.py`, `judge.py`, `prompts/judge_*.txt` | the decomposed judge |
| `round_trip.py`, `lean_check.py`, `prompts/formalize.txt` | back-translation and BEq+ |
| `review_sheet.py`, `calibrate.py` | human review and judge calibration |
| `src/lean_explore/parser/crates/lean-informal` | the Rust planner: `plan`, `assemble` and `statements` |
