//! The declarations that each declaration of a package refers to.
//!
//! [`resolve_references`] reads the identifiers written in every signature and resolves them
//! by Lean's lookup rules, without elaborating anything: against the namespaces of the
//! declaration, the root namespace, the namespaces opened at that point of the file and the
//! names exported there. A name is visible only after it is declared, inside its `mutual`
//! block, or from a module that imports the one declaring it.
//!
//! Two kinds of reference that are not written as identifiers are found as well. A token
//! of a notation declared in source (`+`, `ℝ`, `⥤`) stands for the constants of the
//! notation's expansion ([`notation`]). A field written on a local name whose type is
//! written (`F.obj` with `F : C ⥤ D`) is the declaration of that name in the namespace of
//! the type's head constant, or of a structure it extends.
//!
//! Every resolved name is reported, including names a caller may not keep, such as private
//! declarations, structure fields and `.mk` constructors: they shadow other candidates, so
//! leaving them out here would resolve the identifier to the wrong declaration.

pub mod names;
pub mod notation;
pub mod scopes;
pub mod tokens;
pub mod walk;

use crate::keywords::Modifier;
use crate::scan::{CommandKind, Decl, DeclKind, Parsed, Span, StructureInfo};
use names::{BoundOpen, NameTable, Position, Use, enclosing_namespaces};
use notation::{
    Atoms, Before, Fired, Notation, NotationTable, Part, Reach, Written, read_notations,
};
use rayon::prelude::*;
use scopes::{FileScopes, OpenDeclaration, Scope};
use std::cell::RefCell;
use std::collections::HashMap;
use std::sync::Arc;
use tokens::{Item, nest, tokenize_in};
pub use walk::References;
use walk::{
    Head, Lookup, Resolved, Statement, Used, Walker, expansion_references, find_references,
    reads_body, result_type, signature_binder_names,
};

/// How many `extends` clauses are followed to find an inherited field.
const MAX_PARENT_DEPTH: usize = 8;

/// One source file of a package.
#[derive(Clone, Copy, Debug)]
pub struct SourceFile<'a> {
    /// The file's module name, such as `Mathlib.Data.Nat.Basic`.
    pub module: &'a str,
    /// The file's bytes.
    pub source: &'a [u8],
    /// The result of parsing the file.
    pub parsed: &'a Parsed,
}

/// The bytes of a span, or nothing if the span does not lie in the source.
fn slice(src: &[u8], span: Span) -> &[u8] {
    src.get(span.start as usize..span.end as usize)
        .unwrap_or(&[])
}

/// What one file contributes to resolution apart from its declarations.
struct FileIndex<'a> {
    file: SourceFile<'a>,
    scopes: FileScopes<'a>,
    /// The modules the file imports, as written.
    imports: Vec<String>,
    /// Per declaration, whether it has a name that others can refer to.
    named: Vec<bool>,
    /// The notations the file declares, in order.
    notations: Vec<Written>,
}

impl<'a> FileIndex<'a> {
    fn new(
        file: SourceFile<'a>,
        include_approximate: bool,
        atoms: &Atoms,
        mut notations: Vec<Written>,
    ) -> Self {
        let mut buffer = Vec::new();
        let commands = file.parsed.commands.iter();
        let mut imports: Vec<String> = commands
            .filter(|command| command.kind == CommandKind::Import)
            .filter_map(|command| {
                let tokens = tokens::tokenize_with(slice(file.source, command.range), &mut buffer);
                tokens.last().map(|token| token.text.to_string())
            })
            .collect();
        // Every file imports `Init` unless it says `prelude`.
        let is_prelude = |command: &crate::scan::Command| {
            command.kind == CommandKind::Other && slice(file.source, command.head) == b"prelude"
        };
        if !file.parsed.commands.iter().take(4).any(is_prelude) {
            imports.push("Init".to_string());
        }
        let is_named = |declaration: &Decl| {
            let has_name = declaration.name.as_deref().is_some_and(|n| !n.is_empty());
            has_name && (include_approximate || !declaration.approximate)
        };
        let scopes = FileScopes::with_notations(file.source, file.parsed, atoms, &mut notations);
        FileIndex {
            file,
            scopes,
            imports,
            named: file.parsed.decls.iter().map(is_named).collect(),
            notations,
        }
    }

