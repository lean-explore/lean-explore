//! The declarations a statement refers to, found without elaborating it.
//!
//! Lean stores, as the constants of a declaration, the ones that appear in its elaborated
//! type. Without an elaborated type, this module reads the identifiers written in the
//! signature, leaves out the ones that binders make local, and resolves the rest the way
//! Lean resolves a name.
//!
//! The result is an approximation. References made through notation (`ℝ`, `+`, `∈`), through
//! field access on a term (`s.card`) and through elaboration (instances, coercions) are not
//! found. Where Lean would choose between several candidates by their types, no reference is
//! recorded.

use super::scopes::{Scope, Variable};
use super::tokens::{
    Group, Item, Token, binder_names, colon_index, group_binder_names, instance_key,
};
use crate::scan::DeclKind;
use std::collections::{BTreeSet, HashMap, HashSet};

const BINDER_SYMBOLS: [&str; 13] = [
    "∀", "∃", "λ", "Σ", "Π", "∑", "∏", "⋃", "⋂", "⨆", "⨅", "∫", "∮",
];
const BINDER_WORDS: [&str; 3] = ["fun", "forall", "exists"];
const BINDER_ENDS: [&str; 3] = [",", "=>", "↦"];
const PI_ARROWS: [&str; 3] = ["→", "->", "×"];
const SET_BUILDER_MARKS: [&str; 2] = ["|", "//"];
const SET_BUILDER_STARTS: [&str; 4] = [":", "∈", "|", "//"];
const HEAD_ENDS: [&str; 2] = ["extends", "where"];
const LOCAL_DEFINITIONS: [&str; 2] = ["let", "have"];
const BINDER_STOP_WORDS: [&str; 1] = ["in"];
const UNIVERSE_WORDS: [&str; 2] = ["Type", "Sort"];
const LEVEL_ENDS: [&str; 6] = ["→", "->", ",", ":=", "=", "×"];
const BINDER_MARKS: [&str; 4] = ["'", "!", "ᶠ", "ˢ"];
const VALUE_STARTS: [&str; 2] = [":=", "←"];

/// Whether the signature of a declaration of this kind is syntax, not a type.
fn is_syntax(kind: DeclKind) -> bool {
    use DeclKind::*;
    matches!(
        kind,
        Macro | Elab | Syntax | Notation | Simproc | ConfigElab | UnifHint
    )
}

/// Whether a declaration of this kind takes no instance variables of its section.
fn is_type(kind: DeclKind) -> bool {
    use DeclKind::*;
    matches!(kind, Structure | Class | Inductive | ClassInductive)
}

/// Whether a declaration includes the section variables that only its body uses. A theorem
/// includes those of its signature only.
pub fn reads_body(kind: DeclKind, scope: &Scope<'_>) -> bool {
    let has_names = scope.variables.iter().any(|v| !v.names.is_empty());
    has_names && !matches!(kind, DeclKind::Theorem | DeclKind::Lemma)
}

