//! The declarations a statement refers to, found without elaborating it.
//!
//! Lean stores, as the constants of a declaration, the ones that appear in its elaborated
//! type. Without an elaborated type, this module reads what is written in the signature:
//! the identifiers, leaving out the ones that binders make local, the tokens of notations
//! (`ℝ`, `+`, `∈`), and the fields accessed on a term whose type is written (`s.card` with
//! `s : Finset α`). What they stand for is asked of a [`Lookup`].
//!
//! The result is an approximation. References made through elaboration (instances,
//! coercions, implicit arguments) are not found, nor are the fields of a term whose type is
//! not written. Where Lean would choose between several candidates by their types, no
//! reference is recorded.

use super::notation::{Before, Fired};
use super::scopes::{Scope, Variable};
use super::tokens::{
    Group, Item, Token, binder_names, colon_index, group_binder_names, instance_key,
};
use crate::scan::DeclKind;
use std::collections::{BTreeSet, HashMap};

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
/// Brackets that hold a binder, as `(a b : T)`.
const BINDER_BRACKETS: [&str; 4] = ["(", "{", "[", "⦃"];
/// Words after which a term starts, so that `-` after one of them is a prefix.
const TERM_STARTS: [&str; 12] = [
    "fun", "then", "else", "if", "in", "do", "by", "from", "at", "with", "return", "show",
];
/// How many local names' types are followed to find the head of a type.
const MAX_TYPE_DEPTH: usize = 4;
/// Precedence of the function arrow.
const ARROW_PRECEDENCE: u32 = 25;

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

/// What a type is an application of.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Head<'t> {
    /// A constant, as `Set` in `Set α`.
    Constant(&'t str),
    /// A function type `α → β`, whose fields are looked up in `Function`.
    Function,
}

/// An identifier resolved to a declaration.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Resolved<'t, 'i> {
    /// The declaration's full name.
    pub name: &'t str,
    /// The components of the identifier after the name, which are fields: `baz` when
    /// `Foo.bar.baz` resolves to `Foo.bar`.
    pub fields: &'i str,
}

/// What the walker asks about the names and notations around a statement.
pub trait Lookup<'t> {
    /// The declaration an identifier written in the statement refers to.
    fn resolve<'i>(&self, identifier: &'i str) -> Option<Resolved<'t, 'i>>;

    /// The notation that an atom written in the statement stands for. `before` tells what
    /// comes just before the atom, and `rest` are the items after it.
    fn notation(&self, _atom: u32, _before: Before, _rest: &[Item<'_>]) -> Option<Fired<'_, 't>> {
        None
    }

    /// Whether a term can end with the atom, as it can with a postfix.
    fn is_closer(&self, _atom: u32) -> bool {
        false
    }

    /// The declaration that `.field` on a term with this type head refers to.
    fn field(&self, _head: Head<'t>, _field: &str) -> Option<&'t str> {
        None
    }

    /// The head of the type of a declaration as it is used.
    fn result_head(&self, _name: &'t str, _used: Used) -> Option<Head<'t>> {
        None
    }
}

/// How a declaration is used where the head of its type is wanted.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Used {
    /// By itself, as `f` in `f.comp`.
    Alone,
    /// As a field of a term, which is its first explicit argument: `x.f`.
    OnTerm,
    /// With all its arguments, as `g` in `(g a b).field`.
    Applied,
}

/// The type written for a local name.
struct LocalType<'w> {
    name: &'w str,
    /// Index of the name in the walker's bound names.
    index: usize,
    items: &'w [Item<'w>],
}

