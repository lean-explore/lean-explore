"""Per-attribute recall and precision of declarations that attributes generate.

Usage:
    python3 attributes.py RANGES.jsonl PARSER.jsonl SOURCE_ROOT

A reference constant counts for attribute A when its range starts at A's name in the
source, which is where Lean records attribute-generated declarations. A parser
declaration counts for A when its `origin` is A. Exact means the same range and
selection.
"""

import collections
import json
import re
import sys

ref_path, parser_path, root = sys.argv[1:4]
ATTRS = [
    "to_additive",
    "to_dual",
    "simps",
    "simps!",
    "reassoc",
    "elementwise",
    "mk_iff",
    "ext",
    "higher_order",
    "to_fun",
]


def user(n):
    """Drop Lean's private-name prefix `_private.<module>.0.`."""
    if n.startswith("_private."):
        p = n.split(".")
        if "0" in p[1:]:
            return ".".join(p[p.index("0", 1) + 1 :])
    return n


cache = {}


def line(m, k):
    """Return line `k` (1-based) of module `m`, or an empty string."""
    if m not in cache:
        try:
            cache[m] = (
                open(f"{root}/{m.replace('.', '/')}.lean", encoding="utf-8")
                .read()
                .split("\n")
            )
        except OSError:
            cache[m] = []
    lines = cache[m]
    return lines[k - 1] if 0 < k <= len(lines) else ""


parsed = {}
emitted = collections.Counter()
for row in open(parser_path, encoding="utf-8"):
    d = json.loads(row)
    parsed.setdefault((d["module"], d["name"]), []).append(d)
    if d.get("origin"):
        emitted[d["origin"]] += 1

ref_names = set()
total = collections.Counter()
found = collections.Counter()
exact = collections.Counter()
for row in open(ref_path, encoding="utf-8"):
    r = json.loads(row)
    ref_names.add((r["m"], user(r["n"])))
    if "r" not in r or "internal" in r["f"]:
        continue
    text = line(r["m"], r["r"][0])[r["r"][1] :]
    m = re.match(r"([A-Za-z_]+!?)", text)
    if not m or m.group(1) not in ATTRS:
        continue
    a = m.group(1)
    total[a] += 1
    cands = parsed.get((r["m"], user(r["n"])), [])
    if cands:
        found[a] += 1
        if any(
            tuple(c["range"][i] for i in (1, 2, 4, 5)) == tuple(r["r"])
            and tuple(c["selection"][i] for i in (1, 2, 4, 5)) == tuple(r["s"])
            for c in cands
        ):
            exact[a] += 1

correct = collections.Counter()
for (m, n), ds in parsed.items():
    for d in ds:
        if d.get("origin") and (m, n) in ref_names:
            correct[d["origin"]] += 1


def row_text(label, ref, hit, ok, out, right):
    """Format one table row of reference, found, exact, emitted and precision."""
    return (
        f"{label:14} {ref:9d} {hit / max(ref, 1):8.2%} {ok / max(ref, 1):8.2%}"
        f" {out:8d} {right / max(out, 1):9.2%}"
    )


print(
    f"{'attribute':14} {'reference':>9} {'found':>8} {'exact':>8}"
    f" {'emitted':>8} {'precision':>9}"
)
for a in ATTRS:
    print(row_text(a, total[a], found[a], exact[a], emitted[a], correct[a]))
print(
    row_text(
        "all",
        sum(total.values()),
        sum(found.values()),
        sum(exact.values()),
        sum(emitted.values()),
        sum(correct.values()),
    )
)
