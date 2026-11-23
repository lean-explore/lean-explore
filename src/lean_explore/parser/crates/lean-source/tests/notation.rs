//! References made through notation and through fields of typed local names.

use lean_source::refs::{Project, SourceFile};
use lean_source::{Parsed, parse};
use std::collections::BTreeMap;

/// What a small project's declarations refer to, keyed by declaration name: the names and,
/// of them, the ones written in the type after the signature's colon.
struct Found {
    names: BTreeMap<String, Vec<String>>,
    in_type: BTreeMap<String, Vec<String>>,
}

impl Found {
    fn of(&self, name: &str) -> Vec<&str> {
        self.names[name].iter().map(String::as_str).collect()
    }
}

fn sources<'a>(files: &'a [(&'a str, &'a str)], parsed: &'a [Parsed]) -> Vec<SourceFile<'a>> {
    files
        .iter()
        .zip(parsed)
        .map(|((module, source), parsed)| SourceFile {
            module,
            source: source.as_bytes(),
            parsed,
        })
        .collect()
}

/// Resolves a package given as `(module, source)` pairs with context files given the same
/// way and names known from elsewhere.
fn resolve(files: &[(&str, &str)], context: &[(&str, &str)], known: &[&str]) -> Found {
    let parse_all = |files: &[(&str, &str)]| -> Vec<Parsed> {
        files.iter().map(|(_, s)| parse(s.as_bytes())).collect()
    };
    let (parsed, context_parsed) = (parse_all(files), parse_all(context));
    let project = Project::with_context(
        &sources(files, &parsed),
        &sources(context, &context_parsed),
        known,
        false,
    );
    let mut found = Found {
        names: BTreeMap::new(),
        in_type: BTreeMap::new(),
    };
    for (parsed, references) in parsed.iter().zip(project.detailed_references()) {
        for (declaration, references) in parsed.decls.iter().zip(references) {
            let Some(name) = &declaration.name else {
                continue;
            };
            let written = references.names.iter().zip(&references.in_type);
            let in_type = written.filter(|(_, in_type)| **in_type);
            let in_type = in_type.map(|(name, _)| name.to_string()).collect();
            let names = references.names.iter().map(|n| n.to_string()).collect();
            found.names.entry(name.clone()).or_insert(names);
            found.in_type.entry(name.clone()).or_insert(in_type);
        }
    }
    found
}

fn package(source: &str) -> Found {
    resolve(&[("M", source)], &[], &[])
}

const NONE: [&str; 0] = [];

/// A stand-in for the files of Lean that declare the core notations.
const CORE: [(&str, &str); 2] = [
    ("toolchain.src.lean.Init", "prelude\nimport Init.Notation\n"),
    (
        "toolchain.src.lean.Init.Notation",
        "prelude\n\
         class HAdd (α β γ) where\n  hAdd : α → β → γ\n\
         class HSub (α β γ) where\n  hSub : α → β → γ\n\
         class Neg (α) where\n  neg : α → α\n\
         class Membership (α γ) where\n  mem : γ → α → Prop\n\
         inductive Eq : α → α → Prop\n\
         def Not (a : Prop) : Prop := a → False\n\
         infixl:65 \" + \" => HAdd.hAdd\n\
         infixl:65 \" - \" => HSub.hSub\n\
         macro_rules | `($x + $y) => `(binop% HAdd.hAdd $x $y)\n\
         prefix:max \"-\" => Neg.neg\n\
         infix:50 \" = \" => Eq\n\
         notation:max \"¬\" p:40 => Not p\n\
         notation:50 a:50 \" ∈ \" b:50 => Membership.mem b a\n\
         notation:50 a:50 \" ∉ \" b:50 => ¬ (a ∈ b)\n",
    ),
];

#[test]
fn core_notations_come_from_context_files_through_init() {
    let source = "def T : Type := sorry\n\
                  theorem add (a b : T) : a + b = b + a := sorry\n\
                  theorem sub (a b : T) : a - -b = a - 2 - b := sorry\n\
                  theorem mem (a : T) (s : T) : a ∉ s := sorry\n";
    let found = resolve(&[("M", source)], &CORE, &[]);
    assert_eq!(found.of("add"), ["T", "HAdd.hAdd", "Eq"]);
    assert_eq!(found.of("sub"), ["T", "HSub.hSub", "Neg.neg", "Eq"]);
    assert_eq!(found.of("mem"), ["T", "Not", "Membership.mem"]);
    assert_eq!(found.in_type["add"], ["HAdd.hAdd", "Eq"]);
    // Without the files nothing is known about the tokens.
    assert_eq!(package(source).of("add"), ["T"]);
}

