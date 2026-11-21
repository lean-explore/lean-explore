/-
Variant of DumpRanges.lean for toolchains before `importModules` took `loadExts` (v4.9 to
v4.15). DumpRangesV40.lean adapts it further for v4.0.

Dump the declaration ranges stored in a built Lean environment, one JSON object per
constant, for comparison with the source parser.

Usage, from a Lake workspace whose build contains the modules:

    lake env lean --run DumpRanges.lean OUT.jsonl ROOT_MODULES MODULE_PREFIX

ROOT_MODULES is a comma-separated list such as `Init,Std,Lean`.

Positions use Lean's convention: 1-based lines, 0-based columns in code points.
-/
import Lean

open Lean Meta

def posJson (p : Position) : Json := toJson #[p.line, p.column]

def rangeJson (r : DeclarationRange) : Json :=
  toJson #[r.pos.line, r.pos.column, r.endPos.line, r.endPos.column]

def kindOf : ConstantInfo → String
  | .axiomInfo _ => "axiom"
  | .defnInfo _ => "def"
  | .thmInfo _ => "thm"
  | .opaqueInfo _ => "opaque"
  | .quotInfo _ => "quot"
  | .inductInfo _ => "induct"
  | .ctorInfo _ => "ctor"
  | .recInfo _ => "rec"

unsafe def main (args : List String) : IO UInt32 := do
  let [outPath, root, pfx] := args | do
    IO.eprintln "usage: DumpRanges OUT.jsonl ROOT_MODULE MODULE_PREFIX"
    return 2
  let searchPath ← addSearchPathFromEnv {}
  initSearchPath (← findSysroot) searchPath
  enableInitializersExecution
  let roots := ((root.splitOn ",").map fun r => ({ module := r.toName } : Import)).toArray
  let env ← importModules roots {} (trustLevel := 1024)
  let handle ← IO.FS.Handle.mk outPath IO.FS.Mode.write
  let names := env.header.moduleNames
  let mut emitted := 0
  let mut noRange := 0
  -- Each module's own constants, from its module data: this works across Lean versions.
  for idx in [0:names.size] do
   let modName := names[idx]!
   unless pfx.isPrefixOf modName.toString do continue
   let data := env.header.moduleData[idx]!
   for name in data.constNames do
    let some info := env.find? name | continue
    let flags : Array String := #[]
      |>.push (if isPrivateName name then "private" else "")
      |>.push (if name.isInternal then "internal" else "")
      |>.push (if isAuxRecursor env name then "auxrec" else "")
      |>.push (if isNoConfusion env name then "noconf" else "")
      |>.push (if env.isProjectionFn name then "proj" else "")
      |>.push (if isStructure env name then "struct" else "")
      |>.push (if isInstanceCore env name then "inst" else "")
      |>.push (if isClass env name then "class" else "")
    let flags := flags.filter (· != "")
    match declRangeExt.find? env name with
    | some ranges =>
      emitted := emitted + 1
      handle.putStrLn (Json.mkObj [
        ("m", toJson modName.toString), ("n", toJson name.toString),
        ("k", toJson (kindOf info)), ("f", toJson flags),
        ("r", rangeJson ranges.range), ("s", rangeJson ranges.selectionRange)]).compress
    | none =>
      noRange := noRange + 1
      handle.putStrLn (Json.mkObj [
        ("m", toJson modName.toString), ("n", toJson name.toString),
        ("k", toJson (kindOf info)), ("f", toJson flags)]).compress
  handle.flush
  IO.eprintln s!"DumpRanges: {emitted} with ranges, {noRange} without"
  return 0
