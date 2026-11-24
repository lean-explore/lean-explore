//! What is in scope at each command of a Lean source file.
//!
//! The parser reports a file's commands in order. This module replays the ones that change
//! name resolution (`namespace`, `section`, `mutual`, `end`, `open`, `export`, `variable`,
//! `include` and `omit`) and records the scope in effect at every command, so a declaration
//! can be resolved with the namespaces that are open where it is written. It also tells
//! each notation of the file how far it reaches.

use super::notation::{Atoms, Reach, Written};
use super::tokens::{
    Item, Token, colon_index, identifier_texts, instance_key, nest, plain_names, tokenize_in,
};
use crate::scan::{CommandKind, Parsed};
use std::sync::Arc;

/// One namespace made available by `open` or `export`.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct OpenDeclaration {
    /// Full names the written namespace can stand for, most specific first.
    pub candidates: Vec<String>,
    /// Names selected with `open A (x y)`; `None` opens every name.
    pub only: Option<Vec<String>>,
    /// Names left out with `open A hiding x`.
    pub hidden: Vec<String>,
    /// Pairs `(new, old)` from `open A renaming old → new`.
    pub renamed: Vec<(String, String)>,
    /// Whether this is `open scoped A`, which opens no names (`only` is empty) and only
    /// makes the scoped notations of the namespace usable.
    pub is_scoped: bool,
}

impl OpenDeclaration {
    /// Whether the open makes the scoped notations of its namespace usable: every form
    /// does except a selection `open A (x)` and `open A renaming x → y`.
    pub fn activates(&self) -> bool {
        self.is_scoped || (self.only.is_none() && self.renamed.is_empty())
    }

    /// Whether names of the namespace can be found through the open by their short names
    /// in general, so that a later `open B` can mean a namespace inside it.
    pub fn is_simple(&self) -> bool {
        self.only.is_none() && self.renamed.is_empty()
    }
}

/// One binder of a `variable` command.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Variable<'a> {
    /// The names it binds; empty for an anonymous instance `[Foo a]`.
    pub names: Vec<String>,
    /// The tokens of its type.
    pub type_items: Vec<Item<'a>>,
    /// Whether it is an instance binder.
    pub is_instance: bool,
}

/// What is in scope at one command of a file, apart from the namespace.
#[derive(Clone, Debug, Default)]
pub struct Scope<'a> {
    /// Open declarations in effect, outermost first.
    pub opened: Vec<Arc<OpenDeclaration>>,
    /// Binders of the `variable` commands in effect, in order.
    pub variables: Vec<Arc<Variable<'a>>>,
    /// Variables that `include` adds to every declaration.
    pub included: Vec<String>,
    /// Variables and instance types that `omit` leaves out.
    pub omitted: Vec<String>,
    /// Byte offset of the enclosing `mutual` block, if there is one.
    pub mutual_start: Option<u32>,
}

/// What one `namespace`, `section` or `mutual` adds to the scope.
#[derive(Default)]
struct Frame<'a> {
    is_mutual: bool,
    start: u32,
    opened: Vec<Arc<OpenDeclaration>>,
    variables: Vec<Arc<Variable<'a>>>,
    included: Vec<String>,
    omitted: Vec<String>,
    /// Indexes of the local notations declared in the frame.
    local_notations: Vec<usize>,
}

impl Frame<'_> {
    fn is_empty(&self) -> bool {
        self.opened.is_empty()
            && self.variables.is_empty()
            && self.included.is_empty()
            && self.omitted.is_empty()
    }
}

fn owned(names: Vec<&str>) -> Vec<String> {
    names.into_iter().map(str::to_string).collect()
}