#[test]
fn a_file_that_starts_with_prelude_does_not_import_init() {
    let source = "prelude\ndef T : Type := sorry\ntheorem add (a b : T) : a + b = b + a := sorry\n";
    assert_eq!(resolve(&[("M", source)], &CORE, &[]).of("add"), ["T"]);
}

#[test]
fn context_files_are_seen_through_imports_only() {
    let context = [(
        "packages.lib.Lib.Ops",
        "def Lib.star (a : Nat) : Nat := a\npostfix:max \"⋆\" => Lib.star\n",
    )];
    let uses = "theorem t (a : Nat) : a⋆ = Lib.star a := sorry\n";
    let imported = format!("import Lib.Ops\n{uses}");
    let found = resolve(&[("M", imported.as_str())], &context, &[]);
    assert_eq!(found.of("t"), ["Lib.star"]);
    assert_eq!(resolve(&[("M", uses)], &context, &[]).of("t"), NONE);
    // A known name is visible everywhere; the notation still is not.
    assert_eq!(
        resolve(&[("M", uses)], &context, &["Lib.star"]).of("t"),
        ["Lib.star"]
    );
}

#[test]
fn a_notation_of_another_package_file_needs_its_import() {
    let files = [
        (
            "Ops",
            "def star (a : Nat) : Nat := a\nprefix:max \"⋆\" => star\n",
        ),
        (
            "Uses",
            "import Ops\ntheorem t (a : Nat) : ⋆a = a := sorry\n",
        ),
        ("Apart", "theorem u (a : Nat) : ⋆a = a := sorry\n"),
    ];
    let found = resolve(&files, &[], &[]);
    assert_eq!(found.of("t"), ["star"]);
    assert_eq!(found.of("u"), NONE);
}

#[test]
fn a_notation_is_used_after_it_is_declared() {
    let source = "def star (a : Nat) : Nat := a\n\
                  theorem before (a : Nat) : a ⋆ a = a := sorry\n\
                  infixl:70 \" ⋆ \" => star\n\
                  theorem after (a : Nat) : a ⋆ a = a := sorry\n";
    let found = package(source);
    assert_eq!(found.of("before"), NONE);
    assert_eq!(found.of("after"), ["star"]);
}

#[test]
fn a_local_notation_ends_with_its_section() {
    let source = "def cap : Nat := 8\ndef bound : Nat := 100\n\
                  section\nlocal notation \"bound\" => cap\n\
                  theorem inside : bound = cap := sorry\nend\n\
                  theorem outside : bound = cap := sorry\n\
                  namespace N\nlocal notation \"𝔠\" => cap\ntheorem named : 𝔠 = cap := sorry\nend N\n\
                  theorem later : 𝔠 = cap := sorry\n";
    let found = package(source);
    assert_eq!(found.of("inside"), ["cap"]);
    assert_eq!(found.of("outside"), ["bound", "cap"]);
    assert_eq!(found.of("N.named"), ["cap"]);
    assert_eq!(found.of("later"), ["cap"]);
    // A local notation does not reach other files.
    let files = [
        ("Ops", "def cap : Nat := 8\nlocal notation \"𝔠\" => cap\n"),
        ("Uses", "import Ops\ntheorem t : 𝔠 = 𝔠 := sorry\n"),
    ];
    assert_eq!(resolve(&files, &[], &[]).of("t"), NONE);
}

#[test]
fn a_scoped_notation_needs_its_namespace() {
    let source = "def nhds (a : Nat) : Nat := a\ndef Real : Type := Nat\n\
                  namespace Topology\nscoped notation \"𝓝\" => nhds\n\
                  theorem inside (a : Nat) : 𝓝 a = a := sorry\n\
                  namespace Deeper\ntheorem deeper (a : Nat) : 𝓝 a = a := sorry\nend Deeper\n\
                  end Topology\n\
                  scoped[Reals] notation \"ℝ\" => Real\n\
                  theorem closed (a : ℝ) : 𝓝 a = a := sorry\n\
                  section\nopen scoped Topology Reals\n\
                  theorem scoped_open (a : ℝ) : 𝓝 a = a := sorry\nend\n\
                  section\nopen Topology\ntheorem opened (a : Nat) : 𝓝 a = a := sorry\nend\n\
                  section\nopen Topology (inside)\ntheorem selected (a : Nat) : 𝓝 a = a := sorry\nend\n\
                  open Topology in\ntheorem once (a : Nat) : 𝓝 a = a := sorry\n\
                  theorem after (a : Nat) : 𝓝 a = a := sorry\n";
    let found = package(source);
    assert_eq!(found.of("Topology.inside"), ["nhds"]);
    assert_eq!(found.of("Topology.Deeper.deeper"), ["nhds"]);
    assert_eq!(found.of("closed"), NONE);
    assert_eq!(found.of("scoped_open"), ["Real", "nhds"]);
    assert_eq!(found.of("opened"), ["nhds"]);
    assert_eq!(found.of("selected"), NONE);
    assert_eq!(found.of("once"), ["nhds"]);
    assert_eq!(found.of("after"), NONE);
}

