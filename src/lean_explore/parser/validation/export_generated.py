"""Export attribute-generated names and ranges from a built Lean environment.

This is an optional semantic sidecar, not an inference made by the Rust parser.
Generate the input with DumpRanges.lean from a build matching SOURCE_ROOT. The
output records preserve Lean's names and source ranges and fingerprint the source
files. Missing source and invalid positions on selected records are errors.
Ranges whose start cannot be attributed to a source token are listed in metadata.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from itertools import combinations
from pathlib import Path

ATTRIBUTE = re.compile(r"([^\W\d][\w'.!?]*)")
CHARACTER = re.compile(r"'(?:\\.|[^\\'\n])'")
RAW_STRING = re.compile(r'r(#+)?"')


def lexical_tokens(text: str) -> list[tuple[int, str, bool]]:
    """Tokenize enough Lean syntax to locate attribute heads outside literals."""
    tokens = []
    i = 0
    while i < len(text):
        if text.startswith("--", i):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end + 1
            continue
        if text.startswith("/-", i):
            depth = 1
            i += 2
            while i < len(text) and depth:
                if text.startswith("/-", i):
                    depth += 1
                    i += 2
                elif text.startswith("-/", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            continue
        if text[i].isspace():
            i += 1
            continue
        start = i
        raw = RAW_STRING.match(text, i)
        character = CHARACTER.match(text, i)
        if raw:
            terminator = '"' + (raw.group(1) or "")
            end = text.find(terminator, raw.end())
            i = len(text) if end < 0 else end + len(terminator)
            tokens.append((start, "<literal>", False))
            continue
        if character:
            i = character.end()
            tokens.append((start, "<literal>", False))
            continue
        if text[i] == '"':
            i += 1
            while i < len(text):
                if text[i] == "\\":
                    i += 2
                elif text[i] == '"':
                    i += 1
                    break
                else:
                    i += 1
            tokens.append((start, "<literal>", False))
            continue
        if text[i] == "«":
            end = text.find("»", i + 1)
            i = len(text) if end < 0 else end + 1
            tokens.append((start, "<quoted-name>", False))
            continue
        match = ATTRIBUTE.match(text, i)
        if match:
            tokens.append((start, match.group(1), True))
            i = match.end()
        else:
            tokens.append((start, text[i], False))
            i += 1
    return tokens


def attribute_tokens(text: str) -> dict[int, tuple[str, str]]:
    """Find direct attribute heads and nested `(attr := ...)` configurations."""
    result = {}
    tokens = lexical_tokens(text)
    pending = False
    stack = []
    expecting = {}
    i = 0
    while i < len(tokens):
        start, token, identifier = tokens[i]
        i += 1
        if stack:
            if token in ("(", "[", "{"):
                stack.append(token)
                if token == "(" and [t[1] for t in tokens[i : i + 3]] == [
                    "attr",
                    ":",
                    "=",
                ]:
                    expecting[len(stack)] = True
                    i += 3
            elif token in (")", "]", "}"):
                expecting.pop(len(stack), None)
                stack.pop()
            elif token == "," and len(stack) in expecting:
                expecting[len(stack)] = True
            elif expecting.get(len(stack)) and token not in ("local", "scoped"):
                if identifier:
                    kind = "direct" if len(stack) == 1 else "nested-attr"
                    result[start] = (token, kind)
                expecting[len(stack)] = False
            continue
        if pending and token == "[":
            stack.append("[")
            expecting[1] = True
            pending = False
        else:
            pending = token in ("@", "attribute")
    return result


class Source:
    """Convert Lean's Unicode line/column positions to UTF-8 byte offsets."""

    def __init__(self, data: bytes):
        """Cache source positions and compute its content fingerprint."""
        self.text = data.decode("utf-8")
        self.lines = self.text.split("\n")
        self.character_starts = [0]
        for line in self.lines[:-1]:
            self.character_starts.append(self.character_starts[-1] + len(line) + 1)
        self.attributes = None
        self.starts = [0]
        for line in self.lines[:-1]:
            self.starts.append(self.starts[-1] + len(line.encode("utf-8")) + 1)
        self.sha256 = hashlib.sha256(data).hexdigest()

    def attribute_at(self, line: int, column: int) -> tuple[str, str] | None:
        """Return an attribute only when the position is its lexical head."""
        if self.attributes is None:
            self.attributes = attribute_tokens(self.text)
        return self.attributes.get(self.character_starts[line - 1] + column)

    def point(self, line: int, column: int) -> int:
        """Reject positions outside the source, including invalid Unicode columns."""
        if not 1 <= line <= len(self.lines) or not 0 <= column <= len(
            self.lines[line - 1]
        ):
            raise ValueError(f"Invalid source position: {line}:{column}")
        return self.starts[line - 1] + len(
            self.lines[line - 1][:column].encode("utf-8")
        )

    def span(self, positions: list[int]) -> list[int]:
        """Use the same six-element byte/line/column schema as lean-source."""
        if len(positions) != 4:
            raise ValueError(f"Invalid range: {positions}")
        sl, sc, el, ec = positions
        start, end = self.point(sl, sc), self.point(el, ec)
        if start > end:
            raise ValueError(f"Reversed range: {positions}")
        return [start, sl, sc, end, el, ec]


