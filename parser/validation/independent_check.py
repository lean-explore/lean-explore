"""Check parser spans against Lean's declaration ranges without using compare.py.

Usage:
    python independent_check.py RANGES.jsonl PARSER.jsonl SOURCE_ROOT

Every reference constant with a range that is not internal is looked up in the parser
output by module and name, then by range. A constant that is not found counts as a miss
when its name is written in the source at its selection range.
"""

import collections
import json
import random
import re
import sys

ATTRIBUTE_BEFORE = re.compile(r"@\[\s*([A-Za-z_]+)[^\]]*$")
WORD_BEFORE = re.compile(r"([A-Za-z_]+)\s*(\([^)]*\)\s*)*$")


def strip_private(name):
    """Drop Lean's private-name prefix `_private.<module>.0.`."""
    if name.startswith("_private."):
        parts = name.split(".")
        if "0" in parts[1:]:
            i = parts.index("0", 1)
            return ".".join(parts[i + 1 :])
    return name


def line_col(span):
    """Return the line and column fields of a parser span."""
    return span[1], span[2], span[4], span[5]


def load_parser(path):
    """Index parser declarations by (module, name) and by (module, range)."""
    by_name = collections.defaultdict(list)
    by_range = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            sel = d["selection"]
            rec = (line_col(d["range"]), line_col(sel) if sel else None)
            by_name[(d["module"], d["name"])].append(rec)
            by_range[(d["module"], *line_col(d["range"]))] = rec
    return by_name, by_range


class Sources:
    """Read source lines of modules under a root directory, with a cache."""

    def __init__(self, root):
        """Use `root` as the directory that holds the module files."""
        self.root = root
        self.cache = {}

    def lines(self, module):
        """Return the lines of a module's source file, or None if it is missing."""
        if module not in self.cache:
            path = f"{self.root}/{module.replace('.', '/')}.lean"
            try:
                with open(path, encoding="utf-8") as f:
                    self.cache[module] = f.read().split("\n")
            except OSError:
                self.cache[module] = None
        return self.cache[module]

    def selection_text(self, module, sel):
        """Return the source text of a single-line selection, or None."""
        lines = self.lines(module)
        if lines is None or sel[0] != sel[2] or sel[0] - 1 >= len(lines):
            return None
        return lines[sel[0] - 1][sel[1] : sel[3]]


def is_written(name, text):
    """Return True if `text` spells the constant's name or its last components."""
    if text is None or text.strip("«»") == "":
        return False
    plain = text.strip("«»")
    return name == text or name.endswith("." + text) or name.endswith("." + plain)


def compare(ref_path, by_name, by_range, sources):
    """Count matches and collect misses over the reference constants."""
    stats = collections.Counter()
    misses = []
    span_bad = []
    with open(ref_path, encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            if "r" not in d or "internal" in d["f"]:
                continue
            module, name = d["m"], strip_private(d["n"])
            rng, sel = tuple(d["r"]), tuple(d["s"])
            stats["ref_with_range"] += 1
            cands = by_name.get((module, name))
            if cands:
                stats["found_by_name"] += 1
                if any(c[0] == rng for c in cands):
                    stats["range_exact"] += 1
                else:
                    span_bad.append((module, name, rng, [c[0] for c in cands]))
                if any(c[1] == sel for c in cands):
                    stats["selection_exact"] += 1
            elif (module, *rng) in by_range:
                stats["found_by_position_only"] += 1
            elif is_written(name, sources.selection_text(module, sel)):
                stats["MISS_name_written_in_source"] += 1
                misses.append((module, name, sel))
            else:
                stats["not_found_name_not_written"] += 1
    return stats, misses, span_bad


def miss_contexts(misses, sources):
    """Group misses by the attribute or word that precedes the written name."""
    counts = collections.Counter()
    examples = {}
    for module, _, sel in misses:
        text = sources.lines(module)[sel[0] - 1]
        before = text[: sel[1]]
        match = ATTRIBUTE_BEFORE.search(before) or WORD_BEFORE.search(before)
        key = match.group(1) if match else before[-20:]
        counts[key] += 1
        examples.setdefault(key, text.strip()[:110])
    return counts, examples


def main():
    """Print the counts, exact rates, samples and the contexts of misses."""
    ref_path, parser_path, root = sys.argv[1:4]
    by_name, by_range = load_parser(parser_path)
    sources = Sources(root)
    stats, misses, span_bad = compare(ref_path, by_name, by_range, sources)
    for key, value in stats.items():
        print(f"{key:32} {value}")
    found = max(1, stats["found_by_name"])
    range_rate = 100 * stats["range_exact"] / found
    selection_rate = 100 * stats["selection_exact"] / found
    print(f"range exact / found by name: {range_rate:.4f}%")
    print(f"selection exact / found by name: {selection_rate:.4f}%")
    random.seed(0)
    print("sample misses:", random.sample(misses, min(8, len(misses))))
    print("sample span mismatches:", random.sample(span_bad, min(5, len(span_bad))))
    counts, examples = miss_contexts(misses, sources)
    for key, value in counts.most_common(15):
        print(f"{value:6}  {key:20} e.g. {examples[key]}")


if __name__ == "__main__":
    main()