fn nested_plain_names(items: &[Item<'_>]) -> Vec<String> {
    let mut names = Vec::new();
    plain_names(items, &mut names);
    owned(names)
}

/// What an `omit` command leaves out: the omitted variable names and the keys of omitted
/// instance binders. `arguments` are the items after the keyword.
pub fn omitted_keys(arguments: &[Item<'_>]) -> Vec<String> {
    let mut keys = Vec::new();
    for item in arguments {
        match item {
            Item::Group(group) => keys.push(instance_key(&group.items)),
            Item::Token(_) => keys.extend(
                item.plain()
                    .filter(|name| *name != "in")
                    .map(str::to_string),
            ),
        }
    }
    keys
}

/// Reads the binders of a `variable` command from the items after the keyword: one variable
/// per bracketed binder. A binder without a type, which changes how an earlier variable is
/// bound, has no type items.
pub fn parse_variables<'a>(arguments: Vec<Item<'a>>) -> Vec<Variable<'a>> {
    let mut variables = Vec::new();
    for item in arguments {
        let Item::Group(mut group) = item else {
            continue;
        };
        let is_instance = group.opening == "[";
        match colon_index(&group.items) {
            Some(colon) => {
                let names = nested_plain_names(&group.items[..colon]);
                let type_items = group.items.split_off(colon + 1);
                variables.push(Variable {
                    names,
                    type_items,
                    is_instance,
                });
            }
            None if is_instance => variables.push(Variable {
                names: Vec::new(),
                type_items: group.items,
                is_instance,
            }),
            None => variables.push(Variable {
                names: nested_plain_names(&group.items),
                type_items: Vec::new(),
                is_instance,
            }),
        }
    }
    variables
}

/// Lists the full names a namespace written inside `current` can mean.
fn namespace_candidates(written: &str, current: &str) -> Vec<String> {
    if let Some(rooted) = written.strip_prefix("_root_.") {
        return vec![rooted.to_string()];
    }
    let mut candidates = Vec::new();
    let mut end = current.len();
    while end > 0 {
        candidates.push(format!("{}.{written}", &current[..end]));
        end = current[..end].rfind('.').unwrap_or(0);
    }
    candidates.push(written.to_string());
    candidates
}

/// Reads the open declarations of an `open` or `export` command from the items after the
/// keyword, such as `Set Filter in`. `current` is the namespace the command is in. There is
/// one open declaration per namespace; those of `open scoped` open no names.
pub fn parse_open(arguments: &[Item<'_>], current: &str) -> Vec<OpenDeclaration> {
    let mut items = arguments;
    let mut words = identifier_texts(items);
    if words.last() == Some(&"in") {
        let last = items.iter().rposition(|item| item.is_word(&["in"]));
        items = &items[..last.unwrap_or(items.len())];
        words.pop();
    }
    if words.first() == Some(&"scoped") {
        let scoped = |word: &&str| OpenDeclaration {
            candidates: namespace_candidates(word, current),
            only: Some(Vec::new()),
            is_scoped: true,
            ..Default::default()
        };
        return words[1..].iter().map(scoped).collect();
    }
    let Some(first) = words.first() else {
        return Vec::new();
    };
    let candidates = namespace_candidates(first, current);
    let selection = items.iter().find(|item| matches!(item, Item::Group(_)));
    if let Some(selection) = selection {
        return vec![OpenDeclaration {
            candidates,
            only: Some(nested_plain_names(std::slice::from_ref(selection))),
            ..Default::default()
        }];
    }
    if words.contains(&"hiding") {
        return vec![OpenDeclaration {
            candidates,
            hidden: owned(words[2.min(words.len())..].to_vec()),
            ..Default::default()
        }];
    }
    if words.contains(&"renaming") {
        // Words after `renaming` alternate between the old and the new name.
        let renamed = words[2.min(words.len())..]
            .as_chunks::<2>()
            .0
            .iter()
            .map(|[old, new]| (new.to_string(), old.to_string()))
            .collect();
        return vec![OpenDeclaration {
            candidates,
            renamed,
            ..Default::default()
        }];
    }
    words
        .iter()
        .map(|word| OpenDeclaration {
            candidates: namespace_candidates(word, current),
            ..Default::default()
        })
        .collect()
}

/// The scope in effect at every command of one source file.
#[derive(Default)]
pub struct FileScopes<'a> {
    /// Byte offset where each command starts.
    starts: Vec<u32>,
    /// Per command, the index of its namespace in `Parsed::namespaces` and of its scope.
    at_command: Vec<(u32, u32)>,
    /// The distinct scopes of the file.
    pub scopes: Vec<Scope<'a>>,
    /// Names that `export` makes available, by their full alias name, in file order.
    pub aliases: Vec<(String, Arc<OpenDeclaration>)>,
}

struct Replay<'a, 's> {
    frames: Vec<Frame<'a>>,
    pending: Frame<'a>,
    /// Index of the scope that matches the frames, if it has been recorded.
    current: Option<u32>,
    out: &'s mut FileScopes<'a>,
}