/// The index of the first of the symbols from `start`.
fn find(items: &[Item<'_>], start: usize, symbols: &[&str]) -> Option<usize> {
    (start..items.len()).find(|index| items[*index].is_symbol(symbols))
}

/// Whether an item after `∀` or `fun` is still a binder.
fn is_binder_name(item: &Item<'_>) -> bool {
    match item {
        Item::Group(_) => true,
        Item::Token(_) => item.plain().is_some() && !item.is_word(&BINDER_STOP_WORDS),
    }
}

/// Whether an identifier names an argument or field, as `(x := v)`.
fn is_label(items: &[Item<'_>], index: usize) -> bool {
    let assigned = items.get(index + 1).is_some_and(|i| i.is_symbol(&[":="]));
    assigned && (index == 0 || items[index - 1].is_symbol(&[","]))
}

/// Collects the references of one signature, tracking local names.
struct Walker<'w, 't> {
    resolve: &'w dyn Fn(&str) -> Option<&'t str>,
    /// Names of the section variables in scope.
    variables: HashSet<&'w str>,
    /// Local names in scope, innermost last.
    bound: Vec<&'w str>,
    references: Vec<&'t str>,
    used_variables: BTreeSet<&'w str>,
}

impl<'w> Walker<'w, '_> {
    /// Reads items left to right; binders bind to the end of the items. `labels` tells
    /// whether the items are the whole content of a bracket, where `x := v` names an
    /// argument or field.
    fn walk(&mut self, items: &'w [Item<'w>], labels: bool) {
        let depth = self.bound.len();
        self.walk_open(items, labels);
        self.bound.truncate(depth);
    }

    /// Like [`Walker::walk`], but leaves the names that the items bind in scope.
    fn walk_open(&mut self, items: &'w [Item<'w>], labels: bool) {
        let mut index = 0;
        while index < items.len() {
            let item = &items[index];
            let following = items.get(index + 1);
            if item.is_symbol(&[":="]) && following.is_some_and(|f| f.is_word(&["by"])) {
                // The tactic of an automatic argument is not part of the type.
                return;
            }
            let local = following.and_then(Item::plain);
            match item {
                Item::Group(group) => self.group(group, following),
                Item::Token(_)
                    if item.is_symbol(&BINDER_SYMBOLS) || item.is_word(&BINDER_WORDS) =>
                {
                    index = self.binder(items, index);
                }
                Item::Token(_) if item.is_word(&LOCAL_DEFINITIONS) && local.is_some() => {
                    self.bound.extend(local);
                    index += 1;
                }
                Item::Token(_) if item.is_word(&UNIVERSE_WORDS) => {
                    // What follows `Type` or `Sort` is a universe level.
                    if following.is_some_and(|f| !f.is_symbol(&LEVEL_ENDS)) {
                        index += 1;
                    }
                }
                Item::Token(token) => {
                    if token.is_identifier && !(labels && is_label(items, index)) {
                        self.identifier(token);
                    }
                }
            }
            index += 1;
        }
    }

    /// Records the declaration an identifier refers to, if it is global.
    fn identifier(&mut self, token: &'w Token<'w>) {
        let first = token.text.split('.').next().unwrap_or("");
        if token.after_dot || self.bound.contains(&first) {
            return;
        }
        if self.variables.contains(first) {
            self.used_variables.insert(first);
            return;
        }
        if let Some(name) = (self.resolve)(&token.text)
            && !self.references.contains(&name)
        {
            self.references.push(name);
        }
    }

    /// Reads a bracket group; the names of a dependent-arrow binder stay bound after it.
    fn group(&mut self, group: &'w Group<'w>, following: Option<&Item<'_>>) {
        if group.opening == ".{" {
            return;
        }
        if group.opening == "{" && find(&group.items, 0, &SET_BUILDER_MARKS).is_some() {
            let end = find(&group.items, 0, &SET_BUILDER_STARTS).unwrap_or(0);
            let depth = self.bound.len();
            binder_names(&group.items[..end], &mut self.bound);
            self.walk(&group.items[end..], false);
            self.bound.truncate(depth);
            return;
        }
        let colon = colon_index(&group.items);
        match colon {
            Some(colon) if following.is_some_and(|f| f.is_symbol(&PI_ARROWS)) => {
                self.walk(&group.items[colon + 1..], false);
                group_binder_names(group, &mut self.bound);
            }
            _ => self.walk(&group.items, true),
        }
    }

    /// Reads the binders after `∀`, `fun` and the like, and returns the index of their last
    /// item. A quantifier with no body binds nothing.
    fn binder(&mut self, items: &'w [Item<'w>], index: usize) -> usize {
        let Some(end) = find(items, index + 1, &BINDER_ENDS) else {
            return index;
        };
        let mut binders = &items[index + 1..end];
        while binders.first().is_some_and(|b| b.is_symbol(&BINDER_MARKS)) {
            binders = &binders[1..];
        }
        let count = binders.iter().take_while(|b| is_binder_name(b)).count();
        let names: Vec<&Item> = binders[..count].iter().collect();
        self.binders(&names, &binders[count..]);
        end
    }

    /// Reads binders and their types, and leaves the names they bind in scope.
    fn binders(&mut self, binders: &[&'w Item<'w>], annotation: &'w [Item<'w>]) {
        for &binder in binders {
            if let Item::Group(group) = binder {
                match colon_index(&group.items) {
                    Some(colon) => self.walk(&group.items[colon + 1..], false),
                    None if group.opening == "[" => self.walk(&group.items, false),
                    None => {}
                }
                group_binder_names(group, &mut self.bound);
            }
        }
        self.walk(annotation, false);
        for &binder in binders {
            binder_names(std::slice::from_ref(binder), &mut self.bound);
        }
    }
}

