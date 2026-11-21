# Independent command-syntax validation

`DumpSyntax.lean` parses complete Lean files with Lean's command parser. File lists must come from the corpus inventory, independently of the Rust output. The oracle records the byte boundaries of declaration signatures from Lean syntax nodes. It does not elaborate declarations or proofs.

Build it in a Lean workspace with compiled Mathlib dependencies. These commands were tested with Lean 4.32.0-rc1:

```sh
lake env lean -R parser/validation -c /tmp/DumpSyntax.c parser/validation/DumpSyntax.lean
lake env leanc -O3 -rdynamic -o /tmp/dump-syntax /tmp/DumpSyntax.c
lake env /tmp/dump-syntax CORPUS_ROOT files.txt syntax.jsonl 4 imports.txt
```

`files.txt` contains one relative `.lean` path per line. `imports.txt` is optional and contains one compiled module name per line. The default import is `Mathlib`. With more than one worker, the output is split into `syntax.jsonl.0`, `syntax.jsonl.1`, etc. Each file ends with a summary record; incomplete output is rejected by the comparator. The `-rdynamic` linker option is needed for imported Lean parser extensions.

```sh
python3 parser/validation/compare_syntax.py \
  --parsed declarations.jsonl --inventory files.txt --reference syntax.jsonl.* \
  --report-dir syntax-report --require-clean
```

The comparator requires equality between the inventory and the completed reference file set. A missing worker shard or a wholly missing final file fails validation.

Reverse counts cover authored top-level parser declarations in those files. Candidates with no reference are reported separately for files with and without Lean parse errors; unsupported declaration kinds remain explicit. These counts do not establish global precision.

Use `--strip-reference-prefix src/` when the reference inventory is rooted above the Rust corpus directory. The comparison checks the start and end byte positions of authored top-level signatures. It does not verify resolved declaration names, semantic types, generated declarations, nested structure fields, or proofs. Use the separate compiled `DeclarationRanges` validation for the declarations covered by that reference.

The oracle processes namespace, open, section, and notation commands to maintain parsing context. For `macro` and `elab` commands, it registers the syntax production and does not compile or execute the implementation. It skips variable-type elaboration, which does not register parser grammar, and disables quotation prechecks that require declarations to have been elaborated. It registers unresolved namespace operands when an unbuilt project namespace appears in an `open` command. Selected constants and renamings are not synthesized as namespaces. Lean does not activate scoped grammar for explicit selections or renamings, so failures in those forms are recorded separately from grammar failures. It records every synthesized namespace and does not invent constants or syntax rules. `--require-clean` rejects synthesized namespaces and references that do not record this audit. Imported project macros and notation may still be absent. A parsing error before a declaration body makes its signature reference tainted. Context-command errors are recorded too. Failed grammar changes and recognized unsupported grammar-changing commands taint later signatures. Older references without the current audit version cannot pass `--require-clean`. These declarations remain in the report denominator and are excluded from the conditional exact-boundary count. Without `--require-clean`, a zero exit code means all usable references match; it does not mean the entire corpus has been validated.

Imports matter even when proofs are not elaborated. Importing the whole Mathlib umbrella can introduce syntax absent from a file's actual imports. For example, Hyperreal's `ℝ*` notation changes tokenization of an inner product followed immediately by multiplication. Retry such files with their actual compiled imports, and retain the provenance of replacements. Do not alter the source to make it parse. Different Lean and Mathlib versions can also leave unresolved reference cases; report those cases explicitly. Custom command elaborators are not executed, so this workflow does not reconstruct arbitrary runtime changes to the language.
Run the comparison failure-gate tests with:

```sh
python3 -m unittest discover -s parser/validation/tests -p test_compare_syntax.py
```

`tests/OpenContext.lean` checks namespace operands and the distinction between scoped opens and explicit constant selections. Compile `DumpSyntax.lean` to an `.olean`, add its directory to `LEAN_PATH` within the same `lake env`, then run Lean on the test file.