/- Parse sources using Lean's command parser, without elaborating declarations or proofs. -/
import Lean
open Lean Elab

partial def findKind? (s : Syntax) (k : Name) : Option Syntax :=
  if s.getKind == k then some s
  else s.getArgs.findSome? fun a => findKind? a k

partial def keyword? (s : Syntax) : Option Syntax :=
  match s with
  | .atom _ value =>
    if ["def", "lemma", "theorem", "instance", "structure", "class", "axiom", "abbrev",
        "opaque", "example", "inductive", "coinductive", "irreducible_def", "proof_wanted"].contains value then some s else none
  | _ => s.getArgs.findSome? keyword?

partial def firstAtom? (s : Syntax) : Option String :=
  match s with
  | .atom _ value => some value
  | _ => s.getArgs.findSome? firstAtom?

def offset (p : Option String.Pos.Raw) : Json :=
  match p with
  | some p => toJson p.byteIdx
  | none => Json.null

partial def declarations (s : Syntax) (file : String) (ns : Name) (errors : Array Nat) (contextTainted : Bool := false) : Array Json := Id.run do
  let k := s.getKind.toString
  if "Lean.Parser.Term.".isPrefixOf k || "Lean.Parser.Tactic.".isPrefixOf k then return #[]
  let sig? := s.getArgs.find? fun a => a.isOfKind ``Parser.Command.declSig || a.isOfKind ``Parser.Command.optDeclSig
  if let some sig := sig? then
    let name := (findKind? s ``Parser.Command.declId).map fun id => id[0].getId.toString
    let body? := s.getArgs.find? fun a => a.isOfKind ``Parser.Command.declValSimple || a.isOfKind ``Parser.Command.declValEqns || a.isOfKind ``Parser.Command.whereStructInst ||
      (s.isOfKind ``Parser.Command.structure && ((firstAtom? a).map fun atom => atom == "where" || atom == ":=").getD false)
    let sigEnd := if s.isOfKind ``Parser.Command.structure then
        ((findKind? s ``Parser.Command.extends).bind Syntax.getTailPos?) <|> sig.getTailPos? <|> (findKind? s ``Parser.Command.declId).bind Syntax.getTailPos?
      else sig.getTailPos? <|> (findKind? s ``Parser.Command.declId).bind Syntax.getTailPos? <|> (keyword? s).bind Syntax.getTailPos?
    let keywordPos := (keyword? s).bind Syntax.getPos?
    let start := keywordPos.map (·.byteIdx) |>.getD 0
    let boundary := (body?.bind Syntax.getPos?).map (·.byteIdx)
    let tainted := contextTainted || sig.hasMissing || errors.any (fun p => start ≤ p && (boundary.map (fun b => decide (p < b))).getD true)
    return #[Json.mkObj [
      ("file", toJson file), ("kind", toJson k), ("name", toJson name), ("namespace", toJson ns.toString),
      ("start", offset keywordPos), ("signature_end", offset sigEnd),
      ("end", offset s.getTailPos?), ("body_start", offset (body?.bind Syntax.getPos?)),
      ("context_tainted", toJson contextTainted), ("signature_tainted", toJson tainted), ("command_missing", toJson s.hasMissing)]]
  let mut rows := #[]
  for a in s.getArgs do rows := rows ++ declarations a file ns errors contextTainted
  return rows

partial def namespaceIds (s : Syntax) : Array Name :=
  match s with
  | `(Parser.Command.openDecl| scoped $nss*) => nss.map (·.getId)
  | `(Parser.Command.openDecl| $ns ($_ids*)) => #[ns.getId]
  | `(Parser.Command.openDecl| $ns hiding $_ids*) => #[ns.getId]
  | `(Parser.Command.openDecl| $ns renaming $[$_froms -> $_tos],*) => #[ns.getId]
  | `(Parser.Command.openDecl| $nss*) => nss.map (·.getId)
  | _ => s.getArgs.foldl (fun names a => names ++ namespaceIds a) #[]

/-- Explicit selections and renamings do not call `activateScoped` in Lean.Elab.Open. -/
partial def openGrammarEffects (s : Syntax) : Array Bool :=
  match s with
  | `(Parser.Command.openDecl| $_ns ($_ids*)) => #[false]
  | `(Parser.Command.openDecl| $_ns renaming $[$_froms -> $_tos],*) => #[false]
  | `(Parser.Command.openDecl| scoped $_nss*) => #[true]
  | `(Parser.Command.openDecl| $_ns hiding $_ids*) => #[true]
  | `(Parser.Command.openDecl| $_nss*) => #[true]
  | _ => s.getArgs.foldl (fun effects a => effects ++ openGrammarEffects a) #[]

