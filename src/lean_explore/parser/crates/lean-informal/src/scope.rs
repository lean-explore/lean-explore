//! The scope in effect at each declaration of a file.
//!
//! A statement such as `theorem mul_comm' : a * b = b * a` means nothing without the
//! `variable {G : Type*} [CommGroup G] (a b : G)` line above it. The scope is kept as Lean
//! keeps it: a stack with one frame per open `namespace` or `section`, each holding the
//! `open`, `variable`, `universe`, `include`, `omit` and notation commands written in it, and
//! `end` drops a frame with everything it held. A command that ends in `in` applies only to
//! the next command.

use lean_source::{CommandKind, DeclKind, Parsed, Span};

/// A top-level declaration with the scope it is read in.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Unit {
    /// Index of the declaration in `Parsed::decls`.
    pub decl: usize,
    /// Every scope command in effect, outermost first, as a request that starts with this
    /// declaration shows them. `in` prefixes are not included.
    pub scope: Vec<String>,
    /// The scope commands written since the previous unit, in order, `in` prefixes included:
    /// what a request that already shows the previous unit adds before this one.
    pub between: Vec<String>,
    /// Commands ending in `in` written directly before the declaration.
    pub prefix: Vec<String>,
}

#[derive(Default)]
struct Frame {
    header: Option<String>,
    lines: Vec<String>,
}

#[derive(Default)]
struct Walk {
    stack: Vec<Frame>,
    between: Vec<String>,
    prefix: Vec<String>,
}

impl Walk {
    fn scope(&self) -> Vec<String> {
        let mut lines = Vec::new();
        for frame in &self.stack {
            lines.extend(frame.header.iter().cloned());
            lines.extend(frame.lines.iter().cloned());
        }
        lines
    }

    /// Records a command that changes the scope, or only the next command if it ends in `in`.
    fn scope_line(&mut self, text: &str) {
        if ends_with_in(text) {
            self.prefix.push(text.to_string());
        } else {
            self.stack
                .last_mut()
                .expect("the root frame is never dropped")
                .lines
                .push(text.to_string());
            self.between.push(text.to_string());
        }
    }

    fn open_frame(&mut self, text: &str) {
        self.between.push(text.to_string());
        self.stack.push(Frame {
            header: Some(text.to_string()),
            lines: Vec::new(),
        });
    }

    fn close_frame(&mut self, text: &str) {
        self.between.push(text.to_string());
        if self.stack.len() > 1 {
            self.stack.pop();
        }
    }

    fn unit(&mut self, decl: usize) -> Unit {
        let prefix = std::mem::take(&mut self.prefix);
        let mut between = std::mem::take(&mut self.between);
        between.extend(prefix.iter().cloned());
        Unit {
            decl,
            scope: self.scope(),
            between,
            prefix,
        }
    }
}

/// The source text of a span, or "" if it is not UTF-8.
pub fn text(src: &[u8], span: Span) -> &str {
    std::str::from_utf8(&src[span.start as usize..span.end as usize]).unwrap_or("")
}

/// True if a command ends with the keyword `in`, as in `open Real in`.
fn ends_with_in(text: &str) -> bool {
    let trimmed = text.trim_end();
    trimmed.ends_with(" in") || trimmed.ends_with("\nin") || trimmed == "in"
}

/// Commands of kind `Other` that change which variables a statement includes.
fn is_variable_command(head: &str) -> bool {
    matches!(head, "include" | "omit")
}

/// Declaration commands that introduce notation later statements are written in.
fn is_notation_command(head: &str) -> bool {
    matches!(
        head,
        "notation" | "infix" | "infixl" | "infixr" | "prefix" | "postfix"
    )
}

/// Start offsets of the top-level declarations with their indices, sorted by start.
fn top_level_starts(parsed: &Parsed) -> Vec<(u32, usize)> {
    let mut starts: Vec<(u32, usize)> = parsed
        .decls
        .iter()
        .enumerate()
        .filter(|(_, decl)| decl.parent.is_none())
        .map(|(index, decl)| (decl.range.start, index))
        .collect();
    starts.sort_unstable();
    starts
}

/// Index of the first top-level declaration that starts inside `range`, if any.
fn top_level_at(starts: &[(u32, usize)], range: Span) -> Option<usize> {
    let first = starts.partition_point(|(start, _)| *start < range.start);
    starts
        .get(first)
        .filter(|(start, _)| *start < range.end)
        .map(|(_, index)| *index)
}

/// The top-level declarations for which `wanted` holds, each with its scope.
///
/// Declarations that are not wanted are left out, but the scope commands written before them
/// are carried to the next wanted declaration.
pub fn units(src: &[u8], parsed: &Parsed, wanted: impl Fn(usize) -> bool) -> Vec<Unit> {
    let mut walk = Walk {
        stack: vec![Frame::default()],
        ..Walk::default()
    };
    let starts = top_level_starts(parsed);
    let mut out = Vec::new();
    for command in &parsed.commands {
        let command_text = text(src, command.range).trim_end();
        match command.kind {
            CommandKind::Namespace | CommandKind::Section | CommandKind::Mutual => {
                walk.open_frame(command_text)
            }
            CommandKind::End => walk.close_frame(command_text),
            CommandKind::Open | CommandKind::Variable | CommandKind::Universe => {
                walk.scope_line(command_text)
            }
            CommandKind::Other if is_variable_command(text(src, command.head)) => {
                walk.scope_line(command_text)
            }
            // `set_option ... in` sits between an `open ... in` and its declaration.
            CommandKind::SetOption if ends_with_in(command_text) => {}
            CommandKind::Declaration if is_notation_command(text(src, command.head)) => {
                walk.scope_line(command_text)
            }
            CommandKind::Declaration => match top_level_at(&starts, command.range) {
                Some(index) if parsed.decls[index].kind == DeclKind::Notation => {
                    walk.scope_line(command_text)
                }
                Some(index) if wanted(index) => out.push(walk.unit(index)),
                _ => walk.prefix.clear(),
            },
            _ => walk.prefix.clear(),
        }
    }
    out
}