/// Where a signature's name and binders end and its type begins.
fn head_end(items: &[Item<'_>]) -> usize {
    items
        .iter()
        .position(|item| item.is_symbol(&[":", ":="]) || item.is_word(&HEAD_ENDS))
        .unwrap_or(items.len())
}

/// The binders of a signature's head.
///
/// Bracketed binders always count. Bare names count only after the declaration's own name,
/// so keywords and the name itself are not bound.
fn head_binders<'w>(items: &'w [Item<'w>], name: &str) -> Vec<&'w Item<'w>> {
    let head = &items[..head_end(items)];
    let is_group = |item: &&Item| matches!(item, Item::Group(_));
    let is_name = |item: &Item| {
        let written = item.token().filter(|token| token.is_identifier);
        written.is_some_and(|token| {
            let rest = name.strip_suffix(&*token.text);
            !name.is_empty() && rest.is_some_and(|rest| rest.is_empty() || rest.ends_with('.'))
        })
    };
    match head.iter().position(is_name) {
        Some(index) => {
            let groups = head[..index].iter().filter(is_group);
            groups.chain(&head[index + 1..]).collect()
        }
        None => head.iter().filter(is_group).collect(),
    }
}

/// The names bound by the binders of a signature, given as its items, of the declaration
/// `name`.
pub fn signature_binder_names(signature: &[Item<'_>], name: &str) -> Vec<String> {
    let mut names = Vec::new();
    for binder in head_binders(signature, name) {
        binder_names(std::slice::from_ref(binder), &mut names);
    }
    names.into_iter().map(str::to_string).collect()
}

/// The variables that a variable's type mentions.
fn mentioned<'w>(variable: &'w Variable<'w>, names: &HashMap<&str, usize>) -> Vec<&'w str> {
    let mut found = Vec::new();
    super::tokens::plain_names(&variable.type_items, &mut found);
    found.retain(|name| names.contains_key(name));
    found
}

/// Adds the references of the section variables a declaration includes.
///
/// A named variable is included when the declaration uses it, directly or through the type
/// of another included variable. An instance variable is included when every variable its
/// type mentions is included, unless `omit` leaves it out; a type declaration takes none.
fn include_variables<'w>(
    walker: &mut Walker<'w, '_>,
    scope: &'w Scope<'w>,
    mut used: BTreeSet<&'w str>,
    instances: bool,
) {
    let variables = &scope.variables;
    let mut by_name = HashMap::new();
    for (index, variable) in variables.iter().enumerate() {
        by_name.extend(variable.names.iter().map(|name| (name.as_str(), index)));
    }
    let mut walked = vec![false; variables.len()];
    let mut waiting: Vec<&str> = used.iter().copied().collect();
    while let Some(name) = waiting.pop() {
        let Some(&index) = by_name.get(name) else {
            continue;
        };
        if std::mem::replace(&mut walked[index], true) {
            continue;
        }
        walker.bound.clear();
        walker.walk(&variables[index].type_items, false);
        waiting.extend(walker.used_variables.difference(&used));
        used.extend(&walker.used_variables);
    }
    for (index, variable) in variables.iter().enumerate() {
        if !instances || !variable.is_instance || walked[index] {
            continue;
        }
        let is_omitted = scope.omitted.contains(&instance_key(&variable.type_items));
        let is_complete = mentioned(variable, &by_name)
            .iter()
            .all(|name| used.contains(name));
        if !is_omitted && is_complete {
            walker.bound.clear();
            walker.walk(&variable.type_items, false);
        }
    }
}

/// One declaration whose references are wanted.
pub struct Statement<'w> {
    /// The declaration's kind.
    pub kind: DeclKind,
    /// Fully qualified name of the declaration.
    pub name: &'w str,
    /// The declaration's signature as written.
    pub signature: &'w [Item<'w>],
    /// Further local names, such as the binders of the structure a field belongs to.
    pub bound: &'w [String],
    /// The scope in effect at the declaration.
    pub scope: &'w Scope<'w>,
    /// The whole declaration as written, when [`reads_body`] says it is read.
    pub body: &'w [Token<'w>],
}