    /// The declarations that others can refer to, with their indexes and names.
    fn named_declarations(&self) -> impl Iterator<Item = (u32, &'a str, &'a Decl)> {
        let declarations = self.file.parsed.decls.iter().zip(&self.named);
        declarations
            .enumerate()
            .filter(|(_, (_, named))| **named)
            .filter_map(|(index, (declaration, _))| {
                Some((index as u32, declaration.name.as_deref()?, declaration))
            })
    }
}

/// Computes, per module, a bit set of itself and everything it imports.
fn visible_modules(imports: &[Vec<usize>]) -> Vec<Vec<u64>> {
    let count = imports.len();
    let words = count.div_ceil(64);
    let mut visible = vec![Vec::new(); count];
    let mut done = vec![false; count];
    let mut started = vec![false; count];
    for root in 0..count {
        let mut stack = vec![root];
        while let Some(&module) = stack.last() {
            started[module] = true;
            let waiting = imports[module].iter().filter(|other| !started[**other]);
            let before = stack.len();
            if !done[module] {
                stack.extend(waiting);
            }
            if stack.len() > before {
                continue;
            }
            let mut mask = vec![0u64; words];
            mask[module / 64] |= 1 << (module % 64);
            for other in &imports[module] {
                for (word, bits) in mask.iter_mut().zip(&visible[*other]) {
                    *word |= bits;
                }
            }
            visible[module] = mask;
            done[module] = true;
            stack.pop();
        }
    }
    visible
}

/// The names under which a module can be imported: a context file is named by its path,
/// so every ending of its name that starts a component is one.
fn import_names(module: &str, is_context: bool) -> impl Iterator<Item = &str> {
    let starts = module.match_indices('.').map(|(dot, _)| dot + 1);
    let endings = starts
        .filter(move |_| is_context)
        .map(|start| &module[start..]);
    std::iter::once(module).chain(endings)
}

/// Builds the table of names the package's signatures can refer to: the declarations of
/// its files, which are the first `package` of `files`, those of the context files after
/// them and `known_names`.
fn name_table<'a, S: AsRef<str>>(
    files: &[FileIndex<'a>],
    package: usize,
    known_names: &'a [S],
) -> NameTable<'a> {
    let mut table = NameTable::default();
    let mut indexes = HashMap::new();
    for (index, file) in files.iter().enumerate() {
        let is_context = index >= package;
        for name in import_names(file.file.module, is_context) {
            // A package module comes first and keeps its name.
            indexes.entry(name).or_insert(index);
        }
        for (alias, declaration) in &file.scopes.aliases {
            table.aliases.insert(alias.clone(), declaration.clone());
        }
        for (number, name, declaration) in file.named_declarations() {
            let is_private = declaration.modifiers & Modifier::Private.bit() != 0;
            if is_context && is_private {
                continue;
            }
            let entry = table.declare(name);
            match entry.position {
                // A name declared twice keeps its first declaration, and is private only if
                // every declaration of it is.
                Some(_) => entry.is_private &= is_private,
                None => {
                    entry.is_private = is_private;
                    entry.source = Some((index as u32, number));
                    entry.position = Some(Position {
                        module: index as u32,
                        offset: declaration.range.start,
                    });
                }
            }
            entry.is_protected |= declaration.modifiers & Modifier::Protected.bit() != 0;
        }
    }
    for name in known_names {
        let entry = table.declare(name.as_ref());
        entry.is_private = false;
        // A known name is visible everywhere, also if a context file declares it.
        if entry
            .source
            .is_some_and(|(file, _)| file as usize >= package)
        {
            entry.position = None;
        }
    }
    let imports: Vec<Vec<usize>> = files
        .iter()
        .map(|file| {
            let written = file.imports.iter();
            written
                .filter_map(|name| indexes.get(name.as_str()).copied())
                .collect()
        })
        .collect();
    table.visible = visible_modules(&imports);
    table
}