#[test]
fn open_scoped_opens_no_names() {
    let source = "namespace A\ndef f : Nat := 1\nscoped notation \"𝔣\" => f\nend A\n\
                  open scoped A\ntheorem t : f = 𝔣 := sorry\n";
    assert_eq!(package(source).of("t"), ["A.f"]);
    let named = "namespace A\ndef f : Nat := 1\nend A\nopen scoped A\ntheorem t : f = f := sorry\n";
    assert_eq!(package(named).of("t"), NONE);
}

#[test]
fn several_notations_for_a_token_give_what_they_share() {
    let source = "def A.tensor (a b : Nat) : Nat := a\ndef B.tensor (a b : Nat) : Nat := a\n\
                  def wrap (a : Nat) : Nat := a\n\
                  scoped[A] infixl:70 \" ⊗ \" => A.tensor\n\
                  scoped[B] infixl:70 \" ⊗ \" => B.tensor\n\
                  scoped[B] notation:70 a \" ⊠ \" b => wrap (B.tensor a b)\n\
                  scoped[A] notation:70 a \" ⊠ \" b => wrap (A.tensor a b)\n\
                  section\nopen scoped A\ntheorem one (a : Nat) : a ⊗ a = a ⊠ a := sorry\nend\n\
                  open scoped A B\ntheorem both (a : Nat) : a ⊗ a = a ⊠ a := sorry\n";
    let found = package(source);
    assert_eq!(found.of("one"), ["A.tensor", "wrap"]);
    assert_eq!(found.of("both"), ["wrap"]);
}

#[test]
fn syntax_with_code_behind_it_stands_for_nothing_known() {
    let source = "def Subset (a b : Nat) : Prop := True\ndef le (a b : Nat) : Prop := True\n\
                  infix:50 \" ⊆ \" => Subset\n\
                  theorem plain (a : Nat) : a ⊆ a := sorry\n\
                  syntax:50 (priority := high) term:51 \" ⊆ \" term:51 : term\n\
                  theorem preferred (a : Nat) : a ⊆ a := sorry\n\
                  syntax:50 term:51 \" ≼ \" term:51 : term\n\
                  macro_rules | `($a ≼ $b) => `(le $a $b)\n\
                  theorem ruled (a : Nat) : a ≼ a := sorry\n";
    let found = package(source);
    assert_eq!(found.of("plain"), ["Subset"]);
    assert_eq!(found.of("preferred"), NONE);
    assert_eq!(found.of("ruled"), ["le"]);
}

#[test]
fn atoms_are_read_longest_first() {
    let source = "def Real : Type := Nat\ndef NNReal : Type := Nat\n\
                  def norm (a : Nat) : Nat := a\ndef nnnorm (a : Nat) : Nat := a\n\
                  def Maps (a b : Type) : Type := a\ndef Poly (a : Type) : Type := a\n\
                  def X : Nat := 0\ndef C : Nat := 0\n\
                  notation \"ℝ\" => Real\nnotation \"ℝ≥0\" => NNReal\n\
                  notation \"‖\" e \"‖\" => norm e\nnotation \"‖\" e \"‖₊\" => nnnorm e\n\
                  notation \"C(\" a \", \" b \")\" => Maps a b\n\
                  syntax:max term:max noWs \"[\" noWs &\"X\" noWs \"]\" : term\n\
                  macro_rules | `($R[X]) => `(Poly $R)\n\
                  theorem reals (a : ℝ) (b : ℝ≥0) : a = b := sorry\n\
                  theorem norms (a : Nat) : ‖a‖₊ = ‖a‖ := sorry\n\
                  theorem maps (f : C(ℝ, ℝ≥0)) (p : ℝ[X]) : C = X := sorry\n";
    let found = package(source);
    assert_eq!(found.of("reals"), ["Real", "NNReal"]);
    assert_eq!(found.of("norms"), ["nnnorm", "norm"]);
    assert_eq!(
        found.of("maps"),
        ["Maps", "Real", "NNReal", "Poly", "C", "X"]
    );
}