/// Lists the names that a statement refers to, in order of first appearance.
///
/// `resolve` turns an identifier written in the statement into a declaration name.
pub fn find_references<'t>(
    statement: &Statement<'_>,
    resolve: &dyn Fn(&str) -> Option<&'t str>,
) -> Vec<&'t str> {
    let (kind, scope) = (statement.kind, statement.scope);
    if is_syntax(kind) {
        return Vec::new();
    }
    // A deriving clause or a value on the same line is not the type.
    let has_value = matches!(kind, DeclKind::Initialize | DeclKind::RegisterOption);
    let is_end =
        |item: &Item| item.is_word(&["deriving"]) || (has_value && item.is_symbol(&VALUE_STARTS));
    let end = statement.signature.iter().position(is_end);
    let items = &statement.signature[..end.unwrap_or(statement.signature.len())];
    let names = scope.variables.iter().flat_map(|v| v.names.iter());
    let mut walker = Walker {
        resolve,
        variables: names.map(String::as_str).collect(),
        bound: statement.bound.iter().map(String::as_str).collect(),
        references: Vec::new(),
        used_variables: BTreeSet::new(),
    };
    let head = match kind {
        DeclKind::ParentProjection => 0,
        _ => head_end(items),
    };
    if head > 0 {
        walker.binders(&head_binders(items, statement.name), &[]);
    }
    walker.walk_open(&items[head..], false);
    let mut used = walker.used_variables.clone();
    for name in &scope.included {
        used.extend(walker.variables.get(name.as_str()));
    }
    for token in statement.body {
        let first = token.text.split('.').next().unwrap_or("");
        if token.is_identifier && !token.after_dot {
            used.extend(walker.variables.get(first));
        }
    }
    if !scope.variables.is_empty() {
        for name in &scope.omitted {
            used.remove(name.as_str());
        }
        include_variables(&mut walker, scope, used, !is_type(kind));
    }
    walker.references
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::refs::names::{NameTable, Use, enclosing_namespaces};
    use crate::refs::scopes::{parse_open, parse_variables};
    use crate::refs::tokens::{nest, tokenize};
    use std::sync::Arc;

    const NAMES: [&str; 12] = [
        "Nat",
        "Set",
        "Good",
        "Flag",
        "Box",
        "X",
        "x",
        "b",
        "u",
        "helper",
        "Pkg.addOne",
        "Pkg.Point",
    ];

    /// A statement to resolve among the test names.
    struct Case<'a> {
        signature: &'a str,
        name: &'a str,
        kind: DeclKind,
        scope: Scope<'a>,
        bound: Vec<String>,
        body: &'a str,
    }

    fn case(signature: &str) -> Case<'_> {
        Case {
            signature,
            name: "t",
            kind: DeclKind::Def,
            scope: Scope::default(),
            bound: Vec::new(),
            body: "",
        }
    }

    impl<'a> Case<'a> {
        fn named(mut self, name: &'a str) -> Self {
            self.name = name;
            self
        }

        fn kind(mut self, kind: DeclKind) -> Self {
            self.kind = kind;
            self
        }

        fn scope(mut self, scope: Scope<'a>) -> Self {
            self.scope = scope;
            self
        }

        fn body(mut self, body: &'a str) -> Self {
            self.body = body;
            self
        }

        fn references(&self) -> Vec<&'static str> {
            let mut table = NameTable::default();
            for name in NAMES {
                table.declare(name);
            }
            let opened = table.open_namespaces(&self.scope.opened);
            let namespaces = enclosing_namespaces(self.name);
            let place = Use {
                opened: &opened,
                namespaces: &namespaces,
                ..Default::default()
            };
            let signature = nest(tokenize(self.signature.as_bytes()));
            let body = if reads_body(self.kind, &self.scope) {
                tokenize(self.body.as_bytes())
            } else {
                Vec::new()
            };
            let statement = Statement {
                kind: self.kind,
                name: self.name,
                signature: &signature,
                bound: &self.bound,
                scope: &self.scope,
                body: &body,
            };
            let mut found =
                find_references(&statement, &|identifier| table.resolve(identifier, &place));
            found.retain(|name| *name != self.name);
            found
        }
    }

    fn references(signature: &str) -> Vec<&'static str> {
        case(signature).references()
    }

    fn arguments(command: &str) -> Vec<Item<'_>> {
        let mut items = nest(tokenize(command.as_bytes()));
        items.remove(0);
        items
    }

    /// A scope built from one `variable` command.
    fn variables(command: &str) -> Scope<'_> {
        Scope {
            variables: parse_variables(arguments(command))
                .into_iter()
                .map(Arc::new)
                .collect(),
            ..Default::default()
        }
    }

    const NONE: [&str; 0] = [];

    #[test]
    fn references_keep_first_appearance_order() {
        let signature = "theorem Pkg.t (n : Nat) (p : Point) : addOne n = addOne (addOne n)";
        assert_eq!(
            case(signature).named("Pkg.t").references(),
            ["Nat", "Pkg.Point", "Pkg.addOne"]
        );
    }

    #[test]
    fn no_references_is_empty() {
        assert_eq!(references("theorem t : True"), NONE);
        assert_eq!(references("def t"), NONE);
    }

    #[test]
    fn the_declaration_itself_is_resolved_like_any_name() {
        let statement = case("theorem x : x = x").named("x");
        assert_eq!(statement.references(), NONE);
    }

    #[test]
    fn head_binders_are_local() {
        assert_eq!(references("theorem t (x : Nat) {b} : x = b"), ["Nat"]);
        assert_eq!(references("def t x b : Nat"), ["Nat"]);
        assert_eq!(
            references("instance (priority := 100) t : Good x"),
            ["Good", "x"]
        );
    }

    #[test]
    fn words_before_the_name_are_not_binders() {
        assert_eq!(references("theorem t : theorem"), NONE);
        let binders = |signature: &str, name: &str| {
            signature_binder_names(&nest(tokenize(signature.as_bytes())), name)
        };
        assert_eq!(
            binders("structure Box (X : Nat) extends Good X", "Box"),
            ["X"]
        );
        assert!(binders("instance : Good x", "inst").is_empty());
    }

    #[test]
    fn ascription_in_the_type_is_not_a_binder() {
        assert_eq!(references("theorem t : (x : Nat) = x"), ["x", "Nat"]);
    }

    #[test]
    fn quantifier_binders_are_scoped() {
        assert_eq!(
            references("theorem t : (∀ x : Nat, x = x) ∧ x = x"),
            ["Nat", "x"]
        );
        assert_eq!(
            references("theorem t : ∀ (a : Nat) (b : Nat), a = b"),
            ["Nat"]
        );
        assert_eq!(references("theorem t : ∃ x b, x = b"), NONE);
        assert_eq!(references("theorem t : ∀ x ∈ b, x = x"), ["b"]);
    }

    #[test]
    fn binder_marks_and_stop_words() {
        assert_eq!(references("def t : Σ' X : Nat, X = X"), ["Nat"]);
        assert_eq!(references("theorem t : ∃! x, x = b"), ["b"]);
        assert_eq!(references("theorem t : ∫ x in b, x = 0"), ["b"]);
    }

    #[test]
    fn a_binder_without_an_end_binds_nothing() {
        assert_eq!(references("theorem t : ∀ x"), ["x"]);
    }

    #[test]
    fn function_and_pi_binders() {
        assert_eq!(references("def t : (fun x => x) = fun b ↦ b"), NONE);
        assert_eq!(references("def t : (X : Nat) → X = X"), ["Nat"]);
        assert_eq!(references("def t : (X : Nat) = X"), ["X", "Nat"]);
    }

    #[test]
    fn set_builder_binds_inside_the_braces() {
        assert_eq!(
            references("def t : Set Nat → {x | x = b}"),
            ["Set", "Nat", "b"]
        );
        assert_eq!(
            references("def t : {x : Nat // x = b} → x"),
            ["Nat", "b", "x"]
        );
    }

    #[test]
    fn local_definitions_bind_the_rest() {
        assert_eq!(references("def t : let x := b; x = x"), ["b"]);
    }

    #[test]
    fn named_arguments_and_field_access_are_not_references() {
        assert_eq!(
            references("theorem t : helper (x := b) = (b).x"),
            ["helper", "b"]
        );
    }

    #[test]
    fn automatic_argument_tactics_are_not_read() {
        assert_eq!(
            references("def t (h : Good x := by exact helper b) : Nat"),
            ["Good", "x", "Nat"]
        );
    }

    #[test]
    fn default_values_are_part_of_the_type() {
        assert_eq!(
            references("theorem t (n : Nat := b) : Good n"),
            ["Nat", "b", "Good"]
        );
    }

    #[test]
    fn syntax_declarations_and_values_are_not_types() {
        let of_kind = |signature, kind| case(signature).kind(kind).references();
        assert_eq!(
            of_kind("macro \"m\" : term => helper", DeclKind::Macro),
            NONE
        );
        assert_eq!(
            of_kind("initialize t : Box ← helper", DeclKind::Initialize),
            ["Box"]
        );
        let option = "register_option t : Nat := { defValue := b }";
        assert_eq!(of_kind(option, DeclKind::RegisterOption), ["Nat"]);
    }

    #[test]
    fn universe_levels_are_not_constants() {
        assert_eq!(
            references("def t (α : Type u) (β : Sort (max u b)) : Nat.{u}"),
            ["Nat"]
        );
        assert_eq!(references("def t : Type → u"), ["u"]);
    }

    #[test]
    fn deriving_and_extends() {
        let constructor = case("| k : Box deriving Good")
            .named("Box.k")
            .kind(DeclKind::Constructor);
        assert_eq!(constructor.references(), ["Box"]);
        assert_eq!(
            references("structure t (x : Nat) extends Good x"),
            ["Nat", "Good"]
        );
    }

    #[test]
    fn parent_projection_is_all_type() {
        let projection = case("Good b")
            .named("t.toGood")
            .kind(DeclKind::ParentProjection);
        assert_eq!(projection.references(), ["Good", "b"]);
    }

    #[test]
    fn further_bound_names() {
        let mut field = case("val : X").named("S.val");
        assert_eq!(field.references(), ["X"]);
        field.bound = vec!["X".to_string()];
        assert_eq!(field.references(), NONE);
    }

    #[test]
    fn opened_names_resolve() {
        let opened = parse_open(&arguments("open Pkg"), "");
        let scope = Scope {
            opened: opened.into_iter().map(Arc::new).collect(),
            ..Default::default()
        };
        let statement = case("theorem t : addOne = addOne").scope(scope);
        assert_eq!(statement.references(), ["Pkg.addOne"]);
    }

    #[test]
    fn used_variable_brings_its_type() {
        let scoped =
            |signature| case(signature).scope(variables("variable (v : Box) (w : Good v)"));
        assert_eq!(scoped("theorem t : v = v").references(), ["Box"]);
        assert_eq!(scoped("theorem t : w = w").references(), ["Good", "Box"]);
        assert_eq!(scoped("theorem t : True").references(), NONE);
    }

    #[test]
    fn local_binder_shadows_a_variable() {
        let statement = case("theorem t (v : Nat) : v = v").scope(variables("variable (v : Box)"));
        assert_eq!(statement.references(), ["Nat"]);
    }

    #[test]
    fn instance_variables_follow_their_variables() {
        let scoped =
            |signature| case(signature).scope(variables("variable {α : Type} [Good α] [Flag]"));
        assert_eq!(
            scoped("theorem t (a : α) : a = a").references(),
            ["Good", "Flag"]
        );
        assert_eq!(scoped("theorem t : True").references(), ["Flag"]);
        let structure = scoped("structure t").kind(DeclKind::Structure);
        assert_eq!(structure.references(), NONE);
    }

    #[test]
    fn omitted_and_included_variables() {
        let mut omitted = variables("variable {α : Type} [Good α]");
        omitted.omitted = vec!["[Good α]".to_string()];
        let statement = case("theorem t (a : α) : a = a").scope(omitted);
        assert_eq!(statement.references(), NONE);
        let mut included = variables("variable (v : Box)");
        included.included = vec!["v".to_string()];
        assert_eq!(
            case("theorem t : True").scope(included).references(),
            ["Box"]
        );
    }

    #[test]
    fn definition_bodies_include_their_variables() {
        let with_body = |signature, kind| {
            case(signature)
                .kind(kind)
                .scope(variables("variable (v : Box)"))
                .body("def t : Nat := v.val")
        };
        assert_eq!(
            with_body("def t : Nat", DeclKind::Def).references(),
            ["Nat", "Box"]
        );
        assert_eq!(
            with_body("theorem t : Nat", DeclKind::Theorem).references(),
            ["Nat"]
        );
    }
}