impl<'a> FileScopes<'a> {
    /// Replays the commands of `parsed` once and records the scope at each of them.
    pub fn new(src: &'a [u8], parsed: &Parsed) -> Self {
        FileScopes::with_notations(src, parsed, &Atoms::default(), &mut [])
    }

    /// Like [`FileScopes::new`], reading the atoms of notations whole. `notations` are the
    /// notations the file declares, in order: a local one is told where its section ends
    /// and a scoped one which namespace it is declared in.
    pub fn with_notations(
        src: &'a [u8],
        parsed: &Parsed,
        atoms: &Atoms,
        notations: &mut [Written],
    ) -> Self {
        let mut scopes = FileScopes::default();
        let mut buffer = Vec::new();
        let mut replay = Replay {
            frames: vec![Frame::default()],
            pending: Frame::default(),
            current: None,
            out: &mut scopes,
        };
        let mut next_notation = 0;
        for command in &parsed.commands {
            let scope = replay.snapshot();
            replay.out.starts.push(command.range.start);
            replay.out.at_command.push((command.namespace, scope));
            while notations
                .get(next_notation)
                .is_some_and(|notation| notation.offset <= command.range.start)
            {
                let namespace = &parsed.namespaces[command.namespace as usize];
                replay.declare(next_notation, &mut notations[next_notation], namespace);
                next_notation += 1;
            }
            if command.kind == CommandKind::End {
                replay.end_local_notations(command, src, notations);
            }
            let changes_scope = matches!(
                command.kind,
                CommandKind::Open
                    | CommandKind::Variable
                    | CommandKind::Universe
                    | CommandKind::Namespace
                    | CommandKind::Section
                    | CommandKind::Mutual
                    | CommandKind::End
                    | CommandKind::SetOption
                    | CommandKind::Attribute
                    | CommandKind::Other
            );
            if changes_scope {
                let text = super::slice(src, command.range);
                let tokens = tokenize_in(text, &mut buffer, atoms);
                let namespace = &parsed.namespaces[command.namespace as usize];
                replay.apply(command.kind, tokens, text, namespace, command.range.start);
            } else {
                // A declaration ends any `open ... in` prefixes before it.
                replay.clear_pending();
            }
        }
        scopes
    }

    /// The index into `Parsed::namespaces` and the scope index of the command that contains
    /// a byte offset, or `None` before the first command.
    pub fn at(&self, byte_offset: u32) -> Option<(u32, u32)> {
        let index = self.starts.partition_point(|start| *start <= byte_offset);
        index.checked_sub(1).map(|index| self.at_command[index])
    }
}