#[test]
fn a_binder_notation_binds_its_names() {
    let source = "def sum (f : Nat → Nat) : Nat := 0\ndef x : Nat := 0\ndef mem (a s : Nat) : Prop := True\n\
                  infix:50 \" ∈ \" => mem\n\
                  notation3 \"∑ \"(...)\", \"r:67:(scoped f => sum f) => r\n\
                  theorem bound (s : Nat) : ∑ x, x = x := sorry\n\
                  def total (s : Nat) (f : Nat → Nat) : Nat := 0\n\
                  syntax \"∏ \" term \" ∈ \" term \", \" term : term\n\
                  macro_rules | `(∏ $i ∈ $s, $f) => `(total $s (fun $i ↦ $f))\n\
                  theorem ruled (s : Nat) : ∏ i ∈ s, i = x := sorry\n";
    let found = package(source);
    // In `bound` the binder reaches to the end of the type, so both `x` are the bound
    // one; in `ruled` the `∈` belongs to the product and `x` is the declaration.
    assert_eq!(found.of("bound"), ["sum"]);
    assert_eq!(found.of("ruled"), ["total", "x"]);
}

const STRUCTURES: &str = "structure Point where\n  x : Nat\n  y : Nat\n\
                          structure Colored extends Point where\n  hue : Nat\n\
                          structure Segment where\n  first : Point\n  last : Colored\n\
                          def Point.norm (p : Point) : Nat := p.x\n\
                          def Point.shift (p : Point) (by_ : Nat) : Point := p\n\
                          def Point.flip (p : Point) : Point := p\n";

fn with_structures(source: &str) -> Found {
    package(&format!("{STRUCTURES}{source}"))
}

#[test]
fn a_field_of_a_typed_binder_is_its_declaration() {
    let found = with_structures(
        "theorem direct (p : Point) : p.x = p.norm := sorry\n\
         theorem chained (s : Segment) : s.first.x = s.last.hue := sorry\n\
         theorem methods (p : Point) : p.flip.flip.norm = (p.shift 1).y := sorry\n\
         theorem quantified : ∀ p : Point, ∃ q : Colored, p.x = q.hue := sorry\n",
    );
    assert_eq!(found.of("direct"), ["Point", "Point.x", "Point.norm"]);
    assert_eq!(
        found.of("chained"),
        [
            "Segment",
            "Segment.first",
            "Point.x",
            "Segment.last",
            "Colored.hue"
        ]
    );
    assert_eq!(
        found.of("methods"),
        [
            "Point",
            "Point.flip",
            "Point.norm",
            "Point.shift",
            "Point.y"
        ]
    );
    assert_eq!(
        found.of("quantified"),
        ["Point", "Colored", "Point.x", "Colored.hue"]
    );
    assert_eq!(found.in_type["direct"], ["Point.x", "Point.norm"]);
}

#[test]
fn an_inherited_field_is_found_in_the_parent() {
    let found = with_structures(
        "structure Deep extends Colored where\n  depth : Nat\n\
         theorem inherited (c : Colored) (d : Deep) : c.x = d.norm := sorry\n\
         theorem own (d : Deep) : d.depth = d.hue := sorry\n",
    );
    assert_eq!(
        found.of("inherited"),
        ["Colored", "Deep", "Point.x", "Point.norm"]
    );
    assert_eq!(found.of("own"), ["Deep", "Deep.depth", "Colored.hue"]);
}

#[test]
fn a_section_variable_can_be_the_receiver() {
    let found = with_structures(
        "section\nvariable (p : Point) {s : Segment}\n\
         theorem uses : p.x = s.last.y := sorry\nend\n",
    );
    assert_eq!(
        found.of("uses"),
        ["Point.x", "Segment.last", "Point.y", "Segment", "Point"]
    );
}

