//! References of the declarations of small packages, resolved across files.

use lean_source::refs::{SourceFile, resolve_references};
use lean_source::{Parsed, parse};
use std::collections::BTreeMap;

/// Resolves a package given as `(module, source)` pairs and returns the references of each
/// named declaration, keyed by `module:name`. A name declared twice in a module keeps the
/// references of its first declaration.
fn references_with(
    files: &[(&str, &str)],
    known: &[&str],
    include_approximate: bool,
) -> BTreeMap<String, Vec<String>> {
    let parsed: Vec<Parsed> = files.iter().map(|(_, s)| parse(s.as_bytes())).collect();
    let sources: Vec<SourceFile> = files
        .iter()
        .zip(&parsed)
        .map(|((module, source), parsed)| SourceFile {
            module,
            source: source.as_bytes(),
            parsed,
        })
        .collect();
    let resolved = resolve_references(&sources, known, include_approximate);
    let mut found = BTreeMap::new();
    for ((module, _), (parsed, references)) in files.iter().zip(parsed.iter().zip(resolved)) {
        assert_eq!(parsed.decls.len(), references.len());
        for (declaration, names) in parsed.decls.iter().zip(references) {
            if let Some(name) = &declaration.name {
                let names = names.into_iter().map(str::to_string).collect();
                found.entry(format!("{module}:{name}")).or_insert(names);
            }
        }
    }
    found
}

fn references(files: &[(&str, &str)], known: &[&str]) -> BTreeMap<String, Vec<String>> {
    references_with(files, known, false)
}

const NONE: [&str; 0] = [];

#[test]
fn references_within_a_file_follow_declaration_order() {
    let source = "abbrev T := Nat\nnamespace A\ndef f : T := 1\ntheorem T : f = f := rfl\n\
                  def g : T = T := T\nend A\n";
    let found = references(&[("M", source)], &[]);
    assert_eq!(found["M:T"], NONE);
    assert_eq!(found["M:A.f"], ["T"]);
    assert_eq!(found["M:A.T"], ["A.f"]);
    assert_eq!(found["M:A.g"], ["A.T"]);
}

#[test]
fn references_across_files_follow_imports() {
    let files = [
        ("Lower", "def base := 1\n"),
        (
            "Upper",
            "import Lower\nimport Elsewhere\ndef uses : base = Nat := sorry\n",
        ),
        ("Apart", "def misses : base := sorry\n"),
        ("Top", "import Upper\ndef far : base := sorry\n"),
    ];
    let found = references(&files, &["Nat"]);
    assert_eq!(found["Lower:base"], NONE);
    assert_eq!(found["Upper:uses"], ["base", "Nat"]);
    assert_eq!(found["Apart:misses"], NONE);
    assert_eq!(found["Top:far"], ["base"]);
}

#[test]
fn private_names_shadow_in_their_module_only() {
    let files = [
        (
            "M",
            "abbrev T := Nat\nnamespace A\nprivate abbrev T := Nat\ndef f : T := 1\nend A\n",
        ),
        ("N", "import M\nnamespace A\ndef g : T := 1\nend A\n"),
    ];
    let found = references(&files, &[]);
    // The private name is reported, so that a caller can tell it from the root name.
    assert_eq!(found["M:A.f"], ["A.T"]);
    assert_eq!(found["N:A.g"], ["T"]);
}

#[test]
fn a_known_name_is_not_private() {
    let files = [
        ("M", "namespace A\nprivate abbrev T := Nat\nend A\n"),
        ("N", "import M\nnamespace A\ndef g : T := 1\nend A\n"),
    ];
    assert_eq!(references(&files, &[])["N:A.g"], NONE);
    assert_eq!(references(&files, &["A.T"])["N:A.g"], ["A.T"]);
}

