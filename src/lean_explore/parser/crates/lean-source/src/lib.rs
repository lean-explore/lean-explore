//! Build-free parser for Lean 4 source files.
//!
//! [`parse`] finds every command and declaration of a file and reports names, modifiers and
//! byte spans without elaborating anything. Positions convert to Lean's line/column
//! convention with [`pos::LineIndex`]. [`refs::resolve_references`] finds the declarations
//! that the signatures of a package refer to.

pub mod chars;
pub mod json;
pub mod keywords;
pub mod lexer;
pub mod pos;
pub mod refs;
pub mod scan;
pub mod translate;
mod translate_tables;

pub use scan::{Attr, Command, CommandKind, Decl, DeclKind, Parsed, Span, parse, parse_with};
pub use scan::{Context, parse_in};
