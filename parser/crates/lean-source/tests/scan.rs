//! Declarations, names and spans on small sources. Each case comes from a construct that
//! Mathlib, Lean core or Batteries uses.

use lean_source::pos::LineIndex;
use lean_source::{DeclKind, parse};

/// `(kind, name)` of every declaration.
fn decls(src: &str) -> Vec<(DeclKind, Option<String>)> {
    parse(src.as_bytes())
        .decls
        .into_iter()
        .map(|d| (d.kind, d.name))
        .collect()
}

fn names(src: &str) -> Vec<String> {
    parse(src.as_bytes())
        .decls
        .into_iter()
        .filter_map(|d| d.name)
        .collect()
}

fn range_text(src: &str, name: &str) -> String {
    let p = parse(src.as_bytes());
    let d = p
        .decls
        .iter()
        .find(|d| d.name.as_deref() == Some(name))
        .expect(name);
    src[d.range.start as usize..d.range.end as usize].to_string()
}

fn selection_text(src: &str, name: &str) -> String {
    let p = parse(src.as_bytes());
    let d = p
        .decls
        .iter()
        .find(|d| d.name.as_deref() == Some(name))
        .expect(name);
    src[d.selection.start as usize..d.selection.end as usize].to_string()
}

#[test]
fn namespaces_and_root() {
    let src = "namespace A.B\ntheorem t : True := trivial\nsection\nprotected def d := 1\nend\ntheorem _root_.r : True := trivial\nend A.B\ndef top := 2\n";
    assert_eq!(names(src), ["A.B.t", "A.B.d", "r", "top"]);
}

#[test]
fn modifiers_and_doc_are_in_the_range() {
    let src = "/-- Doc. -/\n@[simp, norm_cast]\nprivate noncomputable def f : Nat := 1\n";
    assert_eq!(range_text(src, "f"), src.trim_end());
    assert_eq!(selection_text(src, "f"), "f");
    let p = parse(src.as_bytes());
    let attrs: Vec<_> = p
        .attrs
        .iter()
        .map(|a| &src[a.span.start as usize..a.span.end as usize])
        .collect();
    assert_eq!(attrs, ["simp", "norm_cast"]);
}

#[test]
fn prefix_commands_are_separate() {
    let src = "open Classical in\nset_option maxHeartbeats 400000 in\n/-- d -/\ntheorem t : True := by\n  open Foo in exact trivial\n";
    assert_eq!(
        range_text(src, "t"),
        "/-- d -/\ntheorem t : True := by\n  open Foo in exact trivial"
    );
}

#[test]
fn structures() {
    let src = "@[ext]\nstructure P (α : Type) extends Q α, R where\n  /-- x -/\n  protected x : α\n  y : Nat := 0\n  (u v : Nat)\n  deriving Repr\n";
    let mut n = names(src);
    n.sort();
    assert_eq!(
        n,
        [
            "P",
            "P.ext",
            "P.ext_iff",
            "P.mk",
            "P.toQ",
            "P.toR",
            "P.u",
            "P.v",
            "P.x",
            "P.y"
        ]
    );
    assert_eq!(range_text(src, "P.x"), "x");
    let ctor = "structure S where\n  mk' ::\n  a : Nat\n";
    assert_eq!(names(ctor), ["S", "S.mk'", "S.a"]);
}

#[test]
fn inductives() {
    let src = "inductive T\n  /-- first -/\n  | a\n  | local (n : Nat)\n  | /-- doc -/\n    prefix (s : Nat)\n  deriving Repr\ndef after := 1\n";
    assert_eq!(names(src), ["T", "T.a", "T.local", "T.prefix", "after"]);
    assert_eq!(range_text(src, "T.a"), "/-- first -/\n  | a");
}

#[test]
fn where_and_let_rec() {
    let src = "def f (n : Nat) : Nat := go n\nwhere\n  /-- helper -/\n  go : Nat → Nat\n    | 0 => 0\n    | k + 1 => go k\n  termination_by n => n\ndef g := by\n  let rec loop (i : Nat) : Nat :=\n    let rec inner : Nat := i\n    inner\n  exact loop 0\n";
    assert_eq!(names(src), ["f", "f.go", "g", "g.loop", "g.loop.inner"]);
    assert_eq!(
        range_text(src, "f.go"),
        "go : Nat → Nat\n    | 0 => 0\n    | k + 1 => go k"
    );
}

#[test]
fn instances() {
    let src = "instance : Inhabited Nat := ⟨0⟩\ninstance (priority := 100) named.{u} : Inhabited (ULift.{u} Nat) := ⟨⟨0⟩⟩\n";
    assert_eq!(
        decls(src),
        [
            (DeclKind::Instance, None),
            (DeclKind::Instance, Some("named".into()))
        ]
    );
    assert_eq!(selection_text(src, "named"), "named.{u}");
}