#[test]
fn protected_names_need_their_namespace() {
    let source = "abbrev T := Nat\nnamespace B\nprotected abbrev T := Nat\ndef g : T := 1\n\
                  def h : B.T := 1\nend B\n";
    let found = references(&[("M", source)], &[]);
    assert_eq!(found["M:B.g"], ["T"]);
    assert_eq!(found["M:B.h"], ["B.T"]);
}

#[test]
fn a_name_declared_twice_keeps_its_first_declaration() {
    let files = [
        ("First", "def a := 1\n"),
        ("Second", "def a := 2\n"),
        ("User", "import Second\ndef b : a = a := rfl\n"),
        ("Other", "import First\ndef c : a = a := rfl\n"),
    ];
    let found = references(&files, &[]);
    assert_eq!(found["User:b"], NONE);
    assert_eq!(found["Other:c"], ["a"]);
}

#[test]
fn a_declaration_can_refer_to_itself() {
    let found = references(&[("M", "def a := 1\ndef a : a = a := rfl\n")], &[]);
    assert_eq!(found["M:a"], NONE);
    // The second declaration, which a caller drops, sees the first one.
    let source = "def a := 1\ndef a : a = a := rfl\n";
    let parsed = parse(source.as_bytes());
    let file = SourceFile {
        module: "M",
        source: source.as_bytes(),
        parsed: &parsed,
    };
    let again = resolve_references(&[file], &NONE[..], false);
    assert_eq!(again, [vec![vec![], vec!["a"]]]);
}

#[test]
fn constructor_refers_to_its_type() {
    let source = "inductive Color where\n  | red\n  | mix (a b : Color)\n";
    let found = references(&[("M", source)], &[]);
    assert_eq!(found["M:Color"], NONE);
    assert_eq!(found["M:Color.red"], ["Color"]);
    assert_eq!(found["M:Color.mix"], ["Color"]);
}

#[test]
fn fields_use_their_owner() {
    let source = "def val := 1\nnamespace N\ndef Good (n : Nat) := True\n\
                  structure S (α : Type) where\n  val : α\n  other : val = val\n  good : Good 1\n\
                  end N\n";
    let found = references(&[("M", source)], &["Nat"]);
    // The structure's binder is local to its fields, a sibling field wins over the root
    // name, and names are looked up where the structure is.
    assert_eq!(found["M:N.S.val"], NONE);
    assert_eq!(found["M:N.S.other"], ["N.S.val"]);
    assert_eq!(found["M:N.S.good"], ["N.Good"]);
    assert_eq!(found["M:N.Good"], ["Nat"]);
}

#[test]
fn opens_variables_and_exports_of_the_file_are_used() {
    let source = "namespace Lib\ndef helper := 1\nclass Good (α : Type) where\nend Lib\n\
                  namespace Tools\ndef tool := 1\nend Tools\n\
                  namespace User\nexport Tools (tool)\nend User\n\
                  open Lib\nvariable {α : Type} [Good α]\n\
                  theorem uses (a : α) : helper = helper := rfl\n\
                  open Missing in\ntheorem plain : helper = 1 := rfl\n\
                  namespace User\ntheorem exported : tool = 1 := rfl\nend User\n";
    let found = references(&[("M", source)], &[]);
    assert_eq!(found["M:uses"], ["Lib.helper", "Lib.Good"]);
    assert_eq!(found["M:plain"], ["Lib.helper"]);
    assert_eq!(found["M:User.exported"], ["Tools.tool"]);
}

#[test]
fn approximate_names_are_referred_to_only_on_request() {
    let source = "@[to_additive]\ntheorem mul_thing : True := trivial\n\
                  theorem uses : add_thing = add_thing := rfl\n";
    let exact = references(&[("M", source)], &[]);
    assert_eq!(exact["M:uses"], NONE);
    assert_eq!(exact["M:add_thing"], NONE);
    let approximate = references_with(&[("M", source)], &[], true);
    assert_eq!(approximate["M:uses"], ["add_thing"]);
}