/// Collects the references of one signature, tracking local names.
pub struct Walker<'w, 't> {
    lookup: &'w dyn Lookup<'t>,
    /// The section variables in scope by name.
    variables: HashMap<&'w str, &'w Variable<'w>>,
    /// Local names in scope, innermost last.
    bound: Vec<&'w str>,
    /// The types written for local names.
    types: Vec<LocalType<'w>>,
    references: Vec<&'t str>,
    /// Per reference, whether it is written in the type after the signature's colon.
    in_type: Vec<bool>,
    reading_type: bool,
    /// Whether an identifier counts only if all of it is a declaration's name, as in the
    /// expansion of a notation, where nothing is known about fields.
    whole_names: bool,
    used_variables: BTreeSet<&'w str>,
}

/// Whether an item is a symbol that starts with a binder symbol, as `∑'` does.
fn is_binder_symbol(item: &Item<'_>) -> bool {
    let token = item.token().filter(|token| !token.is_identifier);
    token.is_some_and(|token| {
        BINDER_SYMBOLS
            .iter()
            .any(|symbol| token.text.starts_with(symbol))
    })
}

impl<'w, 't> Walker<'w, 't> {
    /// A walker over a statement written in `scope`, with `bound` as further local names.
    pub fn new(lookup: &'w dyn Lookup<'t>, scope: &'w Scope<'w>, bound: &'w [String]) -> Self {
        let mut variables = HashMap::new();
        for variable in &scope.variables {
            let names = variable.names.iter();
            variables.extend(names.map(|name| (name.as_str(), variable.as_ref())));
        }
        Walker {
            lookup,
            variables,
            bound: bound.iter().map(String::as_str).collect(),
            types: Vec::new(),
            references: Vec::new(),
            in_type: Vec::new(),
            reading_type: false,
            whole_names: false,
            used_variables: BTreeSet::new(),
        }
    }

