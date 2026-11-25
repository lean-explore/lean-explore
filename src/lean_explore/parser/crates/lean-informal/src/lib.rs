//! Per-file informalization planning.
//!
//! The current pipeline asks a language model for one description per declaration and sends
//! the same instructions with every request. This crate turns a Lean file into a few
//! requests instead: each holds the scope the file sets up (`namespace`, `section`, `open`,
//! `variable`, local notation), the statements of a run of declarations without their proofs,
//! and the descriptions of the dependencies those statements use most. Declarations whose
//! description follows from another one are not sent at all: an `alias` takes its target's
//! description, and a `reassoc` or `elementwise` lemma takes its parent's with one fixed
//! sentence added.
//!
//! [`plan::plan_file`] builds the requests of one file, and [`assemble::assemble`] checks the
//! model's answers against the names each request asked for.

pub mod assemble;
pub mod context;
pub mod input;
pub mod plan;
pub mod scope;

/// Rough token count of a text for budgeting: about 3.2 characters per token, which is what
/// Lean statements and English descriptions average with common subword tokenizers.
pub fn estimate_tokens(text: &str) -> usize {
    (text.chars().count() * 10).div_ceil(32)
}
