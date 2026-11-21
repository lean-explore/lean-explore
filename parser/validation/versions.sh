#!/bin/bash
# Validate the parser on the Lean core of every installed toolchain and on built Mathlib trees.
#
# usage: versions.sh OUT_DIR [INDEPENDENT_CHECK.py]
#
# For each toolchain under ~/.elan/toolchains, the reference comes from that toolchain's own
# .olean files and the sources from its src/lean. Mathlib rows are given as
# "label toolchain-dir workspace-dir [lean-source flags]" lines in $MATHLIB_ROWS; their LEAN_PATH is assembled from
# the existing builds, so nothing is built or updated.
set -u
out=$1
check=${2:-}
here=$(cd "$(dirname "$0")" && pwd)
bin=${LEAN_SOURCE_BIN:-lean-source}
mkdir -p "$out"
summary="$out/summary.txt"
: > "$summary"

dumper_for() {
  case "$1" in
    *v4.0.0) echo "$here/DumpRangesV40.lean" ;;
    *v4.9.0 | *v4.15.0) echo "$here/DumpRangesLegacy.lean" ;;
    *) echo "$here/DumpRanges.lean" ;;
  esac
}

score() {
  local label=$1 ranges=$2 parsed=$3 root=$4 prefix=$5
  local cmp
  cmp=$(python3 "$here/compare.py" "$ranges" "$parsed" "$root" --prefix "$prefix" --report "$out/$label/report" 2>&1)
  local authored
  authored=$(echo "$cmp" | grep "^all authored")
  local prec
  prec=$(echo "$cmp" | grep "^precision (authored")
  local ind=""
  if [ -n "$check" ]; then
    ind=$(python3 "$check" "$ranges" "$parsed" "$root" 2>&1 | grep -E "^(found_by_name|range_exact|selection_exact|MISS_name_written_in_source|ref_with_range) " | tr -s ' ' | tr '\n' ' ')
  fi
  echo "$label | $authored | $prec | $ind" >> "$summary"
}

for tdir in ~/.elan/toolchains/*; do
  label=$(basename "$tdir" | sed -e 's/leanprover--lean4---//' -e 's/leanprover--lean4-nightly---//')
  src="$tdir/src/lean"
  [ -d "$src/Init" ] || continue
  roots=""
  dirs=()
  for r in Init Std Lean; do
    if [ -f "$src/$r.lean" ]; then
      roots="${roots:+$roots,}$r"
      dirs+=("$src/$r")
    fi
  done
  mkdir -p "$out/$label"
  ranges="$out/$label/ranges.jsonl"
  if [ ! -s "$ranges" ]; then
    (cd /tmp && PATH="$tdir/bin:$PATH" timeout 1800 "$tdir/bin/lean" --run "$(dumper_for "$label")" "$ranges" "$roots" "" > "$out/$label/dump.log" 2>&1)
  fi
  "$bin" --root "$src" "${dirs[@]}" -o "$out/$label/parsed.jsonl"
  score "core-$label" "$ranges" "$out/$label/parsed.jsonl" "$src" ""
done

while read -r label tdir ws flags; do
  [ -n "${label:-}" ] || continue
  mkdir -p "$out/$label"
  lp=""
  for d in "$ws/.lake/build/lib/lean" "$ws"/.lake/packages/*/.lake/build/lib/lean; do
    [ -d "$d" ] && lp="${lp:+$lp:}$d"
  done
  ml="$ws"
  [ -d "$ws/.lake/packages/mathlib/Mathlib" ] && ml="$ws/.lake/packages/mathlib"
  ranges="$out/$label/ranges.jsonl"
  if [ ! -s "$ranges" ]; then
    (cd /tmp && PATH="$tdir/bin:$PATH" LEAN_PATH="$lp" timeout 3600 "$tdir/bin/lean" --run "$here/DumpRanges.lean" "$ranges" Mathlib Mathlib > "$out/$label/dump.log" 2>&1)
  fi
    # shellcheck disable=SC2086
  "$bin" ${flags:-} --root "$ml" "$ml/Mathlib" "$ml/Mathlib.lean" -o "$out/$label/parsed.jsonl"
  score "$label" "$ranges" "$out/$label/parsed.jsonl" "$ml" "Mathlib"
done <<< "${MATHLIB_ROWS:-}"
cat "$summary"