#[test]
fn keywords_used_as_names_or_text() {
    // `.end` is a field access, `| opaque` a constructor, JSX text holds `theorem`.
    let src = "namespace N\ndef e (p : P) := p.end\ninductive K\n  | opaque\ndef v := <div> loading theorem </div>\nend N\n";
    assert_eq!(names(src), ["N.e", "N.K", "N.K.opaque", "N.v"]);
}

#[test]
fn absolute_value_is_not_an_alternative() {
    let src = "theorem abs_le (a : Int) : |a| ≤ |a| := le_refl _\n";
    let p = parse(src.as_bytes());
    let d = &p.decls[0];
    assert_eq!(
        &src[d.type_.unwrap().start as usize..d.type_.unwrap().end as usize],
        "|a| ≤ |a|"
    );
}

#[test]
fn doc_comment_arguments() {
    let src = "library_note «note»\n/--\nText.\n-/\n\ntheorem t : True := trivial\n#adaptation_note\n/-- later -/\ntheorem u : True := trivial\n";
    assert_eq!(range_text(src, "t"), "theorem t : True := trivial");
    assert_eq!(range_text(src, "u"), "theorem u : True := trivial");
}

#[test]
fn syntax_and_scoped_namespaces() {
    let src = "namespace X\nscoped[Topology] notation3 (name := nhdsNE) \"𝓝[≠] \" x:100 => f x\n/-- doc -/\n@[inherit_doc]\nmacro (name := tac) \"tac\" : tactic => `(tactic| rfl)\nsyntax cat := ident\nend X\n";
    assert_eq!(names(src), ["Topology.nhdsNE", "X.tac", "X.cat"]);
    assert_eq!(
        range_text(src, "X.tac"),
        "macro (name := tac) \"tac\" : tactic => `(tactic| rfl)"
    );
}

#[test]
fn irreducible_def_and_alias() {
    let src = "irreducible_def f (lemma := f_eq) (n : Nat) : Nat := n\nirreducible_def g : Nat := 1\nalias ⟨mp, mpr⟩ := Iff.rfl\n";
    assert_eq!(names(src), ["f", "f_eq", "g", "g_def", "mp", "mpr"]);
}

#[test]
fn mutual_and_deriving_instance() {
    let src = "mutual\n  def even : Nat → Bool\n    | 0 => true\n    | n + 1 => odd n\n  def odd : Nat → Bool\n    | 0 => false\n    | n + 1 => even n\nend\nderiving instance Repr for Foo\ntheorem after : True := trivial\n";
    assert_eq!(names(src), ["even", "odd", "after"]);
}

#[test]
fn positions_follow_lean() {
    let src = "namespace ℕ\n/-- é -/\ntheorem αβ : True := trivial\n";
    let p = parse(src.as_bytes());
    let idx = LineIndex::new(src.as_bytes());
    let d = &p.decls[0];
    let s = idx.pos(src.as_bytes(), d.selection.start);
    assert_eq!((s.line, s.col), (3, 8));
    let e = idx.pos(src.as_bytes(), d.range.end);
    assert_eq!((e.line, e.col), (3, 28));
}

#[test]
fn attribute_generated_declarations() {
    let src = "namespace Foo
@[to_additive bar_add] theorem bar_mul : True := trivial
@[to_additive (attr := simp)] theorem one_mul' : True := trivial
@[simps apply] def e : Nat := 0
@[mk_iff] inductive Rel : Prop | intro
@[ext] theorem hom_ext : True := trivial
end Foo
";
    let p = parse(src.as_bytes());
    let generated: Vec<_> = p
        .decls
        .iter()
        .filter(|d| d.generated && d.origin.is_some())
        .map(|d| {
            (
                d.name.clone().unwrap(),
                &src[d.origin.unwrap().start as usize..d.origin.unwrap().end as usize],
                d.approximate,
            )
        })
        .collect();
    assert_eq!(
        generated,
        [
            ("Foo.bar_add".to_string(), "to_additive", false),
            ("Foo.zero_add'".to_string(), "to_additive", true),
            ("Foo.e_apply".to_string(), "simps", false),
            ("Foo.rel_iff".to_string(), "mk_iff", true),
            ("Foo.hom_ext_iff".to_string(), "ext", false),
        ]
    );
    let d = p
        .decls
        .iter()
        .find(|d| d.name.as_deref() == Some("Foo.bar_add"))
        .unwrap();
    assert_eq!(
        &src[d.range.start as usize..d.range.end as usize],
        "to_additive bar_add"
    );
    assert_eq!(
        &src[d.selection.start as usize..d.selection.end as usize],
        "bar_add"
    );
}

#[test]
fn escaped_names_print_like_lean() {
    let src = "namespace P
theorem «exists» : True := trivial
def «term⟪_⟫» := 1
end P
";
    assert_eq!(names(src), ["P.exists", "P.«term⟪_⟫»"]);
}