/// The namespaces a declaration's signature is resolved in, innermost first.
///
/// A declaration `Foo.bar` inside `namespace A` sees `A.Foo` and `A`. One written
/// `_root_.Foo.bar` keeps the enclosing `A` only. A field or constructor is resolved where
/// its structure or inductive type is.
fn lookup_namespaces<'a>(
    declaration: &'a Decl,
    namespace: &'a str,
    declarations: &'a [Decl],
) -> Vec<&'a str> {
    let mut owner = declaration;
    // A parent comes before its children, so the walk ends.
    while let Some(parent) = owner.parent.and_then(|p| declarations.get(p as usize)) {
        if std::ptr::eq(parent, owner) {
            break;
        }
        owner = parent;
    }
    let name = owner.name.as_deref().unwrap_or("");
    let inside = name
        .strip_prefix(namespace)
        .is_some_and(|rest| rest.starts_with('.'));
    if namespace.is_empty() || inside {
        return enclosing_namespaces(name);
    }
    let mut namespaces = vec![namespace];
    namespaces.extend(enclosing_namespaces(namespace));
    namespaces
}

/// What the fields and constructors of one structure or inductive type can see.
#[derive(Default)]
struct Owner<'a> {
    /// The names bound by the owner's binders; filled in when first needed.
    bound: Option<Vec<String>>,
    /// The owner's fields by their short names.
    siblings: HashMap<&'a str, &'a str>,
}

/// Whether `namespace` is `inside` or a namespace within it.
fn is_within(namespace: &str, inside: &str) -> bool {
    let rest = namespace.strip_prefix(inside);
    rest.is_some_and(|rest| rest.is_empty() || rest.starts_with('.'))
}

/// A package's files with the table of names they can refer to.
pub struct Project<'a> {
    /// The package's files, then the context files.
    files: Vec<FileIndex<'a>>,
    /// How many of the files are the package's.
    package: usize,
    table: NameTable<'a>,
    atoms: Atoms,
    notations: NotationTable<'a>,
    /// The structures of all files by full name.
    structures: HashMap<&'a str, &'a StructureInfo>,
    /// The scope before the first command of a file.
    no_scope: Scope<'a>,
}

/// Where in a file something is resolved, with what is in scope there.
struct Place<'p, 'a> {
    file: u32,
    offset: u32,
    /// The namespace of the command.
    namespace: &'a str,
    scope: &'p Scope<'a>,
    /// The namespaces searched first, innermost first.
    namespaces: Vec<&'a str>,
}

/// What was found before for the declarations of a file, kept to answer again.
#[derive(Default)]
struct Memo<'a> {
    /// The type heads of declarations, by name and by how it is used.
    heads: HashMap<(&'a str, Used), Option<Head<'a>>>,
    /// The structures a structure extends.
    parents: HashMap<&'a str, Vec<&'a str>>,
}

/// Answers the walker's questions for one statement.
struct Site<'p, 'a> {
    project: &'p Project<'a>,
    /// The notations to look atoms up in.
    notations: &'p NotationTable<'a>,
    place: &'p Place<'p, 'a>,
    names: Use<'p, 'a>,
    /// The namespaces whose scoped notations the open commands make usable.
    activated: &'p [String],
    /// Earlier answers; `None` where the type of another declaration is being read, which
    /// does not follow further declarations.
    memo: Option<&'p RefCell<Memo<'a>>>,
}

impl<'a> Site<'_, 'a> {
    /// Whether a notation can be used here.
    fn is_active(&self, notation: &Notation<'a>) -> bool {
        let file = self.place.file;
        let is_visible = match notation.file == file {
            true => notation.offset < self.place.offset,
            false => self.project.table.sees(file, notation.file),
        };
        is_visible
            && match &notation.reach {
                Reach::Everywhere => true,
                Reach::Local { end } => notation.file == file && self.place.offset < *end,
                Reach::Scoped(namespace) => {
                    is_within(self.place.namespace, namespace) || self.activated.contains(namespace)
                }
            }
    }
}

