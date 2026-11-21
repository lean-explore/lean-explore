"""Rebuild one stale reference module in an artifact overlay and refresh its ranges.

The source file is compiled unchanged under its original module name. Existing
artifacts are linked into the overlay; the shared build is never modified.
Requires Linux, Lake, and an already built dependency environment.
"""

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path


def main() -> None:
    """Compile an unchanged module and replace only its reference records."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--artifacts-root", type=Path, required=True)
    parser.add_argument("--module", required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    for name in (
        "workspace",
        "source_root",
        "artifacts_root",
        "reference",
        "output",
        "work",
    ):
        setattr(args, name, getattr(args, name).resolve())
    if args.output == args.reference:
        parser.error("output must differ from the reference input")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    parts = args.module.split(".")
    source = args.source_root.joinpath(*parts).with_suffix(".lean")
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    overlay = args.work / "artifacts"
    if overlay.exists() or overlay.is_symlink():
        parser.error("work/artifacts already exists; choose a fresh --work directory")
    overlay.mkdir(parents=True)
    # Lean selects a search root by module prefix, so link siblings along the
    # whole module path instead of relying on a per-file fallback search root.
    for depth in range(len(parts)):
        existing = args.artifacts_root.joinpath(*parts[:depth])
        target = overlay.joinpath(*parts[:depth])
        target.mkdir(parents=True, exist_ok=True)
        for child in existing.iterdir():
            if depth < len(parts) - 1 and child.name == parts[depth]:
                continue
            if depth == len(parts) - 1 and child.name.startswith(parts[-1] + "."):
                continue
            link = target / child.name
            if not link.exists() and not link.is_symlink():
                link.symlink_to(child, target_is_directory=child.is_dir())
    output_module = overlay.joinpath(*parts).with_suffix(".olean")
    compile_command = [
        "lake",
        "env",
        "lean",
        "--root=" + str(args.source_root),
        "-o",
        str(output_module),
        str(source),
    ]
    subprocess.run(compile_command, cwd=args.workspace, check=True)
    if hashlib.sha256(source.read_bytes()).hexdigest() != source_hash:
        raise RuntimeError("Source changed while its reference module was compiling")
    lean_path = subprocess.check_output(
        ["lake", "env", "printenv", "LEAN_PATH"], cwd=args.workspace, text=True
    ).strip()
    prefix = subprocess.check_output(
        ["lake", "env", "lean", "--print-prefix"], cwd=args.workspace, text=True
    ).strip()
    environment = dict(os.environ, LEAN_PATH=str(overlay) + ":" + lean_path)
    dump = Path(__file__).with_name("DumpRanges.lean")
    module_reference = args.work / "module-ranges.jsonl"
    dump_command = [
        str(Path(prefix) / "bin/lean"),
        "--run",
        str(dump),
        str(module_reference),
        args.module,
        args.module,
    ]
    subprocess.run(dump_command, cwd=args.workspace, env=environment, check=True)
    fresh = [json.loads(line) for line in module_reference.open(encoding="utf-8")]
    if any(row["m"] != args.module for row in fresh):
        raise ValueError("Reference dump includes a different module")
    previous = []
    with args.output.open("w", encoding="utf-8") as handle:
        for line in args.reference.open(encoding="utf-8"):
            row = json.loads(line)
            if row["m"] == args.module:
                previous.append(row)
            else:
                handle.write(line)
        for row in fresh:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    provenance = {
        "module": args.module,
        "source": str(source),
        "source_sha256": source_hash,
        "compile_command": compile_command,
        "dump_command": dump_command,
        "previous_records": previous,
        "fresh_records": fresh,
    }
    args.output.with_suffix(".provenance.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