def source_name(name: str) -> str:
    """Remove a private prefix while retaining the original name in kernel_name."""
    if name.startswith("_private."):
        parts = name.split(".")
        try:
            return ".".join(parts[parts.index("0", 1) + 1 :])
        except ValueError:
            pass
    return name


def export(
    ranges: Path, root: Path, attributes: set[str], prefix: str
) -> tuple[list[dict], dict]:
    """Extract authoritative generated records; no type or name is guessed."""
    root = root.resolve()
    sources: dict[str, Source] = {}
    records = []
    seen = set()
    counts = {attribute: 0 for attribute in sorted(attributes)}
    internal = 0
    attribution_counts = {"direct": 0, "nested-attr": 0}
    candidates = 0
    unattributed_tokens = []
    missing_ranges = 0
    unattributed_positions = []
    with ranges.open(encoding="utf-8") as stream:
        for line in stream:
            reference = json.loads(line)
            module = reference["m"]
            if module != prefix and not module.startswith(prefix + "."):
                continue
            if "r" not in reference:
                missing_ranges += 1
                continue
            file = module.replace(".", "/") + ".lean"
            if module not in sources:
                path = (root / file).resolve()
                if not path.is_relative_to(root):
                    raise ValueError(f"Module escapes source root: {module}")
                sources[module] = Source(path.read_bytes())
            source = sources[module]
            sl, sc, _, _ = reference["r"]
            try:
                source.point(sl, sc)
            except ValueError:
                unattributed_positions.append(
                    {"module": module, "name": reference["n"], "range": reference["r"]}
                )
                continue
            match = ATTRIBUTE.match(source.lines[sl - 1][sc:])
            if match is None or match.group(1) not in attributes:
                continue
            origin = match.group(1)
            candidates += 1
            attribution = source.attribute_at(sl, sc)
            if attribution is None or attribution[0] != origin:
                unattributed_tokens.append(
                    {"module": module, "name": reference["n"], "range": reference["r"]}
                )
                continue
            attribution_counts[attribution[1]] += 1
            key = (module, reference["n"])
            if key in seen:
                raise ValueError(f"Duplicate reference constant: {key}")
            seen.add(key)
            is_internal = "internal" in reference.get("f", [])
            internal += is_internal
            counts[origin] += 1
            records.append(
                {
                    "file": file,
                    "module": module,
                    "name": source_name(reference["n"]),
                    "kernel_name": reference["n"],
                    "kind": reference["k"],
                    "generated": True,
                    "approximate": False,
                    "internal": is_internal,
                    "origin": origin,
                    "attribution": attribution[1],
                    "range": source.span(reference["r"]),
                    "selection": source.span(reference["s"]),
                    "source_sha256": source.sha256,
                    "provenance": "lean-environment",
                }
            )
    records.sort(key=lambda d: (d["file"], d["range"][0], d["kernel_name"]))
    return records, {
        "schema": "lean-generated-sidecar-v1",
        "provenance": "lean-environment",
        "source_root": str(root),
        "reference_sha256": hashlib.sha256(ranges.read_bytes()).hexdigest(),
        "records": len(records),
        "internal_records": internal,
        "selected_token_candidates": candidates,
        "by_attribution": attribution_counts,
        "unattributed_token_candidates": unattributed_tokens,
        "by_attribute": counts,
        "reference_constants_without_ranges": missing_ranges,
        "unattributed_source_positions": unattributed_positions,
        "limitation": (
            "Requires a built environment matching these sources; constants without "
            "ranges cannot be attributed to an attribute by this export."
        ),
    }


def main() -> int:
    """Write the semantic sidecar and its provenance manifest."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ranges", type=Path)
    parser.add_argument("source_root", type=Path)
    parser.add_argument("--attributes", nargs="+", default=["simps", "simps!"])
    parser.add_argument("--prefix", default="Mathlib")
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()
    metadata_path = args.output.with_suffix(args.output.suffix + ".meta.json")
    paths = (args.ranges, args.output, metadata_path)
    for left, right in combinations(paths, 2):
        same_path = left.resolve() == right.resolve()
        same_file = left.exists() and right.exists() and left.samefile(right)
        if same_path or same_file:
            parser.error("reference, output and metadata must refer to different files")
    records, metadata = export(
        args.ranges, args.source_root, set(args.attributes), args.prefix
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                key: metadata[key]
                for key in ("records", "internal_records", "by_attribute")
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
