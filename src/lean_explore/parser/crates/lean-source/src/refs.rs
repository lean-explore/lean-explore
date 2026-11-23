//! The declarations that each declaration of a package refers to.
//!
//! [`resolve_references`] reads the identifiers written in every signature and resolves them
//! by Lean's lookup rules, without elaborating anything: against the namespaces of the
//! declaration, the root namespace, the namespaces opened at that point of the file and the
//! names exported there. A name is visible only after it is declared, inside its `mutual`
//! block, or from a module that imports the one declaring it.
//!
//! Every resolved name is reported, including names a caller may not keep, such as private
//! declarations, structure fields and `.mk` constructors: they shadow other candidates, so
//! leaving them out here would resolve the identifier to the wrong declaration.

pub mod names;
pub mod scopes;
pub mod tokens;
pub mod walk;

use crate::keywords::Modifier;
use crate::scan::{CommandKind, Decl, DeclKind, Parsed, Span};
use names::{BoundOpen, NameTable, Position, Use, enclosing_namespaces};
use rayon::prelude::*;
use scopes::{FileScopes, Scope};
use std::collections::HashMap;
use tokens::{nest, tokenize_with};
use walk::{Statement, find_references, reads_body, signature_binder_names};

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
}

impl<'a> FileIndex<'a> {
    fn new(file: SourceFile<'a>, include_approximate: bool) -> Self {
        let mut buffer = Vec::new();
        let commands = file.parsed.commands.iter();
        let imports = commands
            .filter(|command| command.kind == CommandKind::Import)
            .filter_map(|command| {
                let tokens = tokenize_with(slice(file.source, command.range), &mut buffer);
                tokens.last().map(|token| token.text.to_string())
            })
            .collect();
        let is_named = |declaration: &Decl| {
            let has_name = declaration.name.as_deref().is_some_and(|n| !n.is_empty());
            has_name && (include_approximate || !declaration.approximate)
        };
        FileIndex {
            file,
            scopes: FileScopes::new(file.source, file.parsed),
            imports,
            named: file.parsed.decls.iter().map(is_named).collect(),
        }
    }

    /// The declarations that others can refer to, with their names.
    fn named_declarations(&self) -> impl Iterator<Item = (&'a str, &'a Decl)> {
        let declarations = self.file.parsed.decls.iter().zip(&self.named);
        declarations
            .filter(|(_, named)| **named)
            .filter_map(|(declaration, _)| Some((declaration.name.as_deref()?, declaration)))
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

/// Builds the table of names the package's signatures can refer to.
fn name_table<'a, S: AsRef<str>>(files: &[FileIndex<'a>], known_names: &'a [S]) -> NameTable<'a> {
    let mut table = NameTable::default();
    let mut indexes = HashMap::new();
    for (index, file) in files.iter().enumerate() {
        indexes.insert(file.file.module, index);
        for (alias, declaration) in &file.scopes.aliases {
            table.aliases.insert(alias.clone(), declaration.clone());
        }
        for (name, declaration) in file.named_declarations() {
            let is_private = declaration.modifiers & Modifier::Private.bit() != 0;
            let entry = table.declare(name);
            match entry.position {
                // A name declared twice keeps its first declaration, and is private only if
                // every declaration of it is.
                Some(_) => entry.is_private &= is_private,
                None => {
                    entry.is_private = is_private;
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
        table.declare(name.as_ref()).is_private = false;
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

/// A package's files with the table of names they can refer to.
pub struct Project<'a> {
    files: Vec<FileIndex<'a>>,
    table: NameTable<'a>,
    /// The scope before the first command of a file.
    no_scope: Scope<'a>,
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
        let files: Vec<FileIndex<'a>> = files
            .par_iter()
            .map(|file| FileIndex::new(*file, include_approximate))
            .collect();
        let table = name_table(&files, known_names);
        Project {
            files,
            table,
            no_scope: Scope::default(),
        }
    }

    /// The references of every declaration of every file, resolved in parallel.
    pub fn references(&self) -> Vec<Vec<Vec<&'a str>>> {
        (0..self.files.len())
            .into_par_iter()
            .map(|index| self.file_references(index))
            .collect()
    }

    /// The references of every declaration of the file at `index`, in order of first
    /// appearance. A declaration without a usable name has none.
    pub fn file_references(&self, index: usize) -> Vec<Vec<&'a str>> {
        let file = &self.files[index];
        let parsed: &'a Parsed = file.file.parsed;
        let declarations = &parsed.decls;
        let mut resolver = FileResolver {
            project: self,
            module: index as u32,
            file,
            owners: HashMap::new(),
            opened: vec![None; file.scopes.scopes.len()],
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
                false => Vec::new(),
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
            let signature = nest(tokenize_with(
                slice(source, owner.signature),
                &mut self.buffer,
            ));
            let name = owner.name.as_deref().unwrap_or("");
            entry.bound = Some(signature_binder_names(&signature, name));
        }
        entry.bound.clone().unwrap_or_default()
    }

    fn references(&mut self, declaration: &'a Decl) -> Vec<&'a str> {
        let project = self.project;
        let table = &project.table;
        let scopes = &self.file.scopes;
        let parsed: &'a Parsed = self.file.file.parsed;
        let source = self.file.file.source;
        let name = declaration.name.as_deref().unwrap_or("");
        let at = scopes.at(declaration.range.start);
        let namespace = at.map_or("", |(index, _)| parsed.namespaces[index as usize].as_str());
        let bound = match declaration.parent {
            Some(parent) => self.owner_bound(parent),
            None => Vec::new(),
        };
        let (scope, opened) = match at {
            Some((_, index)) => {
                let scope = &scopes.scopes[index as usize];
                let opened = self.opened[index as usize]
                    .get_or_insert_with(|| table.open_namespaces(&scope.opened));
                (scope, opened.as_slice())
            }
            None => (&project.no_scope, &[][..]),
        };
        let siblings = match (declaration.kind, declaration.parent) {
            (DeclKind::Field, Some(parent)) => self.owners.get(&parent).map(|o| &o.siblings),
            _ => None,
        };
        let namespaces = lookup_namespaces(declaration, namespace, &parsed.decls);
        let place = Use {
            opened,
            position: Some(Position {
                module: self.module,
                offset: declaration.range.start,
            }),
            mutual_start: scope.mutual_start,
            namespaces: &namespaces,
            siblings,
        };
        let signature = nest(tokenize_with(
            slice(source, declaration.signature),
            &mut self.buffer,
        ));
        let body = match reads_body(declaration.kind, scope) {
            true => tokenize_with(slice(source, declaration.range), &mut self.buffer),
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
        let mut found =
            find_references(&statement, &|identifier| table.resolve(identifier, &place));
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
            && !found.contains(&owner)
        {
            found.push(owner);
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
/// parallel.
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
