"""Compare parser output with the declaration ranges of a built Lean environment.

Usage:
    python compare.py RANGES.jsonl PARSER.jsonl SOURCE_ROOT [--report DIR]

RANGES.jsonl comes from DumpRanges.lean and PARSER.jsonl from
`lean-source --root SOURCE_ROOT`.

Reference constants are classified by what the source says about them:

* declaration: the text at the selection range is the constant's name (or its last
  components), and the range covers a whole command;
* field / constructor: the range is the written field or constructor name;
* anonymous instance: an `instance` without a written name; matched by position;
* generated: everything else (`to_additive`, `simps`, parent projections, default
  constructors, notation, auxiliary definitions). Source text alone cannot name these
  in general; parent projections and default constructors are reported separately
  because the parser derives them.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys


def user_name(name: str) -> str:
    """Drop Lean's private-name prefix `_private.<module>.0.`."""
    if name.startswith("_private."):
        parts = name.split(".")
        try:
            i = parts.index("0", 1)
        except ValueError:
            return name
        return ".".join(parts[i + 1 :])
    return name


def norm(name: str) -> str:
    """Remove guillemets and a leading `_root_.` from a name."""
    return name.replace("«", "").replace("»", "").removeprefix("_root_.")


class Sources:
    """Read source lines of modules under a root directory, with a cache."""

    def __init__(self, root: str):
        """Use `root` as the directory that holds the module files."""
        self.root = root
        self.cache: dict[str, list[str] | None] = {}

    def lines(self, module: str) -> list[str] | None:
        """Return the lines of a module's source file, or None if it is missing."""
        if module not in self.cache:
            path = os.path.join(self.root, module.replace(".", "/") + ".lean")
            try:
                with open(path, encoding="utf-8", newline="") as f:
                    self.cache[module] = f.read().split("\n")
            except OSError:
                self.cache[module] = None
        return self.cache[module]

    def text(self, module: str, r: list[int]) -> str:
        """Return the source text between two line/column positions."""
        ls = self.lines(module)
        if ls is None:
            return ""
        sl, sc, el, ec = r
        if sl < 1 or el > len(ls):
            return ""
        if sl == el:
            return ls[sl - 1][sc:ec]
        return "\n".join([ls[sl - 1][sc:]] + ls[sl : el - 1] + [ls[el - 1][:ec]])


COMMAND_STARTS = {
    "/--",
    "@[",
    "|",
    "private",
    "protected",
    "public",
    "noncomputable",
    "partial",
    "unsafe",
    "nonrec",
    "meta",
    "scoped",
    "local",
    "theorem",
    "lemma",
    "def",
    "abbrev",
    "instance",
    "structure",
    "class",
    "inductive",
    "coinductive",
    "opaque",
    "axiom",
    "irreducible_def",
    "alias",
    "syntax",
    "macro",
    "elab",
    "notation",
    "notation3",
    "infix",
    "infixl",
    "infixr",
    "prefix",
    "postfix",
    "initialize",
    "builtin_initialize",
    "register_option",
    "register_builtin_option",
    "declare_config_elab",
    "declare_command_config_elab",
    "declare_core_config_elab",
    "simproc",
    "simproc_decl",
    "dsimproc",
    "dsimproc_decl",
}


def first_word(text: str) -> str:
    """Return the first word of a command, or its leading doc or attribute token."""
    t = text.lstrip()
    for p in ("/--", "@[", "|"):
        if t.startswith(p):
            return p
    w = ""
    for ch in t:
        if ch.isspace() or ch in "([{:":
            break
        w += ch
    return w


def in_attribute(src: Sources, module: str, sel: list[int]) -> bool:
    """True if the selection lies inside `@[...]` on its line."""
    ls = src.lines(module)
    if ls is None or sel[0] > len(ls):
        return False
    before = ls[sel[0] - 1][: sel[1]]
    i = max(before.rfind("@["), before.rfind("attribute ["))
    return i >= 0 and "]" not in before[i:]