impl<'a> Lookup<'a> for Site<'_, 'a> {
    fn resolve<'i>(&self, identifier: &'i str) -> Option<Resolved<'a, 'i>> {
        let found = self.project.table.resolve_prefix(identifier, &self.names);
        found.map(|(name, fields)| Resolved { name, fields })
    }

    fn notation(&self, atom: u32, before: Before, rest: &[Item<'_>]) -> Option<Fired<'_, 'a>> {
        let is_active = |notation: &Notation<'a>| self.is_active(notation);
        self.notations.fire(atom, before, rest, &is_active)
    }

    fn is_closer(&self, atom: u32) -> bool {
        self.notations.is_closer(atom)
    }

    fn field(&self, head: Head<'a>, field: &str) -> Option<&'a str> {
        let owner = match head {
            Head::Constant(name) => name,
            Head::Function => "Function",
        };
        self.project
            .field(owner, field, &self.names, self.memo, MAX_PARENT_DEPTH)
    }

    fn result_head(&self, name: &'a str, used: Used) -> Option<Head<'a>> {
        let memo = self.memo?;
        if let Some(known) = memo.borrow().heads.get(&(name, used)) {
            return *known;
        }
        let head = self.project.result_head(name, used);
        memo.borrow_mut().heads.insert((name, used), head);
        head
    }
}

impl<'a> Project<'a> {
    /// Reads the scopes of every file, in parallel, and builds the shared name table.
    ///
    /// `known_names` are names declared outside the package that its signatures can refer
    /// to. `include_approximate` also takes names that a naming rule derived, which are
    /// guesses.
    pub fn new<S: AsRef<str> + Sync>(
        files: &[SourceFile<'a>],
        known_names: &'a [S],
        include_approximate: bool,
    ) -> Self {
        Project::with_context(files, &[], known_names, include_approximate)
    }

    /// Like [`Project::new`], with `context`: source files outside the package, such as
    /// those of Lean itself and of the package's dependencies. Their notations and
    /// declarations can be used in the modules that import them, but they get no
    /// references themselves.
    ///
    /// An import is matched to a context file by the end of the file's module name, so
    /// that name can be the file's whole path with dots: `import Init.Core` finds
    /// `toolchain.src.lean.Init.Core`. Every file imports `Init` unless it starts with
    /// `prelude`. A name in `known_names` is visible everywhere.
    pub fn with_context<S: AsRef<str> + Sync>(
        files: &[SourceFile<'a>],
        context: &[SourceFile<'a>],
        known_names: &'a [S],
        include_approximate: bool,
    ) -> Self {
        let all: Vec<SourceFile<'a>> = files.iter().chain(context).copied().collect();
        let written: Vec<Vec<Written>> = all
            .par_iter()
            .map(|file| read_notations(file.source, &file.parsed.commands))
            .collect();
        let mut atoms = Atoms::default();
        let patterns = written.iter().flatten().flat_map(|w| w.patterns.iter());
        for part in patterns.flatten() {
            if let Part::Atom(atom) = part {
                atoms.intern(atom);
            }
        }
        atoms.finish();
        let indexed: Vec<FileIndex<'a>> = all
            .par_iter()
            .zip(written)
            .map(|(file, written)| FileIndex::new(*file, include_approximate, &atoms, written))
            .collect();
        let table = name_table(&indexed, files.len(), known_names);
        let mut structures = HashMap::new();
        for file in &all {
            for (name, info) in &file.parsed.structures {
                structures.entry(name.as_str()).or_insert(info);
            }
        }
        let mut project = Project {
            files: indexed,
            package: files.len(),
            table,
            notations: NotationTable::new(atoms.len()),
            atoms,
            structures,
            no_scope: Scope::default(),
        };
        // An expansion can use other notations, as `a ∉ b => ¬ (a ∈ b)` does, so the
        // constants are read a second time with the notations of the first reading.
        for _ in 0..2 {
            project.notations = project.expand_notations();
        }
        project
    }

    /// Resolves the constants of every notation with the notations found so far.
    fn expand_notations(&self) -> NotationTable<'a> {
        let expanded: Vec<Vec<(u32, Option<u32>, Notation<'a>)>> = (0..self.files.len())
            .into_par_iter()
            .map(|index| self.expand_file(index as u32))
            .collect();
        let mut table = NotationTable::new(self.atoms.len());
        for (atom, last, notation) in expanded.into_iter().flatten() {
            table.add(atom, last, notation);
        }
        table
    }

    /// The notations of one file with their constants: per pattern its first atom, the
    /// atom it ends with and the notation.
    fn expand_file(&self, index: u32) -> Vec<(u32, Option<u32>, Notation<'a>)> {
        let file = &self.files[index as usize];
        let mut buffer = Vec::new();
        let mut expanded = Vec::new();
        for written in &file.notations {
            let mut place = self.place(index, written.offset, None);
            if let Some(namespace) = &written.namespace {
                // `scoped[NS]` resolves the expansion inside `NS`.
                let names = self.table.namespace(namespace);
                place.namespaces = names.into_iter().chain(place.namespaces).collect();
            }
            let opened = self.table.open_namespaces(&place.scope.opened);
            let activated = self.activated(&self.notations, &place.scope.opened);
            let mut site = self.site(&place, &opened, &activated, None);
            // A quotation is resolved where the notation is used, so it can name what is
            // declared after it.
            site.names.position = None;
            let mut constants: Vec<&'a str> = Vec::new();
            let mut head = None;
            let mut is_first = true;
            for span in &written.expansions {
                let text = slice(file.file.source, *span);
                let items = nest(tokenize_in(text, &mut buffer, &self.atoms));
                let (found, applied) =
                    expansion_references(&items, &written.bound, place.scope, &site);
                head = applied.filter(|_| written.expansions.len() == 1);
                if written.is_macro && found.is_empty() {
                    continue;
                }
                // Of the quotations of a macro one is the result, so only the constants
                // that all of them have are sure.
                if written.is_macro && !is_first {
                    constants.retain(|constant| found.contains(constant));
                } else {
                    constants.extend(found);
                }
                is_first = false;
            }
            let named = written.named.iter().filter_map(|name| site.resolve(name));
            constants.extend(named.map(|resolved| resolved.name));
            let mut seen = std::collections::HashSet::new();
            constants.retain(|constant| seen.insert(*constant));
            for pattern in &written.patterns {
                let mut numbers = pattern.iter().filter_map(|part| match part {
                    Part::Atom(atom) => self.atoms.number(atom),
                    Part::Hole => None,
                });
                let Some(first) = numbers.next() else {
                    continue;
                };
                let later_atoms = numbers
                    .map(|number| self.atoms.core(number).to_string())
                    .collect();
                let last = match pattern.last() {
                    Some(Part::Atom(atom)) => self.atoms.number(atom),
                    _ => None,
                };
                let notation = Notation {
                    file: index,
                    offset: written.offset,
                    reach: written.reach.clone(),
                    leading: matches!(pattern.first(), Some(Part::Atom(_))),
                    later_atoms,
                    precedence: written.precedence,
                    priority: written.priority,
                    is_opaque: written.is_opaque,
                    hides_binders: written.hides_binders,
                    after_binder: written.after_binder,
                    binds: written.binds,
                    constants: constants.clone(),
                    head,
                };
                expanded.push((first, last, notation));
            }
        }
        expanded
    }

    /// The place of the command at a byte offset of a file. With a declaration, the
    /// namespaces are those its signature is resolved in.
    fn place(&self, file: u32, offset: u32, declaration: Option<&'a Decl>) -> Place<'_, 'a> {
        let index = &self.files[file as usize];
        let parsed: &'a Parsed = index.file.parsed;
        let at = index.scopes.at(offset);
        let namespace = at.map_or("", |(index, _)| parsed.namespaces[index as usize].as_str());
        let scope = match at {
            Some((_, scope)) => &index.scopes.scopes[scope as usize],
            None => &self.no_scope,
        };
        let namespaces = match declaration {
            Some(declaration) => lookup_namespaces(declaration, namespace, &parsed.decls),
            None if namespace.is_empty() => Vec::new(),
            None => std::iter::once(namespace)
                .chain(enclosing_namespaces(namespace))
                .collect(),
        };
        Place {
            file,
            offset,
            namespace,
            scope,
            namespaces,
        }
    }

    /// The answers for a statement at a place.
    fn site<'p>(
        &'p self,
        place: &'p Place<'p, 'a>,
        opened: &'p [BoundOpen],
        activated: &'p [String],
        memo: Option<&'p RefCell<Memo<'a>>>,
    ) -> Site<'p, 'a> {
        let position = Position {
            module: place.file,
            offset: place.offset,
        };
        Site {
            project: self,
            notations: &self.notations,
            place,
            names: Use {
                opened,
                position: Some(position),
                mutual_start: place.scope.mutual_start,
                namespaces: &place.namespaces,
                siblings: None,
            },
            activated,
            memo,
        }
    }

    /// The namespaces whose scoped notations the open commands in effect make usable.
    ///
    /// A written namespace stands for its first match, innermost first, and for the ones
    /// inside namespaces that an earlier `open` opened.
    fn activated(
        &self,
        notations: &NotationTable<'a>,
        opened: &[Arc<OpenDeclaration>],
    ) -> Vec<String> {
        let mut activated = Vec::new();
        if notations.namespaces.is_empty() {
            return activated;
        }
        let exists =
            |name: &str| notations.namespaces.contains(name) || self.table.is_namespace(name);
        let mut simple: Vec<String> = Vec::new();
        for declaration in opened {
            let Some(written) = declaration.candidates.last() else {
                continue;
            };
            let lexical = declaration.candidates.iter().find(|name| exists(name));
            let inside = simple
                .iter()
                .map(|namespace| format!("{namespace}.{written}"));
            let found: Vec<String> = lexical
                .cloned()
                .into_iter()
                .chain(inside.filter(|name| exists(name)))
                .collect();
            for namespace in found {
                if declaration.activates() && notations.namespaces.contains(&namespace) {
                    activated.push(namespace.clone());
                }
                if declaration.is_simple() {
                    simple.push(namespace);
                }
            }
        }
        activated
    }

    /// The declaration that `.field` refers to on a term whose type is an application of
    /// `owner`: `owner.field`, or the same in a structure that `owner` extends.
    fn field(
        &self,
        owner: &str,
        field: &str,
        names: &Use<'_, 'a>,
        memo: Option<&RefCell<Memo<'a>>>,
        depth: usize,
    ) -> Option<&'a str> {
        let candidate = format!("{owner}.{field}");
        if let Some(name) = self.table.visible_name(&candidate, names) {
            return Some(name);
        }
        let owner: &'a str = self.structures.get_key_value(owner)?.0;
        let known = memo.and_then(|memo| memo.borrow().parents.get(owner).cloned());
        let parents = known.unwrap_or_else(|| {
            let parents = self.parents(owner);
            if let Some(memo) = memo {
                memo.borrow_mut().parents.insert(owner, parents.clone());
            }
            parents
        });
        let depth = depth.checked_sub(1)?;
        parents
            .iter()
            .find_map(|parent| self.field(parent, field, names, memo, depth))
    }

    /// The structures that a structure extends, resolved where it is declared.
    fn parents(&self, structure: &str) -> Vec<&'a str> {
        let Some(info) = self.structures.get(structure) else {
            return Vec::new();
        };
        let source = self
            .table
            .get(structure)
            .and_then(|(_, entry)| entry.source);
        let Some((file, number)) = source else {
            return Vec::new();
        };
        let declaration = &self.files[file as usize].file.parsed.decls[number as usize];
        let place = self.place(file, declaration.range.start, Some(declaration));
        let opened = self.table.open_namespaces(&place.scope.opened);
        let site = self.site(&place, &opened, &[], None);
        let resolve = |written: &String| {
            let resolved = site.resolve(written)?;
            resolved.fields.is_empty().then_some(resolved.name)
        };
        info.parents.iter().filter_map(resolve).collect()
    }

    /// The head of the type of a declaration as it is used, read from its signature where
    /// it is written: a function type as long as explicit arguments are missing.
    fn result_head(&self, name: &str, used: Used) -> Option<Head<'a>> {
        let (file, number) = self.table.get(name)?.1.source?;
        let index = &self.files[file as usize];
        let parsed: &'a Parsed = index.file.parsed;
        let declaration = parsed.decls.get(number as usize)?;
        let mut buffer = Vec::new();
        let text = slice(index.file.source, declaration.signature);
        let signature = nest(tokenize_in(text, &mut buffer, &self.atoms));
        let (arity, result) = result_type(&signature, name, declaration.kind)?;
        // A field written without an explicit argument takes the term from a section
        // variable, which the signature does not show.
        let given = match used {
            Used::Alone => 0,
            Used::OnTerm => 1,
            Used::Applied => arity,
        };
        if arity > given {
            return Some(Head::Function);
        }
        let result = result?;
        let mut bound = signature_binder_names(&signature, name);
        let owner = declaration
            .parent
            .and_then(|parent| parsed.decls.get(parent as usize));
        if let Some(owner) = owner {
            let text = slice(index.file.source, owner.signature);
            let signature = nest(tokenize_in(text, &mut buffer, &self.atoms));
            bound.extend(signature_binder_names(
                &signature,
                owner.name.as_deref().unwrap_or(""),
            ));
        }
        let place = self.place(file, declaration.range.start, Some(declaration));
        let opened = self.table.open_namespaces(&place.scope.opened);
        let activated = self.activated(&self.notations, &place.scope.opened);
        let site = self.site(&place, &opened, &activated, None);
        Walker::new(&site, place.scope, &bound).type_head(result)
    }

    /// The names every declaration of every package file refers to, resolved in parallel.
    pub fn references(&self) -> Vec<Vec<Vec<&'a str>>> {
        let names = |found: References<'a>| found.names;
        self.detailed_references()
            .into_iter()
            .map(|file| file.into_iter().map(names).collect())
            .collect()
    }

    /// The references of every declaration of every package file, resolved in parallel,
    /// each with the part of the signature it is written in.
    pub fn detailed_references(&self) -> Vec<Vec<References<'a>>> {
        (0..self.package)
            .into_par_iter()
            .map(|index| self.file_references(index))
            .collect()
    }

    /// The references of every declaration of the package file at `index`, in order of
    /// first appearance. A declaration without a usable name has none.
    pub fn file_references(&self, index: usize) -> Vec<References<'a>> {
        let file = &self.files[index];
        let parsed: &'a Parsed = file.file.parsed;
        let declarations = &parsed.decls;
        let mut resolver = FileResolver {
            project: self,
            module: index as u32,
            file,
            owners: HashMap::new(),
            opened: vec![None; file.scopes.scopes.len()],
            activated: vec![None; file.scopes.scopes.len()],
            memo: RefCell::new(Memo::default()),
            buffer: Vec::new(),
        };
        for declaration in declarations {
            if let (DeclKind::Field, Some(parent), Some(name)) = (
                declaration.kind,
                declaration.parent,
                declaration.name.as_deref(),
            ) {
                let short = name.rsplit('.').next().unwrap_or(name);
                let owner = resolver.owners.entry(parent).or_default();
                if !name.is_empty() {
                    owner.siblings.insert(short, name);
                }
            }
        }
        let named = declarations.iter().zip(&file.named);
        named
            .map(|(declaration, named)| match named {
                true => resolver.references(declaration),
                false => References::default(),
            })
            .collect()
    }
}