#[test]
fn the_receiver_type_can_be_written_with_notation() {
    let source = "structure Functor (C D : Type) where\n  obj : C → D\n\
                  structure Iso (X Y : Type) where\n  hom : X → Y\n\
                  infixr:26 \" ⥤ \" => Functor\ninfixr:10 \" ≅ \" => Iso\n\
                  def Functor.comp {C D E : Type} (F : C ⥤ D) (G : D ⥤ E) : C ⥤ E := sorry\n\
                  def forget (C D : Type) : C ⥤ D := sorry\n\
                  theorem direct (F : Nat ⥤ Nat) (e : Nat ≅ Nat) : F.obj 1 = e.hom 1 := sorry\n\
                  theorem composed (F : Nat ⥤ Nat) : (F.comp F).obj 1 = 1 := sorry\n\
                  theorem applied : (forget Nat Nat).obj 1 = ((forget Nat Nat).comp (forget Nat Nat)).obj 1 := sorry\n\
                  theorem ascribed (F) : (F : Nat ⥤ Nat).obj 1 = 1 := sorry\n";
    let found = package(source);
    assert_eq!(
        found.of("direct"),
        ["Functor", "Iso", "Functor.obj", "Iso.hom"]
    );
    assert_eq!(
        found.of("composed"),
        ["Functor", "Functor.comp", "Functor.obj"]
    );
    assert_eq!(
        found.of("applied"),
        ["forget", "Functor.obj", "Functor.comp"]
    );
    assert_eq!(found.of("ascribed"), ["Functor", "Functor.obj"]);
}

#[test]
fn a_function_has_the_fields_of_function() {
    let source = "def Function.comp (f g : Nat → Nat) : Nat → Nat := f\n\
                  def twice (n : Nat) : Nat := n\ndef plain := twice\n\
                  theorem global : twice.comp twice = twice := sorry\n\
                  theorem local_ (f : Nat → Nat) : f.comp f = f := sorry\n\
                  theorem untyped : plain.comp plain = plain := sorry\n";
    let found = package(source);
    assert_eq!(found.of("global"), ["twice", "Function.comp"]);
    assert_eq!(found.of("local_"), ["Function.comp"]);
    assert_eq!(found.of("untyped"), ["plain"]);
}

#[test]
fn a_constant_can_be_the_receiver() {
    let found = with_structures(
        "def origin : Point := sorry\ndef corner (n : Nat) : Colored := sorry\n\
         theorem constant : origin.x = origin.flip.y := sorry\n\
         theorem function : (corner 1).hue = (corner 2).x := sorry\n",
    );
    assert_eq!(
        found.of("constant"),
        ["origin", "Point.x", "Point.flip", "Point.y"]
    );
    assert_eq!(found.of("function"), ["corner", "Colored.hue", "Point.x"]);
}

#[test]
fn nothing_is_guessed_for_an_unknown_receiver() {
    let found = with_structures(
        "def x : Nat := 0\n\
         theorem untyped (p) : p.x = 0 := sorry\n\
         theorem generic {α : Type} (a : α) : a.x = 0 := sorry\n\
         theorem missing (p : Point) : p.z = p.x.succ := sorry\n\
         theorem pattern : ∀ ⟨p, q⟩ : Point × Point, p.x = q.y := sorry\n\
         theorem leading : (.x : Point → Nat) = Point.y := sorry\n\
         theorem term (p : Point) : (p, p).fst.x = 0 := sorry\n",
    );
    assert_eq!(found.of("untyped"), NONE);
    assert_eq!(found.of("generic"), NONE);
    assert_eq!(found.of("missing"), ["Point", "Point.x"]);
    assert_eq!(found.of("pattern"), ["Point"]);
    assert_eq!(found.of("leading"), ["Point", "Point.y"]);
    assert_eq!(found.of("term"), ["Point"]);
}

#[test]
fn a_hypothesis_has_the_fields_of_its_relation() {
    let source = "def Le (a b : Nat) : Prop := True\ninfix:50 \" ≼ \" => Le\n\
                  theorem Le.trans {a b c : Nat} (h : a ≼ b) (k : b ≼ c) : a ≼ c := sorry\n\
                  def Good (h : 1 ≼ 1) : Prop := True\n\
                  structure Box where\n  n : Nat\n\
                  def Box.Nonempty (b : Box) : Prop := True\n\
                  theorem Box.Nonempty.pos {b : Box} (h : b.Nonempty) : b.n ≼ b.n := sorry\n\
                  theorem uses (h : 1 ≼ 1) : Good (h.trans h) := sorry\n\
                  theorem nested (b : Box) (h : b.Nonempty) : Good h.pos := sorry\n";
    let found = package(source);
    assert_eq!(found.of("uses"), ["Le", "Good", "Le.trans"]);
    assert_eq!(
        found.of("nested"),
        ["Box", "Box.Nonempty", "Good", "Box.Nonempty.pos"]
    );
}

#[test]
fn tactic_blocks_and_antiquotations_are_not_read() {
    let source = "def helper : Nat := 0\ndef Good (n : Nat) (h : n = n) : Prop := True\n\
                  theorem t : Good 1 (by exact helper) := sorry\n";
    assert_eq!(package(source).of("t"), ["Good"]);
}