def openChangesGrammar (s : Syntax) : Bool :=
  let effects := openGrammarEffects s
  effects.isEmpty || effects.any id

def safeCommand (s : Syntax) : Bool :=
  [``Parser.Command.namespace, ``Parser.Command.section, ``Parser.Command.end,
    ``Parser.Command.open, ``Parser.Command.syntax, ``Parser.Command.notation,
    ``Parser.Command.mixfix, ``Parser.Command.macro, ``Parser.Command.elab, ``Parser.Command.syntaxCat, `Mathlib.Tactic.scopedNS, `Mathlib.Notation3.notation3].contains s.getKind

partial def containsContextCommand (s : Syntax) : Bool :=
  safeCommand s || s.getArgs.any containsContextCommand

def skippedGrammarEffect (s : Syntax) : Bool :=
  let kind := s.getKind.toString
  kind == "Lean.Parser.Command.initialize" || kind == "Lean.Parser.Command.builtinInitialize" ||
    kind == "Lean.Parser.Command.runCmd" || kind == "Lean.Parser.Command.runElab" ||
    (!safeCommand s && s.getArgs.any containsContextCommand)

namespace Lean.Elab.Command
open Lean.Syntax
open Lean.Parser.Term hiding macroArg
open Lean.Parser.Command

/-- Register a macro's syntax production without compiling or running its implementation. -/
def elabSyntaxContext (stx : Syntax) : CommandElabM Unit := do
  if !stx.isOfKind ``Parser.Command.macro && !stx.isOfKind ``Parser.Command.elab then
    elabCommand stx
    return
  match stx with
  | `($[$doc?:docComment]? $[@[$attrs?,*]]? $attrKind:attrKind
      macro$[:$prec?]? $[(name := $name?)]? $[(priority := $prio?)]? $args:macroArg* : $cat => $_rhs) =>
    let prio ← liftMacroM <| evalOptPrio prio?
    let (stxParts, _) := (← args.mapM expandMacroArg).unzip
    let _ ← elabSyntax (← `($[$doc?:docComment]? $[@[$attrs?,*]]? $attrKind:attrKind
      syntax$[:$prec?]? $[(name := $name?)]? (priority := $(quote prio):num)
      $[$stxParts]* : $cat))
    pure ()
  | `($[$doc?:docComment]? $[@[$attrs?,*]]? $attrKind:attrKind
      elab$[:$prec?]? $[(name := $name?)]? $[(priority := $prio?)]? $args:macroArg* :
        $cat $[<= $_expectedType?]? => $_rhs) =>
    let prio ← liftMacroM <| evalOptPrio prio?
    let (stxParts, _) := (← args.mapM expandMacroArg).unzip
    let _ ← elabSyntax (← `($[$doc?:docComment]? $[@[$attrs?,*]]? $attrKind:attrKind
      syntax$[:$prec?]? $[(name := $name?)]? (priority := $(quote prio):num)
      $[$stxParts]* : $cat))
    pure ()
  | _ => throwUnsupportedSyntax

end Lean.Elab.Command