/// State of resolving the declarations of one file.
struct FileResolver<'p, 'a> {
    project: &'p Project<'a>,
    module: u32,
    file: &'p FileIndex<'a>,
    owners: HashMap<u32, Owner<'a>>,
    /// Per scope of the file, the namespaces its open declarations open.
    opened: Vec<Option<Vec<BoundOpen>>>,
    /// Per scope of the file, the namespaces whose scoped notations are usable.
    activated: Vec<Option<Vec<String>>>,
    memo: RefCell<Memo<'a>>,
    buffer: Vec<crate::lexer::Token>,
}

impl<'a> FileResolver<'_, 'a> {
    /// The names bound by the binders of the declaration that owns a field or constructor.
    fn owner_bound(&mut self, parent: u32) -> Vec<String> {
        let source = self.file.file.source;
        let Some(owner) = self.file.file.parsed.decls.get(parent as usize) else {
            return Vec::new();
        };
        let entry = self.owners.entry(parent).or_default();
        if entry.bound.is_none() {
            let signature = nest(tokenize_in(
                slice(source, owner.signature),
                &mut self.buffer,
                &self.project.atoms,
            ));
            let name = owner.name.as_deref().unwrap_or("");
            entry.bound = Some(signature_binder_names(&signature, name));
        }
        entry.bound.clone().unwrap_or_default()
    }

    fn references(&mut self, declaration: &'a Decl) -> References<'a> {
        let project = self.project;
        let table = &project.table;
        let scopes = &self.file.scopes;
        let parsed: &'a Parsed = self.file.file.parsed;
        let source = self.file.file.source;
        let name = declaration.name.as_deref().unwrap_or("");
        let bound = match declaration.parent {
            Some(parent) => self.owner_bound(parent),
            None => Vec::new(),
        };
        let place = project.place(self.module, declaration.range.start, Some(declaration));
        let scope = place.scope;
        let (opened, activated) = match scopes.at(declaration.range.start) {
            Some((_, index)) => {
                let opened = self.opened[index as usize]
                    .get_or_insert_with(|| table.open_namespaces(&scope.opened));
                let activated = self.activated[index as usize]
                    .get_or_insert_with(|| project.activated(&project.notations, &scope.opened));
                (opened.as_slice(), activated.as_slice())
            }
            None => (&[][..], &[][..]),
        };
        let mut site = project.site(&place, opened, activated, Some(&self.memo));
        site.names.siblings = match (declaration.kind, declaration.parent) {
            (DeclKind::Field, Some(parent)) => self.owners.get(&parent).map(|o| &o.siblings),
            _ => None,
        };
        let atoms = &project.atoms;
        let signature = nest(tokenize_in(
            slice(source, declaration.signature),
            &mut self.buffer,
            atoms,
        ));
        let body = match reads_body(declaration.kind, scope) {
            true => tokenize_in(slice(source, declaration.range), &mut self.buffer, atoms),
            false => Vec::new(),
        };
        let statement = Statement {
            kind: declaration.kind,
            name,
            signature: &signature,
            bound: &bound,
            scope,
            body: &body,
        };
        let mut found = find_references(&statement, &site);
        // A constructor written without a result type, such as `| nil`, still has its
        // inductive type as its type.
        let owner = match declaration.kind {
            DeclKind::Constructor => declaration.parent,
            _ => None,
        };
        let owner = owner
            .and_then(|parent| parsed.decls.get(parent as usize)?.name.as_deref())
            .and_then(|name| table.get(name));
        if let Some((owner, _)) = owner
            && !found.names.contains(&owner)
        {
            found.names.push(owner);
            found.in_type.push(true);
        }
        found
    }
}

