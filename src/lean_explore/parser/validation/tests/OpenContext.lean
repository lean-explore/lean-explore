import DumpSyntax

open Lean

/-- Check the distinction between namespace operands and selected constant names. -/
unsafe def checkOpenContextSyntax : IO Unit := do
  let searchPath ← addSearchPathFromEnv {}
  initSearchPath (← findSysroot) searchPath
  enableInitializersExecution
  let env ← importModules #[{module := `Lean}] {} (loadExts := true)
  let cases : Array (String × Array Name × Bool) := #[
    ("open A B.C", #[`A, `B.C], true),
    ("open scoped A B.C", #[`A, `B.C], true),
    ("open A (selected other)", #[`A], false),
    ("open A hiding selected other", #[`A], true),
    ("open A renaming selected -> renamed, other -> alias", #[`A], false)]
  for (input, expectedNames, expectedGrammar) in cases do
    let stx ← match Parser.runParserCategory env `command input with
      | .ok stx => pure stx
      | .error message => throw (IO.userError message)
    unless namespaceIds stx == expectedNames do
      throw (IO.userError s!"wrong namespace operands for {input}: {namespaceIds stx}")
    unless openChangesGrammar stx == expectedGrammar do
      throw (IO.userError s!"wrong scoped-grammar classification for {input}")

#eval checkOpenContextSyntax