unsafe def processFiles (env : Environment) (root : String) (files : List String) (outPath : String) : IO Unit := do
  let handle ← IO.FS.Handle.mk outPath IO.FS.Mode.write
  let mut index := 0
  for file in files do
    let path := root ++ "/" ++ file
    let input ← IO.FS.readFile path
    let ictx := Parser.mkInputContext input path
    let (_, ps, headerMessages) ← Parser.parseHeader ictx
    let mut ps := ps
    let mut cmdState := Command.mkState env {} ((maxRecDepth.set {} 4096).setBool `quotPrecheck false)
    let ctx : Command.Context := { fileName := path, fileMap := ictx.fileMap, snap? := none, cancelTk? := none }
    let mut commandCount := 0
    let mut errors : Array Json := #[]
    let mut contextErrors : Array Json := #[]
    let mut contextTainted := false
    let mut namespaceRegistrations : Array Json := #[]
    let mut ignoredVariables := 0
    let mut rows := 0
    for m in headerMessages.toList do
      errors := errors.push (Json.mkObj [("message", toJson (← m.toString)), ("stage", toJson "header")])
    repeat
      let scope := cmdState.scopes.head!
      let pmctx := { env := cmdState.env, options := scope.opts, currNamespace := scope.currNamespace, openDecls := scope.openDecls }
      let (stx, ps', messages) := Parser.parseCommand ictx pmctx ps {}
      let before := ps.pos
      ps := ps'
      let mut errorPos := #[]
      for m in messages.toList do
        let pos := (ictx.fileMap.ofPosition m.pos).byteIdx
        errorPos := errorPos.push pos
        errors := errors.push (Json.mkObj [("pos", toJson pos), ("message", toJson (← m.toString)), ("stage", toJson "parse")])
      let declRows := declarations stx file scope.currNamespace errorPos contextTainted
      for row in declRows do
        handle.putStrLn row.compress
        rows := rows + 1
      if Parser.isTerminalCommand stx then break
      commandCount := commandCount + 1
      if stx.isOfKind ``Parser.Command.variable then ignoredVariables := ignoredVariables + 1
      if declRows.isEmpty && skippedGrammarEffect stx then
        contextErrors := contextErrors.push (Json.mkObj [
          ("pos", offset stx.getPos?), ("message", toJson "Skipped command may change parsing context"),
          ("command", toJson stx.getKind.toString), ("affects_grammar", toJson true),
          ("unsupported_context", toJson true)])
        contextTainted := true
      if declRows.isEmpty && safeCommand stx then
        -- Unbuilt project namespaces may precede a known scoped notation namespace in `open`.
        -- Register names only; no constants, proofs, or syntax rules are invented.
        if stx.isOfKind ``Parser.Command.open then
          for name in namespaceIds stx do
            if (Lean.ResolveName.resolveNamespace cmdState.env scope.currNamespace scope.openDecls name).isEmpty then
              namespaceRegistrations := namespaceRegistrations.push (Json.mkObj [
                ("pos", offset stx.getPos?), ("name", toJson name.toString)])
              cmdState := { cmdState with env := cmdState.env.registerNamespace name }
        let result ← ((Command.elabSyntaxContext stx).run ctx |>.run cmdState).toIO'
        let affectsGrammar := !stx.isOfKind ``Parser.Command.open || openChangesGrammar stx
        match result with
        | .ok (_, state) =>
          for message in state.messages.toList do
            if message.severity == .error then
              contextErrors := contextErrors.push (Json.mkObj [
                ("pos", toJson (ictx.fileMap.ofPosition message.pos).byteIdx),
                ("message", toJson (← message.toString)), ("command", toJson stx.getKind.toString),
                ("affects_grammar", toJson affectsGrammar)])
              contextTainted := contextTainted || affectsGrammar
          cmdState := {state with messages := {}}
        | .error error =>
          contextErrors := contextErrors.push (Json.mkObj [
            ("pos", offset stx.getPos?), ("message", toJson (← error.toMessageData.toString)),
            ("command", toJson stx.getKind.toString), ("affects_grammar", toJson affectsGrammar)])
          contextTainted := contextTainted || affectsGrammar
      if before == ps.pos then
        errors := errors.push (Json.mkObj [("message", toJson "parser made no progress"), ("stage", toJson "parse")])
        break
    handle.putStrLn (Json.mkObj [("file",toJson file),("summary",toJson true),("commands",toJson commandCount),("declarations",toJson rows),("errors",toJson errors),("audit_version",toJson (2 : Nat)),("context_tracking",toJson true),("context_errors",toJson contextErrors),("namespace_tracking",toJson true),("namespace_registrations",toJson namespaceRegistrations),("ignored_variable_commands",toJson ignoredVariables)]).compress
    index := index + 1
    if index % 1000 == 0 then IO.eprintln s!"{index}/{files.length} files"
  handle.flush
unsafe def main (args : List String) : IO UInt32 := do
  let root :: listing :: outPath :: rest := args | do
    IO.eprintln "usage: DumpSyntax ROOT FILES.txt OUT.jsonl [WORKERS]"
    return 2
  let workers := max 1 ((rest.headD "1").toNat?.getD 1)
  let searchPath ← addSearchPathFromEnv {}
  initSearchPath (← findSysroot) searchPath
  enableInitializersExecution
  let roots ← if let some importsPath := rest[1]? then do
      let names := (← IO.FS.readFile (System.FilePath.mk importsPath)).splitOn "\n" |>.filter (· != "")
      pure (names.toArray.map fun name => ({module := name.toName} : Import))
    else pure #[{module := `Mathlib}]
  let env ← importModules roots {} (trustLevel := 1024) (loadExts := true)
  let files := (← IO.FS.readFile listing).splitOn "\n" |>.filter (· != "")
  if workers == 1 then
    processFiles env root files outPath
  else
    let mut tasks := #[]
    for worker in [:workers] do
      let shard := files.zipIdx |>.filterMap fun (file, i) => if i % workers == worker then some file else none
      tasks := tasks.push (← IO.asTask (processFiles env root shard s!"{outPath}.{worker}"))
    for task in tasks do
      match task.get with
      | .ok _ => pure ()
      | .error error => throw error
  return 0