def classify(rec: dict, src: Sources) -> str:
    """Classify a reference constant by what its source text says about it."""
    if "r" not in rec:
        return "no-range"
    sel = norm(src.text(rec["m"], rec["s"]))
    name = norm(user_name(rec["n"]))
    matches = name == sel or name.endswith("." + sel)
    same = rec["r"] == rec["s"]
    if in_attribute(src, rec["m"], rec["s"]):
        # Named or placed by an attribute: `@[ext]`, `@[simps]`, `@[to_additive foo]`.
        return "generated-attribute"
    if matches and not same:
        w = first_word(src.text(rec["m"], rec["r"]))
        if w not in COMMAND_STARTS and norm(w) != sel:
            # Named in an attribute: `@[to_additive foo]`, `@[simps foo]`,
            # `@[to_dual foo]`.
            return "generated-attribute"
        return "constructor" if rec["k"] == "ctor" else "declaration"
    if matches and same:
        if rec["k"] == "ctor":
            return "constructor"
        if "proj" in rec["f"]:
            return "field"
        return "declaration-short"
    if "inst" in rec["f"] and sel == "instance":
        return "anonymous-instance"
    if rec["k"] == "ctor" and same and name.endswith(".mk"):
        return "generated-constructor"
    if (
        rec["k"] == "def"
        and "proj" not in rec["f"]
        and name.split(".")[-1].startswith("to")
        and not same
    ):
        return "generated-parent"
    if (
        "inst" in rec["f"]
        and not same
        and src.text(rec["m"], rec["r"]).lstrip().startswith(("class", "structure"))
    ):
        return "generated-parent"
    return "generated"


AUTHORED = (
    "declaration",
    "declaration-short",
    "field",
    "constructor",
    "anonymous-instance",
)
DERIVED = ("generated-constructor", "generated-parent", "generated-attribute")


def span_lc(span: list[int]) -> tuple[int, int, int, int]:
    """Return the line and column fields of a parser span.

    A parser span is [start_byte, start_line, start_col, end_byte, end_line, end_col].
    """
    return span[1], span[2], span[4], span[5]


