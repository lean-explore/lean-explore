//! Resolution of identifiers written in Lean source to declaration names.
//!
//! The rules follow `Lean/ResolveName.lean`: an identifier is looked up in the namespaces of
//! the declaration it is written in, innermost first, then as a root name, through the
//! namespaces opened there and through exported aliases. Where several names match equally,
//! Lean chooses by type, so nothing is resolved.

use super::scopes::OpenDeclaration;
use std::collections::{HashMap, HashSet};
use std::sync::Arc;

/// Where a declaration is written.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Position {
    /// Index of the declaration's module.
    pub module: u32,
    /// Byte offset of the declaration in its file.
    pub offset: u32,
}

/// What is known about one declaration name.
#[derive(Clone, Copy, Debug, Default)]
pub struct Entry {
    /// Where the package declares the name first; `None` for a name known from elsewhere.
    pub position: Option<Position>,
    /// Whether the name is visible only in the module that declares it.
    pub is_private: bool,
    /// Whether the name cannot be reached by its last component alone.
    pub is_protected: bool,
    /// The file and the index in it of the declaration, if its source is at hand.
    pub source: Option<(u32, u32)>,
}

/// An open declaration bound to the namespace it opens.
pub type BoundOpen = (String, Arc<OpenDeclaration>);

/// The place an identifier is written.
#[derive(Clone, Copy, Debug, Default)]
pub struct Use<'u, 't> {
    /// Open declarations in effect there, bound by [`NameTable::open_namespaces`].
    pub opened: &'u [BoundOpen],
    /// Where the declaration is written, if known.
    pub position: Option<Position>,
    /// Byte offset of the enclosing `mutual` block, if there is one.
    pub mutual_start: Option<u32>,
    /// Namespaces searched first, innermost first.
    pub namespaces: &'u [&'u str],
    /// Short names of the fields beside a structure field, with their full names.
    pub siblings: Option<&'u HashMap<&'t str, &'t str>>,
}

/// What an identifier can stand for.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Found<'t> {
    Nothing,
    One(&'t str),
    Several,
}

impl<'t> Found<'t> {
    fn add(&mut self, name: &'t str) {
        *self = match *self {
            Found::Nothing => Found::One(name),
            Found::One(other) if other == name => Found::One(name),
            _ => Found::Several,
        };
    }
}

/// Lists the namespaces a declaration is in, innermost first.
pub fn enclosing_namespaces(name: &str) -> Vec<&str> {
    let mut namespaces = Vec::new();
    let mut end = name.len();
    while let Some(dot) = name[..end].rfind('.') {
        namespaces.push(&name[..dot]);
        end = dot;
    }
    namespaces
}

/// The declarations that identifiers can resolve to.
#[derive(Debug, Default)]
pub struct NameTable<'t> {
    /// Every declaration name that can be referred to.
    names: HashMap<&'t str, Entry>,
    /// Every proper prefix of a known name.
    namespaces: HashSet<&'t str>,
    /// Names that `export` makes available, by their full alias name.
    pub aliases: HashMap<String, Arc<OpenDeclaration>>,
    /// Per module, a bit set of the modules whose declarations it can see.
    pub visible: Vec<Vec<u64>>,
}

impl<'t> NameTable<'t> {
    /// Adds a name, with the namespaces it is in, and returns what is known about it.
    pub fn declare(&mut self, name: &'t str) -> &mut Entry {
        let mut end = name.rfind('.');
        while let Some(dot) = end.filter(|dot| *dot > 0) {
            if !self.namespaces.insert(&name[..dot]) {
                break;
            }
            end = name[..dot].rfind('.');
        }
        self.names.entry(name).or_default()
    }

    /// Whether `name` is a namespace: a proper prefix of a known name.
    pub fn is_namespace(&self, name: &str) -> bool {
        self.namespaces.contains(name)
    }

    /// The table's own copy of `name` and what is known about it.
    pub fn get(&self, name: &str) -> Option<(&'t str, &Entry)> {
        self.names
            .get_key_value(name)
            .map(|(key, entry)| (*key, entry))
    }