impl<'a> Replay<'a, '_> {
    /// Freezes the current frames into a scope and returns its index.
    fn snapshot(&mut self) -> u32 {
        if let Some(index) = self.current {
            return index;
        }
        let frames = || self.frames.iter().chain([&self.pending]);
        let scope = Scope {
            opened: frames().flat_map(|f| f.opened.iter().cloned()).collect(),
            variables: frames().flat_map(|f| f.variables.iter().cloned()).collect(),
            included: frames().flat_map(|f| f.included.iter().cloned()).collect(),
            omitted: frames().flat_map(|f| f.omitted.iter().cloned()).collect(),
            mutual_start: self.frames.iter().find(|f| f.is_mutual).map(|f| f.start),
        };
        let index = self.out.scopes.len() as u32;
        self.out.scopes.push(scope);
        self.current = Some(index);
        index
    }

    /// Records where a notation declared at the current command reaches.
    fn declare(&mut self, index: usize, notation: &mut Written, namespace: &str) {
        let is_bare = matches!(&notation.reach, Reach::Scoped(written) if written.is_empty());
        if matches!(notation.reach, Reach::Local { .. }) {
            let frame = self.frames.last_mut().unwrap();
            frame.local_notations.push(index);
        } else if is_bare && namespace.is_empty() {
            // A `scoped` notation outside every namespace is an error in Lean.
            notation.reach = Reach::Everywhere;
        } else if is_bare {
            notation.reach = Reach::Scoped(namespace.to_string());
        }
    }

    /// Ends the local notations of the frames that an `end` command closes.
    fn end_local_notations(
        &mut self,
        command: &crate::scan::Command,
        src: &[u8],
        notations: &mut [Written],
    ) {
        let text = super::slice(src, command.range);
        let label = std::str::from_utf8(text).unwrap_or("");
        let label = label.trim_start_matches("end").split_whitespace().next();
        let closed = self.closed_frames(label.map_or(1, |word| word.split('.').count()));
        let first = self.frames.len() - closed;
        for frame in &self.frames[first..] {
            for index in &frame.local_notations {
                if let Reach::Local { end } = &mut notations[*index].reach {
                    *end = command.range.start;
                }
            }
        }
    }

    /// How many frames an `end` command with a label of `depth` components closes.
    fn closed_frames(&self, depth: usize) -> usize {
        let depth = match self.frames.last() {
            Some(frame) if frame.is_mutual => 1,
            _ => depth,
        };
        depth.min(self.frames.len() - 1)
    }

    fn clear_pending(&mut self) {
        if !self.pending.is_empty() {
            self.pending = Frame::default();
            self.current = None;
        }
    }

    /// Applies one command, given as its tokens and its text, to the frames.
    fn apply(
        &mut self,
        kind: CommandKind,
        tokens: Vec<Token<'a>>,
        text: &[u8],
        namespace: &str,
        start: u32,
    ) {
        let is_prefix = tokens
            .last()
            .is_some_and(|token| token.is_identifier && token.text == "in");
        let mut items = nest(tokens);
        if !items.is_empty() {
            items.remove(0);
        }
        self.current = None;
        if kind == CommandKind::Open {
            let opened = parse_open(&items, namespace).into_iter().map(Arc::new);
            self.target(is_prefix).opened.extend(opened);
        } else if kind == CommandKind::Variable {
            self.add_variables(items, is_prefix);
        } else if text.starts_with(b"include") {
            let names = owned(identifier_texts(&items));
            self.target(is_prefix).included.extend(names);
        } else if text.starts_with(b"omit") {
            let keys = omitted_keys(&items);
            self.target(is_prefix).omitted.extend(keys);
        } else if matches!(
            kind,
            CommandKind::Namespace | CommandKind::Section | CommandKind::Mutual
        ) {
            self.push(kind, &items, start);
        } else if kind == CommandKind::End {
            self.pop(&items);
        } else if text.starts_with(b"export") {
            self.export(&items, namespace);
        }
        if !is_prefix {
            self.pending = Frame::default();
        }
    }

    /// The frame a command adds to: the pending one for a prefix such as `open A in`.
    fn target(&mut self, is_prefix: bool) -> &mut Frame<'a> {
        if is_prefix {
            &mut self.pending
        } else {
            self.frames.last_mut().unwrap()
        }
    }

    fn add_variables(&mut self, items: Vec<Item<'a>>, is_prefix: bool) {
        let parsed = parse_variables(items);
        let frames = || self.frames.iter().chain([&self.pending]);
        let is_known = |name: &String| {
            frames().any(|frame| frame.variables.iter().any(|v| v.names.contains(name)))
        };
        // A binder without a type only changes how a known variable binds.
        let kept: Vec<_> = parsed
            .into_iter()
            .filter(|v| !v.type_items.is_empty() || !v.names.iter().all(&is_known))
            .map(Arc::new)
            .collect();
        self.target(is_prefix).variables.extend(kept);
    }

    /// Opens one frame per component of a namespace or section name.
    fn push(&mut self, kind: CommandKind, items: &[Item<'_>], start: u32) {
        let is_mutual = kind == CommandKind::Mutual;
        let depth = match identifier_texts(items).first() {
            Some(word) if !is_mutual => word.split('.').count(),
            _ => 1,
        };
        for _ in 0..depth {
            self.frames.push(Frame {
                is_mutual,
                start,
                ..Default::default()
            });
        }
    }

    /// Closes the frames that an `end` command ends.
    fn pop(&mut self, items: &[Item<'_>]) {
        let depth = match identifier_texts(items).first() {
            Some(word) => word.split('.').count(),
            None => 1,
        };
        for _ in 0..self.closed_frames(depth) {
            self.frames.pop();
        }
    }

    /// Records the aliases an `export A (x y)` command creates.
    fn export(&mut self, items: &[Item<'_>], namespace: &str) {
        for declaration in parse_open(items, namespace) {
            let declaration = Arc::new(declaration);
            for name in declaration.only.iter().flatten() {
                let alias = if namespace.is_empty() {
                    name.clone()
                } else {
                    format!("{namespace}.{name}")
                };
                self.out.aliases.push((alias, declaration.clone()));
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::refs::tokens::tokenize;

    /// The scopes of a file together with what is needed to look one up.
    struct Fixture<'a> {
        source: &'a str,
        parsed: Parsed,
        scopes: FileScopes<'a>,
    }

    impl<'a> Fixture<'a> {
        fn new(source: &'a str) -> Self {
            let parsed = crate::parse(source.as_bytes());
            let scopes = FileScopes::new(source.as_bytes(), &parsed);
            Fixture {
                source,
                parsed,
                scopes,
            }
        }

        /// The namespace and scope at the line that starts with `marker`.
        fn at(&self, marker: &str) -> (&str, &Scope<'a>) {
            let offset = self.source.find(marker).unwrap() as u32;
            let (namespace, scope) = self.scopes.at(offset).unwrap();
            (
                &self.parsed.namespaces[namespace as usize],
                &self.scopes.scopes[scope as usize],
            )
        }
    }

    /// The written namespaces a scope has open.
    fn opened(scope: &Scope<'_>) -> Vec<String> {
        scope
            .opened
            .iter()
            .map(|declaration| declaration.candidates.last().unwrap().clone())
            .collect()
    }

    fn variable_names(scope: &Scope<'_>) -> Vec<Vec<String>> {
        scope.variables.iter().map(|v| v.names.clone()).collect()
    }

    /// The items of a command after its keyword.
    fn arguments(text: &str) -> Vec<Item<'_>> {
        let mut items = nest(tokenize(text.as_bytes()));
        items.remove(0);
        items
    }

    fn open(text: &str, current: &str) -> Vec<OpenDeclaration> {
        parse_open(&arguments(text), current)
    }

    fn strings(names: &[&str]) -> Vec<String> {
        names.iter().map(|name| name.to_string()).collect()
    }

    #[test]
    fn parse_open_namespaces() {
        let written = |text: &str| -> Vec<(String, Option<Vec<String>>)> {
            open(text, "")
                .into_iter()
                .map(|d| (d.candidates.last().unwrap().clone(), d.only))
                .collect()
        };
        assert_eq!(
            written("open Set Filter"),
            [("Set".to_string(), None), ("Filter".to_string(), None)]
        );
        assert_eq!(written("open Set in"), [("Set".to_string(), None)]);
        assert_eq!(
            written("open A (x y)"),
            [("A".to_string(), Some(strings(&["x", "y"])))]
        );
        assert_eq!(
            written("open scoped Topology"),
            [("Topology".to_string(), Some(Vec::new()))]
        );
        let scoped = open("open scoped Topology Real in", "A");
        assert_eq!(scoped.len(), 2);
        assert!(
            scoped
                .iter()
                .all(|opened| opened.is_scoped && opened.activates())
        );
        assert_eq!(scoped[0].only, Some(Vec::new()));
        assert_eq!(scoped[1].candidates, ["A.Real", "Real"]);
        assert!(open("open A", "")[0].activates());
        assert!(!open("open A (x)", "")[0].activates());
        assert!(written("open").is_empty());
        assert!(written("open in").is_empty());
    }

    #[test]
    fn parse_open_hiding_and_renaming() {
        let hiding = open("open A hiding x y", "");
        assert_eq!(hiding.len(), 1);
        assert_eq!(hiding[0].hidden, strings(&["x", "y"]));
        let renaming = open("open A renaming x → y, p → q", "");
        assert_eq!(renaming.len(), 1);
        let expected =
            [("y", "x"), ("q", "p")].map(|(new, old)| (new.to_string(), old.to_string()));
        assert_eq!(renaming[0].renamed, expected);
    }

    #[test]
    fn parse_open_candidates_follow_the_current_namespace() {
        let relative = open("open A", "N.M");
        assert_eq!(relative[0].candidates, ["N.M.A", "N.A", "A"]);
        let rooted = open("open _root_.A", "N.M");
        assert_eq!(rooted[0].candidates, ["A"]);
    }

    #[test]
    fn parse_variables_tells_binders_apart() {
        let found = parse_variables(arguments("variable (a b : T) [inst : C a] [C b] {c} extra"));
        let [typed, named, anonymous, untyped] = found.as_slice() else {
            panic!("expected four variables");
        };
        assert!(typed.names == ["a", "b"] && !typed.is_instance);
        assert!(named.names == ["inst"] && named.is_instance);
        assert!(anonymous.names.is_empty() && anonymous.is_instance);
        assert!(untyped.names == ["c"] && untyped.type_items.is_empty());
    }

    #[test]
    fn omitted_keys_are_names_and_instance_keys() {
        assert_eq!(
            omitted_keys(&arguments("omit x [Good a] in")),
            ["x", "[Good a]"]
        );
    }

    #[test]
    fn opens_and_variables_end_with_their_section() {
        let fixture = Fixture::new(
            "open Set\nsection\nopen Other\nvariable (x : Nat)\ntheorem first : True := trivial\n\
             end\ntheorem second : True := trivial\n",
        );
        let (_, first) = fixture.at("theorem first");
        let (_, second) = fixture.at("theorem second");
        assert_eq!(opened(first), ["Set", "Other"]);
        assert_eq!(variable_names(first), [["x"]]);
        assert_eq!(opened(second), ["Set"]);
        assert!(second.variables.is_empty());
    }

    #[test]
    fn end_closes_one_frame_per_component() {
        let fixture = Fixture::new(
            "namespace A.B\nopen Inner\nend B\ntheorem middle : True := trivial\nend A\n\
             namespace C.D\nopen Other\nend C.D\ntheorem last : True := trivial\n",
        );
        let (namespace, middle) = fixture.at("theorem middle");
        assert!(opened(middle).is_empty());
        assert_eq!(namespace, "A");
        assert!(opened(fixture.at("theorem last").1).is_empty());
    }

    #[test]
    fn mutual_end_keeps_the_enclosing_scope() {
        let source = "namespace P\nopen Nat\nmutual\ndef a : Nat := 1\nend\n\
                      theorem t : True := trivial\nend P\n";
        let fixture = Fixture::new(source);
        let (_, inside) = fixture.at("def a");
        assert_eq!(inside.mutual_start, source.find("mutual").map(|i| i as u32));
        let (namespace, after) = fixture.at("theorem t");
        assert_eq!(opened(after), ["Nat"]);
        assert_eq!(after.mutual_start, None);
        assert_eq!(namespace, "P");
    }

    #[test]
    fn prefix_commands_reach_the_next_declaration_only() {
        let fixture = Fixture::new(
            "open A in\nset_option pp.all true in\nvariable (x : Nat) in\n\
             theorem first : True := trivial\ntheorem second : True := trivial\n",
        );
        let (_, first) = fixture.at("theorem first");
        let (_, second) = fixture.at("theorem second");
        assert_eq!(opened(first), ["A"]);
        assert_eq!(variable_names(first), [["x"]]);
        assert!(opened(second).is_empty() && second.variables.is_empty());
    }

    #[test]
    fn typeless_variable_keeps_the_earlier_type() {
        let fixture = Fixture::new(
            "variable (x : Nat)\nvariable {x}\nvariable {y}\ntheorem t : True := trivial\n",
        );
        let (_, scope) = fixture.at("theorem t");
        assert_eq!(variable_names(scope), [["x"], ["y"]]);
        assert!(!scope.variables[0].type_items.is_empty());
        assert!(scope.variables[1].type_items.is_empty());
    }

    #[test]
    fn include_and_omit_are_recorded() {
        let fixture = Fixture::new(
            "variable (x : Nat)\ninclude x\nomit [Good a] in\ntheorem t : True := trivial\n",
        );
        let (_, scope) = fixture.at("theorem t");
        assert_eq!(scope.included, ["x"]);
        assert_eq!(scope.omitted, ["[Good a]"]);
    }

    #[test]
    fn export_records_aliases() {
        let fixture = Fixture::new("namespace N\nexport A (x y)\nend N\n");
        let aliases = &fixture.scopes.aliases;
        let names: Vec<&str> = aliases.iter().map(|(alias, _)| alias.as_str()).collect();
        assert_eq!(names, ["N.x", "N.y"]);
        let expected = OpenDeclaration {
            candidates: strings(&["N.A", "A"]),
            only: Some(strings(&["x", "y"])),
            ..Default::default()
        };
        assert_eq!(*aliases[0].1, expected);
    }

    #[test]
    fn scope_before_the_first_command_and_stray_end() {
        let empty = Fixture::new("");
        assert!(empty.scopes.at(0).is_none());
        let fixture = Fixture::new("end\ntheorem t : True := trivial\n");
        let (namespace, scope) = fixture.at("theorem t");
        assert_eq!(namespace, "");
        assert!(opened(scope).is_empty() && scope.variables.is_empty());
        assert!(scope.included.is_empty() && scope.omitted.is_empty());
        assert_eq!(scope.mutual_start, None);
    }
}
