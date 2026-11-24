#!/bin/bash
# Compare the parser with Lean's declaration ranges on the packages Mathlib depends on.
# Usage: heldout.sh RANGES.jsonl WORKSPACE TOOLCHAIN_SRC OUT_DIR
# WORKSPACE is a Lake workspace whose .lake/packages hold the dependencies; TOOLCHAIN_SRC is
# the toolchain's src/lean directory with the Lean core sources.
set -e
ranges=$1
ws=$2
core=$3
out=$4
here=$(cd "$(dirname "$0")" && pwd)
bin=${LEAN_SOURCE_BIN:-lean-source}
mkdir -p "$out"
run() {
  local name=$1 root=$2 prefix=$3
  shift 3
  local dirs=()
  for d in "$@"; do dirs+=("$root/$d"); done
  "$bin" --root "$root" "${dirs[@]}" -o "$out/$name.jsonl"
  echo "## $name"
  python3 "$here/compare.py" "$ranges" "$out/$name.jsonl" "$root" --prefix "$prefix" --report "$out/$name" | tail -n +3
}
run lean-core "$core" "" Init Std Lean
run batteries "$ws/.lake/packages/batteries" Batteries Batteries
run aesop "$ws/.lake/packages/aesop" Aesop Aesop
run qq "$ws/.lake/packages/Qq" Qq Qq
run proofwidgets "$ws/.lake/packages/proofwidgets" ProofWidgets ProofWidgets
run plausible "$ws/.lake/packages/plausible" Plausible Plausible
run importgraph "$ws/.lake/packages/importGraph" ImportGraph ImportGraph