    /// Binds each open declaration to the namespace it opens.
    ///
    /// Lean binds the written namespace to its first match, innermost first. A namespace that
    /// is not found that way can be one inside a namespace that an earlier `open` opened, as
    /// in `open Outer Nested`.
    pub fn open_namespaces(&self, opened: &[Arc<OpenDeclaration>]) -> Vec<BoundOpen> {
        let mut bound: Vec<BoundOpen> = Vec::new();
        for declaration in opened {
            // `open scoped A` opens no names.
            let written = declaration
                .candidates
                .last()
                .filter(|_| !declaration.is_scoped);
            let Some(written) = written else {
                continue;
            };
            let inside = bound
                .iter()
                .rev()
                .map(|(namespace, _)| format!("{namespace}.{written}"));
            let candidate = declaration
                .candidates
                .iter()
                .cloned()
                .chain(inside)
                .find(|candidate| self.namespaces.contains(candidate.as_str()));
            if let Some(candidate) = candidate {
                bound.push((candidate, declaration.clone()));
            }
        }
        bound
    }

    /// The table's copy of `name` if it is declared and visible where it is used.
    pub fn visible_name(&self, name: &str, place: &Use<'_, 't>) -> Option<&'t str> {
        let (key, entry) = self.get(name)?;
        self.visible_entry(key, entry, place)
    }

    fn visible_entry(&self, key: &'t str, entry: &Entry, place: &Use<'_, 't>) -> Option<&'t str> {
        let (Some(declared), Some(used)) = (entry.position, place.position) else {
            return Some(key);
        };
        let is_visible = if declared.module != used.module {
            !entry.is_private && self.sees(used.module, declared.module)
        } else {
            declared.offset < used.offset
                || place
                    .mutual_start
                    .is_some_and(|start| declared.offset > start)
        };
        is_visible.then_some(key)
    }

    /// The table's copies of a namespace and of the namespaces around it, innermost
    /// first, as far as they are known.
    pub fn namespace(&self, name: &str) -> Vec<&'t str> {
        let mut found = Vec::new();
        let mut end = Some(name.len());
        while let Some(length) = end {
            found.extend(self.namespaces.get(&name[..length]).copied());
            end = name[..length].rfind('.');
        }
        found
    }

    /// Whether `module` imports `other`, directly or not.
    pub fn sees(&self, module: u32, other: u32) -> bool {
        let Some(bits) = self.visible.get(module as usize) else {
            return true;
        };
        let word = bits.get(other as usize / 64).copied().unwrap_or(0);
        word >> (other % 64) & 1 == 1
    }

    /// The candidate if a short identifier can stand for it.
    fn reachable(&self, candidate: &str, identifier: &str, place: &Use<'_, 't>) -> Option<&'t str> {
        let (key, entry) = self.get(candidate)?;
        if !identifier.contains('.') && entry.is_protected {
            return None;
        }
        self.visible_entry(key, entry, place)
    }

    /// Adds what an identifier can mean through one open declaration.
    fn through_open(
        &self,
        identifier: &str,
        (namespace, opened): (&str, &OpenDeclaration),
        place: &Use<'_, 't>,
        found: &mut Found<'t>,
    ) {
        let is_selected = |only: &Vec<String>| only.iter().any(|name| name == identifier);
        if opened.only.as_ref().is_some_and(|only| !is_selected(only)) {
            return;
        }
        let first = identifier.split('.').next().unwrap_or(identifier);
        if opened.hidden.iter().any(|name| name == first) {
            return;
        }
        let renamed = opened
            .renamed
            .iter()
            .rev()
            .find(|(new, _)| new == identifier);
        let target = renamed.map_or(identifier, |(_, old)| old.as_str());
        let candidate = format!("{namespace}.{target}");
        let selected = opened.only.is_some() || target != identifier;
        let name = if selected {
            self.visible_name(&candidate, place)
        } else {
            self.reachable(&candidate, target, place)
        };
        if let Some(name) = name {
            found.add(name);
        }
    }

    /// The names an identifier can stand for, at the first level found.
    ///
    /// Names in the declaration's own namespaces come first, innermost first, then an exact
    /// dotted root name. After them, the root name, opened namespaces and exported aliases
    /// count equally, as Lean would choose among them by type.
    fn candidates(&self, identifier: &str, place: &Use<'_, 't>) -> Found<'t> {
        if let Some(sibling) = place.siblings.and_then(|siblings| siblings.get(identifier)) {
            return Found::One(sibling);
        }
        let mut candidate = String::new();
        for namespace in place.namespaces {
            candidate.clear();
            candidate.extend([*namespace, ".", identifier]);
            if let Some(name) = self.reachable(&candidate, identifier, place) {
                return Found::One(name);
            }
        }
        let mut found = Found::Nothing;
        if let Some(name) = self.visible_name(identifier, place) {
            found.add(name);
            if identifier.contains('.') {
                return found;
            }
        }
        for (namespace, opened) in place.opened {
            self.through_open(
                identifier,
                (namespace.as_str(), opened.as_ref()),
                place,
                &mut found,
            );
        }
        if self.aliases.is_empty() {
            return found;
        }
        for namespace in place.namespaces.iter().chain(&[""]) {
            candidate.clear();
            if namespace.is_empty() {
                candidate.push_str(identifier);
            } else {
                candidate.extend([*namespace, ".", identifier]);
            }
            let Some(exported) = self.aliases.get(&candidate) else {
                continue;
            };
            for (target, opened) in self.open_namespaces(std::slice::from_ref(exported)) {
                self.through_open(
                    identifier,
                    (target.as_str(), opened.as_ref()),
                    place,
                    &mut found,
                );
            }
        }
        found
    }

    /// Resolves an identifier written inside a declaration to a known name.
    ///
    /// A dotted identifier whose full form is unknown is retried without its trailing
    /// components, so `Foo.bar.baz` resolves to `Foo.bar` when `baz` is a field access.
    /// Returns `None` if nothing matches or several names match equally.
    pub fn resolve(&self, identifier: &str, place: &Use<'_, 't>) -> Option<&'t str> {
        self.resolve_prefix(identifier, place).map(|(name, _)| name)
    }

    /// Like [`NameTable::resolve`], and also returns the components of the identifier
    /// after the resolved name, which are field accesses: `baz` for `Foo.bar.baz`.
    pub fn resolve_prefix<'i>(
        &self,
        identifier: &'i str,
        place: &Use<'_, 't>,
    ) -> Option<(&'t str, &'i str)> {
        if let Some(exact) = identifier.strip_prefix("_root_.") {
            return self.visible_name(exact, place).map(|name| (name, ""));
        }
        let mut end = identifier.len();
        loop {
            match self.candidates(&identifier[..end], place) {
                Found::One(name) => {
                    let fields = identifier[end..].strip_prefix('.').unwrap_or("");
                    return Some((name, fields));
                }
                Found::Several => return None,
                Found::Nothing => end = identifier[..end].rfind('.')?,
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::refs::scopes::parse_open;
    use crate::refs::tokens::{nest, tokenize};

    const NAMES: [&str; 13] = [
        "T",
        "Nat",
        "Nat.succ",
        "Pkg.T",
        "Pkg.Point",
        "Pkg.Point.norm",
        "Pkg.addOne",
        "Other.helper",
        "Other.Path.mark",
        "Path.mark",
        "A.f",
        "B.f",
        "A.g",
    ];

    fn table() -> NameTable<'static> {
        table_without("")
    }

    fn table_without(left_out: &str) -> NameTable<'static> {
        let mut table = NameTable::default();
        for name in NAMES.iter().filter(|name| **name != left_out) {
            table.declare(name);
        }
        table
    }

    fn open_declarations(text: &str, current: &str) -> Vec<Arc<OpenDeclaration>> {
        let mut items = nest(tokenize(text.as_bytes()));
        items.remove(0);
        let opened = parse_open(&items, current);
        opened.into_iter().map(Arc::new).collect()
    }

    /// Resolves `identifier` written inside the declaration `name`.
    fn resolve_in<'t>(table: &NameTable<'t>, identifier: &str, name: &str) -> Option<&'t str> {
        resolve_with(table, identifier, name, "", "")
    }

    /// Resolves `identifier` inside `name` after the command `open`, written in `current`.
    fn resolve_with<'t>(
        table: &NameTable<'t>,
        identifier: &str,
        name: &str,
        open: &str,
        current: &str,
    ) -> Option<&'t str> {
        let opened = if open.is_empty() {
            Vec::new()
        } else {
            table.open_namespaces(&open_declarations(open, current))
        };
        let place = Use {
            opened: &opened,
            namespaces: &enclosing_namespaces(name),
            ..Default::default()
        };
        table.resolve(identifier, &place)
    }

    /// Resolves `identifier` inside `Pkg.thing` written at a position.
    fn resolve_at<'t>(
        table: &NameTable<'t>,
        identifier: &str,
        position: Position,
        mutual_start: Option<u32>,
    ) -> Option<&'t str> {
        let place = Use {
            position: Some(position),
            mutual_start,
            namespaces: &["Pkg"],
            ..Default::default()
        };
        table.resolve(identifier, &place)
    }

    fn at(module: u32, offset: u32) -> Position {
        Position { module, offset }
    }

    #[test]
    fn namespaces_are_the_prefixes_of_names() {
        let table = table();
        let mut namespaces: Vec<&str> = table.namespaces.iter().copied().collect();
        namespaces.sort();
        assert_eq!(
            namespaces,
            [
                "A",
                "B",
                "Nat",
                "Other",
                "Other.Path",
                "Path",
                "Pkg",
                "Pkg.Point"
            ]
        );
        assert_eq!(enclosing_namespaces("A.B.c"), ["A.B", "A"]);
        assert!(enclosing_namespaces("c").is_empty());
    }

    #[test]
    fn innermost_namespace_wins_over_the_root() {
        let table = table();
        assert_eq!(resolve_in(&table, "T", "Pkg.thing"), Some("Pkg.T"));
        assert_eq!(resolve_in(&table, "T", "Elsewhere.thing"), Some("T"));
        assert_eq!(resolve_in(&table, "missing", "Pkg.thing"), None);
    }

    #[test]
    fn explicit_namespaces_replace_the_ones_of_the_name() {
        let place = Use {
            namespaces: &["Pkg"],
            ..Default::default()
        };
        assert_eq!(table().resolve("addOne", &place), Some("Pkg.addOne"));
    }

    #[test]
    fn root_marker_is_an_exact_lookup() {
        let table = table();
        assert_eq!(resolve_in(&table, "_root_.T", "Pkg.thing"), Some("T"));
        assert_eq!(resolve_in(&table, "_root_.missing", "Pkg.thing"), None);
    }

    #[test]
    fn trailing_components_are_field_access() {
        let table = table();
        assert_eq!(
            resolve_in(&table, "Nat.succ.injective", "t"),
            Some("Nat.succ")
        );
        assert_eq!(resolve_in(&table, "unknown.field", "t"), None);
    }

    #[test]
    fn open_makes_members_available() {
        let table = table();
        assert_eq!(resolve_in(&table, "helper", "t"), None);
        let found = resolve_with(&table, "helper", "t", "open Other", "");
        assert_eq!(found, Some("Other.helper"));
    }

    #[test]
    fn open_binds_to_the_innermost_namespace() {
        let found = resolve_with(&table(), "norm", "Pkg.thing", "open Point", "Pkg");
        assert_eq!(found, Some("Pkg.Point.norm"));
    }

    #[test]
    fn open_reaches_a_namespace_of_an_earlier_open() {
        let table = table_without("Path.mark");
        let found = resolve_with(&table, "mark", "t", "open Other Path", "");
        assert_eq!(found, Some("Other.Path.mark"));
    }

    #[test]
    fn exact_dotted_root_name_beats_an_open() {
        let found = resolve_with(&table(), "Path.mark", "t", "open Other", "");
        assert_eq!(found, Some("Path.mark"));
    }

    #[test]
    fn equal_candidates_resolve_to_nothing() {
        let table = table();
        assert_eq!(resolve_with(&table, "f", "t", "open A B", ""), None);
        assert_eq!(resolve_with(&table, "g", "t", "open A B", ""), Some("A.g"));
    }

    #[test]
    fn selective_hiding_and_renaming_opens() {
        let table = table();
        assert_eq!(resolve_with(&table, "f", "t", "open A (g)", ""), None);
        assert_eq!(
            resolve_with(&table, "g", "t", "open A (g)", ""),
            Some("A.g")
        );
        assert_eq!(resolve_with(&table, "g", "t", "open A hiding g", ""), None);
        let renamed = resolve_with(&table, "h", "t", "open A renaming g → h", "");
        assert_eq!(renamed, Some("A.g"));
    }

    #[test]
    fn unknown_opened_namespace_provides_nothing() {
        assert_eq!(resolve_with(&table(), "f", "t", "open Missing", ""), None);
    }

    #[test]
    fn exported_alias_resolves_to_its_target() {
        let mut table = table();
        let exported = open_declarations("export A (g)", "N").remove(0);
        table.aliases.insert("N.g".to_string(), exported);
        assert_eq!(resolve_in(&table, "g", "N.thing"), Some("A.g"));
        assert_eq!(resolve_in(&table, "g", "M.thing"), None);
    }

    #[test]
    fn protected_names_need_a_qualified_spelling() {
        let mut table = table();
        table.declare("Pkg.T").is_protected = true;
        assert_eq!(resolve_in(&table, "T", "Pkg.thing"), Some("T"));
        assert_eq!(resolve_in(&table, "Pkg.T", "thing"), Some("Pkg.T"));
        // A selective open reaches the protected name, which then competes with the root `T`.
        let selected = resolve_with(&table, "T", "t", "open Pkg (T)", "");
        assert_eq!(selected, None);
    }

    #[test]
    fn private_names_shadow_only_in_their_module() {
        let mut table = table();
        let entry = table.declare("Pkg.T");
        entry.is_private = true;
        entry.position = Some(at(0, 10));
        table.visible = vec![vec![0b11], vec![0b11]];
        assert_eq!(resolve_at(&table, "T", at(0, 50), None), Some("Pkg.T"));
        assert_eq!(resolve_at(&table, "T", at(1, 50), None), Some("T"));
    }

    #[test]
    fn later_declarations_of_a_file_are_not_visible() {
        let mut table = table();
        table.declare("T").position = Some(at(0, 0));
        table.declare("Pkg.T").position = Some(at(0, 100));
        assert_eq!(resolve_at(&table, "T", at(0, 50), None), Some("T"));
        assert_eq!(resolve_at(&table, "T", at(0, 150), None), Some("Pkg.T"));
    }

    #[test]
    fn a_mutual_block_sees_its_later_declarations() {
        let mut table = table();
        table.declare("Pkg.T").position = Some(at(0, 100));
        assert_eq!(resolve_at(&table, "T", at(0, 50), Some(40)), Some("Pkg.T"));
    }

    #[test]
    fn names_of_modules_that_are_not_imported_are_not_visible() {
        let mut table = table();
        table.declare("Pkg.T").position = Some(at(1, 0));
        assert_eq!(resolve_at(&table, "T", at(0, 50), None), Some("Pkg.T"));
        table.visible = vec![vec![0b11], vec![0b10]];
        assert_eq!(resolve_at(&table, "T", at(0, 50), None), Some("Pkg.T"));
        table.visible = vec![vec![0b01], vec![0b10]];
        assert_eq!(resolve_at(&table, "T", at(0, 50), None), Some("T"));
    }

    #[test]
    fn sibling_fields_resolve_to_the_structure_field() {
        let mut table = NameTable::default();
        for name in ["S.val", "S.other", "val"] {
            table.declare(name);
        }
        let siblings = HashMap::from([("val", "S.val")]);
        let place = Use {
            siblings: Some(&siblings),
            ..Default::default()
        };
        assert_eq!(table.resolve("val", &place), Some("S.val"));
    }
}