def main() -> int:
    """Compare the two files and print recall, span accuracy and precision."""
    ap = argparse.ArgumentParser()
    ap.add_argument("ranges")
    ap.add_argument("parser")
    ap.add_argument("root")
    ap.add_argument("--report", default=None)
    ap.add_argument("--prefix", default="Mathlib")
    args = ap.parse_args()

    src = Sources(args.root)
    refs = [json.loads(line) for line in open(args.ranges, encoding="utf-8")]
    refs = [r for r in refs if r["m"].startswith(args.prefix)]
    parsed = [json.loads(line) for line in open(args.parser, encoding="utf-8")]
    modules = {p["module"] for p in parsed}
    # Score every module whose source file exists, so that files where a tool finds
    # nothing still count.
    refs = [r for r in refs if r["m"] in modules or src.lines(r["m"]) is not None]
    modules |= {r["m"] for r in refs}

    by_name: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    anon_by_pos: dict[tuple[str, int, int], dict] = {}
    for p in parsed:
        if p["name"] is not None:
            by_name[(p["module"], norm(p["name"]))].append(p)
        elif p["kind"] == "instance":
            anon_by_pos[(p["module"], p["range"][1], p["range"][2])] = p

    ref_names: set[tuple[str, str]] = set()
    ref_starts: set[tuple[str, int, int]] = set()
    counts = collections.Counter()
    stats = collections.defaultdict(collections.Counter)
    misses = collections.defaultdict(list)
    span_misses = collections.defaultdict(list)
    for r in refs:
        ref_names.add((r["m"], norm(user_name(r["n"]))))
        if "r" in r:
            ref_starts.add((r["m"], r["r"][0], r["r"][1]))
        cat = classify(r, src)
        counts[cat] += 1
        if cat not in AUTHORED and cat not in DERIVED:
            continue
        st = stats[cat]
        st["total"] += 1
        if cat == "anonymous-instance":
            p = anon_by_pos.get((r["m"], r["r"][0], r["r"][1]))
            cands = [p] if p else []
        else:
            cands = by_name.get((r["m"], norm(user_name(r["n"]))), [])
        if not cands:
            misses[cat].append(r)
            continue
        st["found"] += 1
        want_r = tuple(r["r"])
        want_s = tuple(r["s"])
        best = min(
            cands,
            key=lambda p: (
                span_lc(p["range"]) != want_r,
                span_lc(p["selection"]) != want_s,
            ),
        )
        got_r = span_lc(best["range"])
        got_s = span_lc(best["selection"])
        ok_start = got_r[:2] == want_r[:2]
        ok_end = got_r[2:] == want_r[2:]
        ok_sel = got_s == want_s
        st["range_start"] += ok_start
        st["range_end"] += ok_end
        st["selection"] += ok_sel
        st["exact"] += ok_start and ok_end and ok_sel
        if not (ok_start and ok_end and ok_sel):
            span_misses[cat].append((r, best))

    # Precision over parser declarations that should be constants, in modules the built
    # environment contains.
    ref_modules = {r["m"] for r in refs}
    prec = collections.Counter()
    false_pos = []
    for p in parsed:
        if p["kind"] in ("example", "proof_wanted") or p["module"] not in ref_modules:
            continue
        key = "generated" if p["generated"] else "authored"
        prec[key + "_total"] += 1
        if p["name"] is None:
            ok = (p["module"], p["range"][1], p["range"][2]) in ref_starts
        else:
            ok = (p["module"], norm(p["name"])) in ref_names
        prec[key + "_ok"] += ok
        if not ok:
            false_pos.append(p)

    print(
        f"modules {len(modules)} (built: {len({r['m'] for r in refs})})"
        f"  reference constants {len(refs)}  parser declarations {len(parsed)}"
    )
    print("reference categories:", dict(counts.most_common()))
    print()
    hdr = (
        f"{'category':22} {'total':>8} {'recall':>8} {'start':>8} {'end':>8}"
        f" {'select':>8} {'exact':>8}"
    )
    print(hdr)
    for cat in AUTHORED + DERIVED:
        st = stats[cat]
        t = st["total"]
        f = st["found"] or 1
        if not t:
            continue
        print(
            f"{cat:22} {t:8d} {st['found'] / t:8.4%} {st['range_start'] / f:8.4%}"
            f" {st['range_end'] / f:8.4%} {st['selection'] / f:8.4%}"
            f" {st['exact'] / f:8.4%}"
        )
    tot = collections.Counter()
    for cat in AUTHORED:
        tot.update(stats[cat])
    f = tot["found"] or 1
    print(
        f"{'all authored':22} {tot['total']:8d} {tot['found'] / tot['total']:8.4%}"
        f" {tot['range_start'] / f:8.4%} {tot['range_end'] / f:8.4%}"
        f" {tot['selection'] / f:8.4%} {tot['exact'] / f:8.4%}"
    )
    print()
    for key in ("authored", "generated"):
        t = prec[key + "_total"]
        if t:
            print(
                f"precision ({key} parser declarations): {prec[key + '_ok']}/{t}"
                f" = {prec[key + '_ok'] / t:.4%}"
            )

    if args.report:
        os.makedirs(args.report, exist_ok=True)
        with open(os.path.join(args.report, "misses.txt"), "w", encoding="utf-8") as f:
            for cat, rs in misses.items():
                f.write(f"=== {cat}: {len(rs)} missing\n")
                for r in rs:
                    f.write(f"{r['m']} {r['n']} {r['k']} {r['f']} {r['r']} {r['s']}\n")
                    f.write(
                        "    "
                        + src.text(r["m"], r["r"])[:300].replace("\n", "\n    ")
                        + "\n"
                    )
        with open(os.path.join(args.report, "spans.txt"), "w", encoding="utf-8") as f:
            for cat, rs in span_misses.items():
                f.write(f"=== {cat}: {len(rs)} span mismatches\n")
                for r, p in rs:
                    f.write(
                        f"{r['m']} {r['n']} want r={r['r']} s={r['s']}"
                        f" got r={list(span_lc(p['range']))}"
                        f" s={list(span_lc(p['selection']))}\n"
                    )
                    f.write(
                        "  WANT "
                        + src.text(r["m"], r["r"])[-200:].replace("\n", "⏎")
                        + "\n"
                    )
                    f.write(
                        "  GOT  "
                        + src.text(r["m"], list(span_lc(p["range"])))[-200:].replace(
                            "\n", "⏎"
                        )
                        + "\n"
                    )
        with open(
            os.path.join(args.report, "false_positives.txt"), "w", encoding="utf-8"
        ) as f:
            for p in false_pos:
                f.write(f"{p['module']} {p['name']} {p['kind']} {p['range'][1:3]}\n")
                f.write(
                    "    "
                    + src.text(p["module"], list(span_lc(p["range"])))[:200].replace(
                        "\n", "⏎"
                    )
                    + "\n"
                )
    return 0


if __name__ == "__main__":
    sys.exit(main())
