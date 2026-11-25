#!/usr/bin/env bash
# Runs the informalization benchmark. See benchmarks/informalizer/README.md.
#
# Required:
#   DATABASE  lean_explore.db of data version 20260714_172516
#   QUERIES   the queries directory of lean-explore-bench version 1
# Optional:
#   MATHLIB   Mathlib checkout at the pinned commit with its cache, for the round trip
#   WORK      output directory (default benchmarks/informalizer/work)
#   CONFIRM   set to "yes" to send paid requests; without it the script stops
#             after the dry runs and the cost estimate
#   EFFORTS   reasoning efforts to run (default "default low"; "default" sends none)
#   DEVICE    device for the embedding model (default: picked by sentence-transformers)
#   WORKERS   Lean servers for the round trip (default 4)
set -euo pipefail

ROOT=$(git rev-parse --show-toplevel)
cd "$ROOT"
HERE=benchmarks/informalizer
WORK=${WORK:-$HERE/work}
DATABASE=${DATABASE:?set DATABASE to lean_explore.db of data version 20260714_172516}
QUERIES=${QUERIES:?set QUERIES to the lean-explore-bench queries directory}
FILES=$HERE/files.json
INPUTS=$WORK/inputs
RUNS=$WORK/runs
RESULTS=$WORK/results
DRY=$WORK/dry
PLANNER=$WORK/bin/lean-informal
PYTHON=${PYTHON:-python}
read -r -a EFFORT_LIST <<< "${EFFORTS:-default low}"

declare -A MODEL=([deepseek]=deepseek/deepseek-v4.1-flash [terra]=openai/gpt-6.1-terra)
# The Terra models on OpenRouter accept no temperature.
declare -A TEMPERATURE=([deepseek]=0.3 [terra]=none)

# Labels have the form <model>-<effort>/<arm>, which the scorers pair up.
RUN_FLAGS=()
REVIEW_FLAGS=()
for model in deepseek terra; do
  for effort in "${EFFORT_LIST[@]}"; do
    for arm in current per-file; do
      RUN_FLAGS+=(--run "$model-$effort/$arm=$RUNS/$model-$effort/$arm")
      if [[ "$effort" == "default" ]]; then
        REVIEW_FLAGS+=(--run "$model-$effort/$arm=$RUNS/$model-$effort/$arm")
      fi
    done
  done
done
if [[ ${#REVIEW_FLAGS[@]} -eq 0 ]]; then REVIEW_FLAGS=("${RUN_FLAGS[@]}"); fi

echo "== Building the planner"
mkdir -p "$WORK/bin"
(cd src/lean_explore/parser && cargo build --release -p lean-informal)
cp "${CARGO_TARGET_DIR:-src/lean_explore/parser/target}/release/lean-informal" "$PLANNER"

echo "== Exporting the inputs"
if [[ ! -f "$INPUTS/manifest.json" ]]; then
  "$PYTHON" -m $HERE.export_inputs --database "$DATABASE" --files "$FILES" --out "$INPUTS"
fi
mkdir -p "$RESULTS"
"$PLANNER" statements --sources "$INPUTS/sources" --targets "$INPUTS/targets.jsonl" \
  --out "$RESULTS/statements.jsonl"

echo "== Dry runs"
"$PYTHON" -m $HERE.arm_current --inputs "$INPUTS" --out "$DRY/current" --dry-run
"$PYTHON" -m $HERE.arm_per_file --inputs "$INPUTS" --out "$DRY/per-file" \
  --planner "$PLANNER" --dry-run
"$PYTHON" -m $HERE.judge --inputs "$INPUTS" --files "$FILES" \
  --statements "$RESULTS/statements.jsonl" "${RUN_FLAGS[@]}" --out "$DRY/judge" --dry-run
"$PYTHON" -m $HERE.round_trip --inputs "$INPUTS" --files "$FILES" \
  --statements "$RESULTS/statements.jsonl" "${RUN_FLAGS[@]}" --out "$DRY/round-trip" \
  --dry-run
"$PYTHON" -m $HERE.estimate_cost --efforts "${EFFORT_LIST[@]}" \
  --generation current="$DRY/current/estimate.json" \
  --generation per-file="$DRY/per-file/estimate.json" \
  --stage judge="$DRY/judge/estimate.json" \
  --stage round-trip="$DRY/round-trip/estimate.json" --out "$DRY/cost.md"
cat "$DRY/cost.md"

if [[ "${CONFIRM:-}" != "yes" ]]; then
  echo "Dry runs done. Set CONFIRM=yes to send the requests priced above."
  exit 0
fi
: "${OPENROUTER_API_KEY:?set OPENROUTER_API_KEY}"

echo "== Generation"
for model in deepseek terra; do
  for effort in "${EFFORT_LIST[@]}"; do
    flags=(--model "${MODEL[$model]}" --temperature "${TEMPERATURE[$model]}")
    if [[ "$effort" != "default" ]]; then flags+=(--reasoning-effort "$effort"); fi
    out=$RUNS/$model-$effort
    "$PYTHON" -m $HERE.arm_current --inputs "$INPUTS" --out "$out/current" "${flags[@]}"
    "$PYTHON" -m $HERE.arm_per_file --inputs "$INPUTS" --out "$out/per-file" \
      --planner "$PLANNER" "${flags[@]}"
  done
done

echo "== Cost"
"$PYTHON" -m $HERE.score_cost --inputs "$INPUTS" --files "$FILES" "${RUN_FLAGS[@]}" \
  --out "$RESULTS/cost"

echo "== Search"
"$PYTHON" -m $HERE.score_search --database "$DATABASE" --inputs "$INPUTS" \
  --queries "$QUERIES" "${RUN_FLAGS[@]}" --out "$RESULTS/search" \
  ${DEVICE:+--device "$DEVICE"}

echo "== Judge"
"$PYTHON" -m $HERE.judge --inputs "$INPUTS" --files "$FILES" \
  --statements "$RESULTS/statements.jsonl" "${RUN_FLAGS[@]}" --out "$RESULTS/judge"

echo "== Review sheet"
"$PYTHON" -m $HERE.review_sheet --inputs "$INPUTS" --files "$FILES" \
  --statements "$RESULTS/statements.jsonl" "${REVIEW_FLAGS[@]}" --out "$RESULTS/review"

echo "== Round trip"
round_trip=(--inputs "$INPUTS" --files "$FILES" --statements "$RESULTS/statements.jsonl"
  "${RUN_FLAGS[@]}" --out "$RESULTS/round-trip")
if [[ -n "${MATHLIB:-}" ]]; then
  "$PYTHON" -m $HERE.round_trip "${round_trip[@]}" --mathlib "$MATHLIB" \
    --workers "${WORKERS:-4}"
else
  "$PYTHON" -m $HERE.round_trip "${round_trip[@]}" --skip-lean
  echo "MATHLIB is not set: candidates written, Lean checks skipped."
fi

echo "Done. Fill in $RESULTS/review/grades.csv, then run:"
echo "  $PYTHON -m $HERE.calibrate --review $RESULTS/review --judge $RESULTS/judge --out $RESULTS/calibration"