    /// The references found so far.
    pub fn into_references(self) -> Vec<&'t str> {
        self.references
    }

    /// Reads items left to right; binders bind to the end of the items. `labels` tells
    /// whether the items are the whole content of a bracket, where `x := v` names an
    /// argument or field.
    pub fn walk(&mut self, items: &'w [Item<'w>], labels: bool) {
        let depth = self.bound.len();
        self.walk_open(items, labels);
        self.unbind(depth);
    }

    /// Forgets the local names bound after the first `depth` ones.
    fn unbind(&mut self, depth: usize) {
        self.bound.truncate(depth);
        while self.types.last().is_some_and(|local| local.index >= depth) {
            self.types.pop();
        }
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
            if item.is_symbol(&["$", "?"]) && following.is_some_and(|f| f.plain().is_some()) {
                // An antiquotation `$x` in the expansion of a macro is not a name, nor is
                // a hole `?m`.
                index += 2;
                continue;
            }
            if let Some(next) = self.notation(items, index) {
                index = next;
                continue;
            }
            let local = following.and_then(Item::plain);
            match item {
                Item::Group(group) => self.group(group, following),
                Item::Token(_) if is_binder_symbol(item) || item.is_word(&BINDER_WORDS) => {
                    index = self.binder(items, index, &[], false);
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
                Item::Token(token) if token.after_dot => {
                    // Fields of a parenthesised term; a leading `.name` needs the expected
                    // type.
                    // The dot is a token of its own between the term and the field.
                    if let Some(Item::Group(group)) = index.checked_sub(2).map(|at| &items[at])
                        && group.opening == "("
                    {
                        self.fields_of_term(group, &token.text);
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

    /// Whether a term ends with the item, so that an atom after it is not a prefix.
    fn ends_term(&self, item: &Item<'_>) -> bool {
        match item {
            Item::Group(_) => true,
            Item::Token(token) if token.is_identifier => !TERM_STARTS.contains(&&*token.text),
            Item::Token(token) => {
                token.text == "·" || (token.atom != 0 && self.lookup.is_closer(token.atom))
            }
        }
    }

    /// The notation the atom at `index` stands for, if any.
    fn fired(&self, items: &[Item<'_>], index: usize) -> Option<Fired<'w, 't>> {
        let token = items[index].token().filter(|token| token.atom != 0)?;
        let previous = index.checked_sub(1).map(|at| &items[at]);
        let is_binder = |item: &Item<'_>| match item {
            Item::Group(group) => {
                BINDER_BRACKETS.contains(&group.opening) && colon_index(&group.items).is_some()
            }
            Item::Token(_) => false,
        };
        let before = Before {
            term: token.after_literal || previous.is_some_and(|item| self.ends_term(item)),
            binder: !token.after_literal && previous.is_some_and(is_binder),
        };
        self.lookup
            .notation(token.atom, before, &items[index + 1..])
    }

    /// Records the constants of the notation written at `index`, if one is, and returns
    /// the index to go on from.
    fn notation(&mut self, items: &'w [Item<'w>], index: usize) -> Option<usize> {
        let fired = self.fired(items, index)?;
        let token = items[index].token()?;
        for constant in fired.constants.iter() {
            self.record(constant);
        }
        let last = index + token.covers as usize;
        // What a notation such as `Category*` does with the name it starts with is
        // unknown, but the name is still written.
        for item in &items[index + 1..(last + 1).min(items.len())] {
            if let Some(token) = item.token().filter(|t| fired.is_opaque && t.is_identifier) {
                self.identifier(token);
            }
        }
        if fired.binds || is_binder_symbol(&items[index]) {
            let hidden = fired.hides_binders;
            return Some(self.binder(items, last, fired.later_atoms, hidden) + 1);
        }
        Some(last + 1)
    }

    /// Adds a reference, or notes that a known one is written in the type as well.
    fn record(&mut self, name: &'t str) {
        match self.references.iter().position(|known| *known == name) {
            Some(index) => self.in_type[index] |= self.reading_type,
            None => {
                self.references.push(name);
                self.in_type.push(self.reading_type);
            }
        }
    }

    /// Records the declaration an identifier refers to, if it is global, and the fields
    /// that its further components access.
    fn identifier(&mut self, token: &'w Token<'w>) {
        let (first, fields) = match token.text.split_once('.') {
            Some((first, fields)) => (first, fields),
            None => (&*token.text, ""),
        };
        let is_bound = self.bound.contains(&first);
        let variable = self.variables.get_key_value(first).map(|(name, _)| *name);
        if !is_bound && let Some(name) = variable {
            self.used_variables.insert(name);
        }
        if is_bound || variable.is_some() {
            if !fields.is_empty() {
                let head = self.local_head(first, MAX_TYPE_DEPTH);
                self.fields(head, fields);
            }
            return;
        }
        if let Some(resolved) = self.lookup.resolve(&token.text)
            && !(self.whole_names && !resolved.fields.is_empty())
        {
            self.record(resolved.name);
            if !resolved.fields.is_empty() {
                let head = self.lookup.result_head(resolved.name, Used::Alone);
                self.fields(head, resolved.fields);
            }
        }
    }

    /// Records the fields written after a parenthesised term, as in `(g a).field`.
    fn fields_of_term(&mut self, group: &'w Group<'w>, fields: &str) {
        let head = self.term_head(group);
        self.fields(head, fields);
    }

    /// The head of the type of a parenthesised term: of its ascription `(e : T)`, or of
    /// what the function it applies returns.
    fn term_head(&self, group: &Group<'_>) -> Option<Head<'t>> {
        let items = group.items.as_slice();
        if let Some(colon) = colon_index(items) {
            return self.type_head(&items[colon + 1..]);
        }
        if let [Item::Group(inner)] = items
            && inner.opening == "("
        {
            return self.term_head(inner);
        }
        let first = items.iter().position(|item| !item.is_symbol(&["@"]))?;
        // The term can itself be fields of a parenthesised term: `(g a).field b`.
        let inner = match &items[first..] {
            [Item::Group(group), dot, Item::Token(token), ..]
                if group.opening == "(" && dot.is_symbol(&["."]) && token.after_dot =>
            {
                Some((self.term_head(group)?, token))
            }
            _ => None,
        };
        let (token, rest) = match inner {
            Some((_, token)) => (token, &items[first + 3..]),
            None => (items[first].token()?, &items[first + 1..]),
        };
        // An operator makes the term something else than an application.
        let is_operator = |item: &Item<'_>| item.token().is_some_and(|t| !t.is_identifier);
        let is_name = token.is_identifier && token.atom == 0 && token.after_dot == inner.is_some();
        if !is_name || rest.iter().any(is_operator) {
            return None;
        }
        // A literal is an argument that is not among the items.
        let is_alone = rest.is_empty() && !group.ends_with_literal;
        let (first, fields) = match token.text.split_once('.') {
            Some((first, fields)) => (first, fields),
            None => (&*token.text, ""),
        };
        let is_local = self.bound.contains(&first) || self.variables.contains_key(first);
        let (mut head, fields) = if let Some((head, token)) = inner {
            (head, &*token.text)
        } else if is_local {
            (self.local_head(first, MAX_TYPE_DEPTH)?, fields)
        } else {
            let resolved = self.lookup.resolve(&token.text)?;
            let used = match is_alone && resolved.fields.is_empty() {
                true => Used::Alone,
                false => Used::Applied,
            };
            (
                self.lookup.result_head(resolved.name, used)?,
                resolved.fields,
            )
        };
        if fields.is_empty() {
            // A local name that is applied has the type its own type returns.
            return (is_alone || !is_local).then_some(head);
        }
        let count = fields.split('.').count();
        for (index, field) in fields.split('.').enumerate() {
            let name = self.lookup.field(head, field)?;
            let used = match is_alone || index + 1 < count {
                true => Used::OnTerm,
                false => Used::Applied,
            };
            head = self.lookup.result_head(name, used)?;
        }
        Some(head)
    }

    /// Records the declarations that a chain of fields on a term with the given type head
    /// refers to, as far as the types are known.
    fn fields(&mut self, mut head: Option<Head<'t>>, fields: &str) {
        for field in fields.split('.') {
            let name = head.and_then(|head| self.lookup.field(head, field));
            let Some(name) = name else {
                return;
            };
            self.record(name);
            head = self.lookup.result_head(name, Used::OnTerm);
        }
    }

    /// What a written type is an application of: the constant at its head, found through
    /// notation if need be, or a function type. `None` if it is a local name or unknown.
    pub fn type_head(&self, items: &[Item<'_>]) -> Option<Head<'t>> {
        self.type_head_within(items, MAX_TYPE_DEPTH)
    }

    /// [`Walker::type_head`] following the types of at most `depth` local names.
    fn type_head_within(&self, items: &[Item<'_>], depth: usize) -> Option<Head<'t>> {
        let end = items.iter().position(|item| item.is_symbol(&[":="]));
        let items = &items[..end.unwrap_or(items.len())];
        if let [Item::Group(group)] = items
            && group.opening == "("
        {
            return self.type_head_within(&group.items, depth);
        }
        // The operator that binds loosest makes the type.
        let mut loosest: Option<(u32, Option<Head<'t>>)> = None;
        let mut is_unknown = false;
        let mut index = 0;
        while index < items.len() {
            let item = &items[index];
            let operator = if item.is_symbol(&["→", "->"]) {
                Some((Some(ARROW_PRECEDENCE), Some(Head::Function)))
            } else if is_binder_symbol(item) || item.is_word(&BINDER_WORDS) {
                // A quantifier takes the rest of the type.
                is_unknown |= index == 0;
                break;
            } else {
                let fired = self.fired(items, index).filter(|fired| !fired.leading);
                fired.map(|fired| (fired.precedence, fired.head.map(Head::Constant)))
            };
            match (operator, loosest) {
                (Some((None, _)), _) => is_unknown = true,
                (Some((Some(precedence), head)), Some((known, other))) => {
                    if precedence < known {
                        loosest = Some((precedence, head));
                    } else if precedence == known && head != other {
                        is_unknown = true;
                    }
                }
                (Some((Some(precedence), head)), None) => loosest = Some((precedence, head)),
                (None, _) => {}
            }
            index += 1;
        }
        if is_unknown {
            return None;
        }
        if let Some((_, head)) = loosest {
            return head;
        }
        let first = items.iter().position(|item| !item.is_symbol(&["@"]))?;
        if let Some(fired) = self.fired(items, first) {
            return fired.head.map(Head::Constant);
        }
        let token = items[first].token().filter(|token| token.is_identifier)?;
        if token.after_dot {
            return None;
        }
        let (first, fields) = match token.text.split_once('.') {
            Some((first, fields)) => (first, fields),
            None => (&*token.text, ""),
        };
        let is_local = self.bound.contains(&first) || self.variables.contains_key(first);
        let (mut head, fields) = if is_local {
            // A type such as `s.Nonempty` is headed by the field's declaration.
            let depth = depth.checked_sub(1)?;
            (self.local_head(first, depth)?, fields)
        } else {
            let resolved = self.lookup.resolve(&token.text)?;
            (Head::Constant(resolved.name), resolved.fields)
        };
        if fields.is_empty() {
            return (!is_local).then_some(head);
        }
        let count = fields.split('.').count();
        for (index, field) in fields.split('.').enumerate() {
            let name = self.lookup.field(head, field)?;
            head = match index + 1 == count {
                true => Head::Constant(name),
                false => self.lookup.result_head(name, Used::OnTerm)?,
            };
        }
        Some(head)
    }

    /// The head of the type written for a local name or section variable.
    fn local_head(&self, name: &str, depth: usize) -> Option<Head<'t>> {
        let Some(index) = self.bound.iter().rposition(|bound| *bound == name) else {
            let variable = self.variables.get(name)?;
            return self.type_head_within(&variable.type_items, depth);
        };
        let mut written = self.types.iter().rev();
        let local = written.find(|local| local.index <= index && local.name == name)?;
        self.type_head_within(local.items, depth)
    }

    /// Notes the type written for the names bound from index `start` on.
    fn note_types(&mut self, start: usize, items: &'w [Item<'w>]) {
        for index in start..self.bound.len() {
            self.types.push(LocalType {
                name: self.bound[index],
                index,
                items,
            });
        }
    }

    /// Binds the names of a bracketed binder, with their type if it is written.
    fn bind_group(&mut self, group: &'w Group<'w>) {
        let start = self.bound.len();
        group_binder_names(group, &mut self.bound);
        if let Some(colon) = colon_index(&group.items)
            && group.opening != "⟨"
        {
            self.note_types(start, &group.items[colon + 1..]);
        }
    }

    /// Reads a bracket group; the names of a dependent-arrow binder stay bound after it.
    fn group(&mut self, group: &'w Group<'w>, following: Option<&Item<'_>>) {
        // A tactic block proves something; what it uses is not written in the type.
        if group.opening == ".{" || group.items.first().is_some_and(|f| f.is_word(&["by"])) {
            return;
        }
        if group.opening == "{" && find(&group.items, 0, &SET_BUILDER_MARKS).is_some() {
            let end = find(&group.items, 0, &SET_BUILDER_STARTS).unwrap_or(0);
            let depth = self.bound.len();
            binder_names(&group.items[..end], &mut self.bound);
            self.walk(&group.items[end..], false);
            self.unbind(depth);
            return;
        }
        let colon = colon_index(&group.items);
        match colon {
            Some(colon) if following.is_some_and(|f| f.is_symbol(&PI_ARROWS)) => {
                self.walk(&group.items[colon + 1..], false);
                self.bind_group(group);
            }
            _ => self.walk(&group.items, true),
        }
    }

    /// Reads the binders after `∀`, `fun` and the like, and returns the index of their last
    /// item. A quantifier with no body binds nothing.
    ///
    /// The symbol after the names is a binder predicate, as in `∀ x ∈ s, p x`. It is not
    /// read if it is one of `atoms`, the further atoms of the notation that the binders
    /// belong to, or if the notation hides what it does with its binders (`hidden`).
    fn binder(
        &mut self,
        items: &'w [Item<'w>],
        index: usize,
        atoms: &[String],
        hidden: bool,
    ) -> usize {
        let Some(end) = find(items, index + 1, &BINDER_ENDS) else {
            return index;
        };
        let mut binders = &items[index + 1..end];
        while binders.first().is_some_and(|b| b.is_symbol(&BINDER_MARKS)) {
            binders = &binders[1..];
        }
        let count = binders.iter().take_while(|b| is_binder_name(b)).count();
        let names: Vec<&Item> = binders[..count].iter().collect();
        let mut annotation = &binders[count..];
        let is_part = |item: &Item<'_>| {
            let token = item.token().filter(|token| !token.is_identifier);
            token.is_some_and(|token| hidden || atoms.iter().any(|atom| *atom == token.text))
        };
        if annotation
            .first()
            .is_some_and(|first| !first.is_symbol(&[":"]) && is_part(first))
        {
            annotation = &annotation[1..];
        }
        self.binders(&names, annotation);
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
                self.bind_group(group);
            }
        }
        self.walk(annotation, false);
        let annotated = annotation
            .split_first()
            .filter(|(first, _)| first.is_symbol(&[":"]));
        for &binder in binders {
            match (binder, annotated) {
                (Item::Group(group), _) => self.bind_group(group),
                (Item::Token(_), Some((_, items))) => {
                    let start = self.bound.len();
                    self.bound.extend(binder.plain());
                    self.note_types(start, items);
                }
                (Item::Token(_), None) => self.bound.extend(binder.plain()),
            }
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
        walker.unbind(0);
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
            walker.unbind(0);
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

/// The names that a statement refers to.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct References<'t> {
    /// The names, in order of first appearance.
    pub names: Vec<&'t str>,
    /// Per name, whether it is written in the type after the signature's colon, and not
    /// only in a binder before it or in the type of a section variable.
    pub in_type: Vec<bool>,
}

/// Lists the names that a statement refers to, in order of first appearance.
///
/// `lookup` turns the identifiers and notations written in the statement into declaration
/// names.
pub fn find_references<'t>(statement: &Statement<'_>, lookup: &dyn Lookup<'t>) -> References<'t> {
    let (kind, scope) = (statement.kind, statement.scope);
    if is_syntax(kind) {
        return References::default();
    }
    // A deriving clause or a value on the same line is not the type.
    let has_value = matches!(kind, DeclKind::Initialize | DeclKind::RegisterOption);
    let is_end =
        |item: &Item| item.is_word(&["deriving"]) || (has_value && item.is_symbol(&VALUE_STARTS));
    let end = statement.signature.iter().position(is_end);
    let items = &statement.signature[..end.unwrap_or(statement.signature.len())];
    let mut walker = Walker::new(lookup, scope, statement.bound);
    let head = match kind {
        DeclKind::ParentProjection => 0,
        _ => head_end(items),
    };
    if head > 0 {
        walker.binders(&head_binders(items, statement.name), &[]);
    }
    walker.reading_type = true;
    walker.walk_open(&items[head..], false);
    walker.reading_type = false;
    let mut used = walker.used_variables.clone();
    for name in &scope.included {
        used.extend(
            walker
                .variables
                .get_key_value(name.as_str())
                .map(|(name, _)| *name),
        );
    }
    for token in statement.body {
        let first = token.text.split('.').next().unwrap_or("");
        if token.is_identifier && !token.after_dot {
            used.extend(walker.variables.get_key_value(first).map(|(name, _)| *name));
        }
    }
    if !scope.variables.is_empty() {
        for name in &scope.omitted {
            used.remove(name.as_str());
        }
        include_variables(&mut walker, scope, used, !is_type(kind));
    }
    References {
        names: walker.references,
        in_type: walker.in_type,
    }
}

/// The constants of a notation's expansion, and the constant it applies if it starts with
/// one.
///
/// `items` are the part of the notation command that holds constants, with the expansion
/// after their last `=>` if they have one. `bound` are the names that the pattern binds and
/// `scope` is the scope in effect at the command.
pub fn expansion_references<'w, 't>(
    items: &'w [Item<'w>],
    bound: &'w [String],
    scope: &'w Scope<'w>,
    lookup: &'w dyn Lookup<'t>,
) -> (Vec<&'t str>, Option<&'t str>) {
    let mut walker = Walker::new(lookup, scope, bound);
    walker.whole_names = true;
    walker.walk(items, false);
    let arrow = items.iter().rposition(|item| item.is_symbol(&["=>"]));
    let head = match walker.type_head(&items[arrow.map_or(0, |arrow| arrow + 1)..]) {
        Some(Head::Constant(name)) => Some(name),
        _ => None,
    };
    (walker.references, head)
}

/// The type that a declaration returns, read from its signature: the number of explicit
/// arguments it takes and, if a type is written, the items of the type after the last of
/// them. A structure field takes the structure as an argument.
///
/// `None` for a declaration that is a type or syntax.
pub fn result_type<'w>(
    signature: &'w [Item<'w>],
    name: &str,
    kind: DeclKind,
) -> Option<(usize, Option<&'w [Item<'w>]>)> {
    if is_syntax(kind) || is_type(kind) {
        return None;
    }
    let explicit = |binder: &Item<'_>| match binder {
        Item::Group(group) if group.opening == "(" => {
            let mut names = Vec::new();
            group_binder_names(group, &mut names);
            names.len()
        }
        Item::Group(_) => 0,
        Item::Token(_) => 1,
    };
    let head = head_end(signature);
    let binders = head_binders(signature, name);
    let mut arity: usize = binders.iter().map(|binder| explicit(binder)).sum();
    arity += usize::from(kind == DeclKind::Field);
    if !signature
        .get(head)
        .is_some_and(|item| item.is_symbol(&[":"]))
    {
        return Some((arity, None));
    }
    let end = signature.iter().position(|item| item.is_symbol(&[":="]));
    let mut rest = &signature[head + 1..end.unwrap_or(signature.len()).max(head + 1)];
    // Leading quantifiers are arguments as well.
    while rest
        .first()
        .is_some_and(|first| first.is_symbol(&["∀"]) || first.is_word(&["forall"]))
    {
        let Some(comma) = find(rest, 1, &[","]) else {
            return Some((arity, None));
        };
        let binders = rest[1..comma].iter().take_while(|b| is_binder_name(b));
        arity += binders.map(explicit).sum::<usize>();
        rest = &rest[comma + 1..];
    }
    while let Some(arrow) = find(rest, 0, &["→", "->"]) {
        arity += 1;
        rest = &rest[arrow + 1..];
    }
    Some((arity, Some(rest)))
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

    /// Resolves identifiers among the test names; there are no notations.
    struct Names<'u> {
        table: &'u NameTable<'static>,
        place: Use<'u, 'static>,
    }

    impl Lookup<'static> for Names<'_> {
        fn resolve<'i>(&self, identifier: &'i str) -> Option<Resolved<'static, 'i>> {
            let found = self.table.resolve_prefix(identifier, &self.place);
            found.map(|(name, fields)| Resolved { name, fields })
        }
    }

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
            let names = Names {
                table: &table,
                place,
            };
            let mut found = find_references(&statement, &names).names;
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