/// Resolves the references of every declaration of a package.
///
/// `files` are all files of the package, in the order that decides which declaration of a
/// name declared twice counts. `known_names` are names declared outside the package that its
/// signatures can refer to. With `include_approximate`, names that a naming rule derived can
/// be referred to as well.
///
/// Returns, per file and per declaration of `Parsed::decls`, the full names it refers to in
/// order of first appearance. The names are those of declarations of the package or of
/// `known_names`; the declaration's own name can be among them. Files are resolved in
/// parallel. [`Project::with_context`] also reads notations and declarations from files
/// outside the package.
pub fn resolve_references<'a, S: AsRef<str> + Sync>(
    files: &[SourceFile<'a>],
    known_names: &'a [S],
    include_approximate: bool,
) -> Vec<Vec<Vec<&'a str>>> {
    Project::new(files, known_names, include_approximate).references()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn visible_modules_are_the_import_closure() {
        let visible = visible_modules(&[vec![], vec![0], vec![], vec![1]]);
        assert_eq!(
            visible,
            [vec![0b0001], vec![0b0011], vec![0b0100], vec![0b1011]]
        );
    }

    #[test]
    fn lookup_namespaces_follow_the_name_and_the_namespace() {
        let parsed = crate::parse(
            b"namespace A\ndef Foo.bar := 1\ndef _root_.Foo.bar := 1\nend A\ndef Foo.baz := 1\n",
        );
        let found: Vec<Vec<&str>> = ["A", "A", ""]
            .iter()
            .zip(&parsed.decls)
            .map(|(namespace, declaration)| {
                lookup_namespaces(declaration, namespace, &parsed.decls)
            })
            .collect();
        assert_eq!(found, [vec!["A.Foo", "A"], vec!["A"], vec!["Foo"]]);
    }
}
