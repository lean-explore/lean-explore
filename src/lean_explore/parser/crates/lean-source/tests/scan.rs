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
            ("Foo.e_apply".to_string(), "simps", true),
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

fn assert_signature_body(signature: &str, expected_type: &str, body: &str) {
    for separator in ["\n", " "] {
        let src = format!("{signature}{separator}{body}\n");
        let parsed = parse(src.as_bytes());
        let d = &parsed.decls[0];
        assert_eq!(
            &src[d.signature.start as usize..d.signature.end as usize],
            signature,
            "{src}"
        );
        let span = d.body.expect("body");
        assert_eq!(&src[span.start as usize..span.end as usize], body, "{src}");
        let span = d.type_.expect("type");
        let expected_type = expected_type.trim();
        assert_eq!(
            &src[span.start as usize..span.end as usize],
            expected_type,
            "{src}"
        );
    }
}

#[test]
fn multiline_absolute_values_stay_in_the_signature() {
    for ty in [
        "\n    |x| = |x|",
        "\n    |x\n      + 1| = |x + 1|",
        "\n    |x\n    | = |x|",
        "\n    0 ≤\n      |x|",
        "\n    |x| = (fun y => |y|) x",
        "\n    |x| = |x| ∧\n      |x + 1| = |x + 1|",
    ] {
        assert_signature_body(
            &format!("theorem abs_test (x : Int) : {ty}"),
            ty,
            ":= by simp",
        );
    }
}

#[test]
fn equation_bodies_can_start_on_the_same_or_following_line() {
    for body in [
        "| 0 => 0\n  | n + 1 => n",
        "| 0\n      => 0\n  | n + 1 => n",
        "| 0 | 1 => 0\n  | n + 2 => n",
        "| (n : Nat) => n",
    ] {
        assert_signature_body("def f : Nat → Nat", "Nat → Nat", body);
    }
    let src = "def f : Nat → Nat | n => n";
    let parsed = parse(src.as_bytes());
    let d = &parsed.decls[0];
    assert_eq!(&src[..d.signature.end as usize], "def f : Nat → Nat");
    assert_eq!(&src[d.body.unwrap().start as usize..], "| n => n");
}

#[test]
fn tactic_assignments_in_local_bindings_stay_in_the_signature() {
    for ty in [
        "∀ (hne : Nonempty Nat),\n    haveI : Nonempty Nat := by\n      obtain ⟨x⟩ := hne\n      exact ⟨x⟩\n    True",
        "let n : Nat := by\n      let x := 1\n      have h : x = 1 := rfl\n      exact x\n    n = n",
        "have h : True := by\n      have h' : True := by\n        let n := 1\n        trivial\n      exact h'\n    True",
    ] {
        assert_signature_body(&format!("theorem local_test : {ty}"), ty, ":= by trivial");
    }
}

#[test]
fn lambdas_in_types_do_not_swallow_equation_bodies() {
    for ty in [
        "∀ n : Nat, (fun x => x + n) = fun x => n + x",
        "∀ n : Nat, Injective fun x : Int ↦ x + n",
        "∀ x : Int, |x| = |x|",
        "∀ x : Int, P |x|",
    ] {
        assert_signature_body(&format!("theorem t : {ty}"), ty, "| _ => by simp");
    }
}

#[test]
fn absolute_values_before_lambdas_stay_in_the_type() {
    let ty = "|f| =ᵐ[μ] fun x => |f x|";
    assert_signature_body(&format!("theorem t : {ty}"), ty, ":= by simp");
}

#[test]
fn tactic_terms_in_types_can_contain_assignments() {
    let ty = "∀ (h : True), P <| fun x => by\n    obtain ⟨a, b⟩ := x\n    exact h";
    assert_signature_body(&format!("theorem t : {ty}"), ty, ":= by trivial");
}

#[test]
fn many_absolute_values_preserve_the_whole_signature() {
    let sum = std::iter::repeat_n("|x|", 4096)
        .collect::<Vec<_>>()
        .join(" + ");
    let ty = format!("{sum} = {sum}");
    assert_signature_body(&format!("theorem t (x : Int) : {ty}"), &ty, ":= rfl");
}

#[test]
fn tactic_type_before_structure_instance_body() {
    let ty = "by\n  letI := instInhabitedNat\n  exact Inhabited Nat";
    assert_signature_body(
        &format!("instance local_test : {ty}"),
        ty,
        "where\n  default := 0",
    );
}

#[test]
fn matches_in_return_types_end_before_dedented_equations() {
    for ty in [
        "∀ b : Bool,\n    match b with\n    | true => Nat\n    | false => Bool",
        "∀ b : Bool,\n    match b with\n    | true =>\n      match b with\n      | true => Nat\n      | false => Nat\n    | false => Bool",
    ] {
        let signature = format!("def f : {ty}");
        let body = "| true => 0\n  | false => false";
        let src = format!("{signature}\n  {body}\n");
        let parsed = parse(src.as_bytes());
        let d = &parsed.decls[0];
        assert_eq!(
            &src[d.signature.start as usize..d.signature.end as usize],
            signature
        );
        let span = d.body.unwrap();
        assert_eq!(&src[span.start as usize..span.end as usize], body);
    }
}

#[test]
fn absolute_values_in_match_scrutinees_stay_in_the_signature() {
    for ty in [
        "match |x| with\n    | 0 => Nat\n    | _ => Nat",
        "match\n      match |x| with\n      | n => n\n    with\n    | 0 => Nat\n    | _ => Nat",
        "match (fun | 0 => 0 | n + 1 => n) |x| with\n    | 0 => Nat\n    | _ => Nat",
    ] {
        assert_signature_body(&format!("def sample (x : Int) :\n    {ty}"), ty, ":= sorry");
    }
    let signature = "def sample : ∀ x : Int,\n    match |x| with\n    | 0 => Nat\n    | _ => Nat";
    let body = "| _ => 0";
    let src = format!("{signature}\n  {body}\n");
    let parsed = parse(src.as_bytes());
    let declaration = &parsed.decls[0];
    assert_eq!(&src[..declaration.signature.end as usize], signature);
    assert_eq!(
        &src[declaration.body.unwrap().start as usize..].trim(),
        &body
    );
}
