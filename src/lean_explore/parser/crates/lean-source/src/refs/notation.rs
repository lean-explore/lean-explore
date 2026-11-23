//! Notations declared in source: which constants a token stands for.
//!
//! Lean elaborates `a + b` to `HAdd.hAdd a b` because a command such as
//! `infixl:65 " + " => HAdd.hAdd` says so. This module reads those commands (`notation`,
//! `notation3`, `infix`, `infixl`, `infixr`, `prefix` and `postfix`) without expanding
//! anything: the literal tokens of the pattern, called atoms, and the text after `=>`, whose
//! constants are resolved where the notation is declared.
//!
//! Term syntax declared in the general way is read as far as it is written down: a
//! `macro_rules` alternative or a `macro` gives a pattern and, as its expansion, the syntax
//! quotations on its right-hand side. A `syntax` or `elab` command gives a pattern only; what
//! it elaborates to is code, so the token is known to stand for something unknown.
//!
//! A notation is found again by its first atom. Where the atom is written, the notations
//! that are active there are the candidates: a plain notation from the place it is declared
//! on and in modules importing it, a `local` one to the end of its section, a `scoped` one
//! where its namespace is entered or opened.

use super::tokens::Item;
use crate::chars::decode;
use crate::lexer::{self, Kind};
use crate::scan::{Command, CommandKind, Span};
use std::collections::{HashMap, HashSet};

/// Characters that the token tree reads as brackets.
const BRACKETS: [char; 10] = ['(', ')', '[', ']', '{', '}', '⟨', '⟩', '⦃', '⦄'];
const OPENING_BRACKETS: [char; 5] = ['(', '[', '{', '⟨', '⦃'];

/// Atoms that are part of every term and never stand for a notation.
const STRUCTURAL: [&str; 9] = [",", ":", ":=", "=>", ".", "|", "@", "←", "↦"];

/// The atoms of every notation, numbered, with an index to find the longest one at a
/// position of a source text.
#[derive(Debug, Default)]
pub struct Atoms {
    /// The atoms as written, without surrounding spaces. An atom's number is its index
    /// plus one, so that zero can stand for no atom.
    texts: Vec<String>,
    /// Per atom, the byte range of its core: the atom without a bracket at its start
    /// and without an opening bracket at its end.
    cores: Vec<(u32, u32)>,
    numbers: HashMap<String, u32>,
    /// Atoms that can be read as one token, by first character and then longest first.
    ordered: Vec<u32>,
    /// Per first character, the range of `ordered` with the atoms that start with it.
    starts: Vec<(u32, u32, u32)>,
}

/// The byte range of an atom's core: the atom without a bracket at its start and without
/// an opening bracket at its end, which both stay brackets, as in `C(`, `]→ₗ[` and `[⋀^`.
fn core_range(text: &str) -> (usize, usize) {
    let first = text.chars().next().filter(|c| BRACKETS.contains(c));
    let last = text
        .chars()
        .next_back()
        .filter(|c| OPENING_BRACKETS.contains(c));
    let start = first.map_or(0, char::len_utf8);
    let end = text.len() - last.map_or(0, char::len_utf8);
    match start < end {
        true => (start, end),
        // The atom is a bracket or two, or balanced as `[X]` is: it has no smaller core.
        false => (0, text.len()),
    }
}

/// Whether a core can replace the tokens it is made of: it is not a token that every
/// term can have, and its brackets are balanced.
fn is_readable(core: &str) -> bool {
    if STRUCTURAL.contains(&core) || core.chars().any(char::is_whitespace) {
        return false;
    }
    let mut depth = 0usize;
    for character in core.chars().filter(|c| BRACKETS.contains(c)) {
        if OPENING_BRACKETS.contains(&character) {
            depth += 1;
        } else if depth == 0 {
            return false;
        } else {
            depth -= 1;
        }
    }
    depth == 0 && !core.chars().all(|c| BRACKETS.contains(&c))
}

impl Atoms {
    /// The number of an atom, which is added if it is new.
    pub fn intern(&mut self, text: &str) -> u32 {
        if let Some(number) = self.numbers.get(text) {
            return *number;
        }
        let (mut start, mut end) = core_range(text);
        if !is_readable(&text[start..end]) {
            (start, end) = (0, text.len());
        }
        self.texts.push(text.to_string());
        self.cores.push((start as u32, end as u32));
        let number = self.texts.len() as u32;
        self.numbers.insert(text.to_string(), number);
        number
    }

    /// The number of an atom that was added before.
    pub fn number(&self, text: &str) -> Option<u32> {
        self.numbers.get(text).copied()
    }

    /// Builds the index of the atoms; call it once after the last [`Atoms::intern`].
    pub fn finish(&mut self) {
        let first = |number: &u32| decode(self.core(*number).as_bytes(), 0).0;
        let mut ordered: Vec<u32> = (1..=self.texts.len() as u32)
            .filter(|number| is_readable(self.core(*number)))
            .collect();
        ordered.sort_by_key(|number| {
            let (start, end) = self.cores[*number as usize - 1];
            let length = self.texts[*number as usize - 1].len();
            // Longest core first, and among equal cores the one with more brackets.
            (first(number), u32::MAX - (end - start), usize::MAX - length)
        });
        let mut starts: Vec<(u32, u32, u32)> = Vec::new();
        for (index, number) in ordered.iter().enumerate() {
            let character = first(number);
            match starts.last_mut() {
                Some(last) if last.0 == character => last.2 = index as u32 + 1,
                _ => starts.push((character, index as u32, index as u32 + 1)),
            }
        }
        self.ordered = ordered;
        self.starts = starts;
    }

    /// Whether there is no atom to look for.
    pub fn is_empty(&self) -> bool {
        self.ordered.is_empty()
    }

    /// The atoms whose core is written at byte `at` of `src`, with the atom's brackets
    /// around it, longest first, each with the number of bytes its core takes.
    pub fn matches<'s>(
        &'s self,
        src: &'s [u8],
        at: usize,
    ) -> impl Iterator<Item = (u32, usize)> + 's {
        let range = if self.ordered.is_empty() || at >= src.len() {
            0..0
        } else {
            let character = decode(src, at).0;
            match self
                .starts
                .binary_search_by_key(&character, |start| start.0)
            {
                Ok(index) => self.starts[index].1 as usize..self.starts[index].2 as usize,
                Err(_) => 0..0,
            }
        };
        self.ordered[range].iter().filter_map(move |number| {
            let text = self.texts[*number as usize - 1].as_bytes();
            let (start, end) = self.cores[*number as usize - 1];
            let (start, end) = (start as usize, end as usize);
            let from = at.checked_sub(start)?;
            src[from..]
                .starts_with(text)
                .then_some((*number, end - start))
        })
    }

    /// The atom as a token shows it: without the brackets around its core.
    pub fn core(&self, number: u32) -> &str {
        let (start, end) = self.cores[number as usize - 1];
        &self.texts[number as usize - 1][start as usize..end as usize]
    }

    /// How many atoms there are.
    pub fn len(&self) -> usize {
        self.texts.len()
    }
}

/// Where a notation can be used.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Reach {
    /// Wherever its module is imported.
    Everywhere,
    /// In its file, up to the byte offset where its section ends.
    Local { end: u32 },
    /// Where the namespace is entered or opened.
    Scoped(String),
}

/// One part of a notation's pattern.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Part {
    /// A literal token, without surrounding spaces.
    Atom(String),
    /// A place for a term.
    Hole,
}

/// A notation command as written, before its constants are resolved.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Written {
    /// Byte offset of the command in its file.
    pub offset: u32,
    /// The pattern. `unicode("≤", "<=")` gives one pattern per spelling.
    pub patterns: Vec<Vec<Part>>,
    /// The precedence written after the keyword, as in `infixl:65`.
    pub precedence: Option<u32>,
    /// Names that the pattern binds in the expansion.
    pub bound: Vec<String>,
    /// The parts of the command whose identifiers are the notation's constants.
    pub expansions: Vec<Span>,
    /// Constants that the command names with a quoted name, as ``` ``Exists ```.
    pub named: Vec<String>,
    /// The priority among notations for the same token; the default is 1000.
    pub priority: u32,
    /// Whether what the notation expands to is not written down as terms.
    pub is_opaque: bool,
    /// Whether the expansions are the quotations of a macro, of which one is the result,
    /// and not the parts of one expansion.
    pub is_macro: bool,
    /// Whether the macro works on its binders with code, so that what a binder predicate
    /// such as the `∈ s` of `∑ x ∈ s, f x` stands for is unknown.
    pub hides_binders: bool,
    /// Whether the pattern starts with binders, as that of `(x : α) × β x` does.
    pub after_binder: bool,
    pub reach: Reach,
    /// The namespace of `scoped[NS]`, in which the expansion is resolved.
    pub namespace: Option<String>,
    /// Whether the notation binds names in what follows it, as `∑ x, f x` does.
    pub binds: bool,
}

/// The holes around the atom of the short forms `infix`, `prefix` and `postfix`.
fn short_form(keyword: &[u8]) -> Option<(bool, bool)> {
    match keyword {
        b"infix" | b"infixl" | b"infixr" => Some((true, true)),
        b"prefix" => Some((false, true)),
        b"postfix" => Some((true, false)),
        _ => None,
    }
}

/// The value of a priority written as `(priority := high + 1)`.
fn priority(words: &[&[u8]]) -> u32 {
    let mut total = 0u32;
    for word in words {
        total += match *word {
            b"default" => 1000,
            b"low" => 10,
            b"mid" => 500,
            b"high" => 10000,
            other => std::str::from_utf8(other)
                .ok()
                .and_then(|text| text.parse().ok())
                .unwrap_or(0),
        };
    }
    total
}

/// The value of a precedence written after `:`.
fn precedence(text: &[u8]) -> Option<u32> {
    match text {
        b"max" => Some(1024),
        b"arg" => Some(1023),
        b"lead" => Some(1022),
        b"min" => Some(10),
        b"min1" => Some(11),
        _ => std::str::from_utf8(text).ok()?.parse().ok(),
    }
}

/// The text of a string literal token, without quotes, escapes and surrounding spaces.
fn atom_text(literal: &[u8]) -> Option<String> {
    let text = std::str::from_utf8(literal).ok()?;
    let inner = text.strip_prefix('"')?.strip_suffix('"')?;
    let mut atom = String::new();
    let mut characters = inner.chars();
    while let Some(character) = characters.next() {
        match character {
            '\\' => atom.extend(characters.next()),
            _ => atom.push(character),
        }
    }
    let atom = atom.trim();
    (!atom.is_empty()).then(|| atom.to_string())
}

/// Reads one notation command.
struct Reader<'s> {
    src: &'s [u8],
    tokens: &'s [lexer::Token],
    index: usize,
}

impl Reader<'_> {
    fn kind(&self, index: usize) -> Option<Kind> {
        self.tokens.get(index).map(|token| token.kind)
    }

    fn text(&self, index: usize) -> &[u8] {
        self.tokens
            .get(index)
            .map_or(&[], |token| token.text(self.src))
    }

    /// The index after the bracket group that opens at `index`.
    fn group_end(&self, index: usize) -> usize {
        let mut depth = 0usize;
        for (at, token) in self.tokens.iter().enumerate().skip(index) {
            match token.kind {
                Kind::Open => depth += 1,
                Kind::Close if depth <= 1 => return at + 1,
                Kind::Close => depth -= 1,
                _ => {}
            }
        }
        self.tokens.len()
    }

    /// Skips a precedence `:65` or `:(arg + 1)` and returns its value if it is a number.
    fn skip_precedence(&mut self) -> Option<u32> {
        if self.kind(self.index) != Some(Kind::Colon) {
            return None;
        }
        self.index += 1;
        if self.kind(self.index) == Some(Kind::Open) {
            self.index = self.group_end(self.index);
            return None;
        }
        let value = precedence(self.text(self.index));
        self.index += 1;
        value
    }

    /// The spellings of an atom at the current token: a string literal or
    /// `unicode("≤", "<=")`.
    fn atoms(&mut self) -> Option<Vec<String>> {
        if self.kind(self.index) == Some(Kind::Str) {
            let atom = atom_text(self.text(self.index));
            self.index += 1;
            return Some(atom.into_iter().collect());
        }
        let is_unicode =
            self.text(self.index) == b"unicode" && self.kind(self.index + 1) == Some(Kind::Open);
        if !is_unicode {
            return None;
        }
        let end = self.group_end(self.index + 1);
        let spellings = (self.index + 2..end)
            .filter(|at| self.kind(*at) == Some(Kind::Str))
            .filter_map(|at| atom_text(self.text(at)))
            .collect();
        self.index = end;
        Some(spellings)
    }

    /// Skips `(name := x)`, `(kind := x)` and `(priority := y)` after the keyword and
    /// returns the priority if one is given.
    fn skip_options(&mut self) -> Option<u32> {
        let mut found = None;
        while self.kind(self.index) == Some(Kind::Open) {
            let end = self.group_end(self.index);
            if self.kind(self.index + 2) != Some(Kind::Assign) {
                break;
            }
            if self.text(self.index + 1) == b"priority" {
                let words: Vec<&[u8]> = (self.index + 3..end - 1)
                    .filter(|at| matches!(self.kind(*at), Some(Kind::Ident | Kind::Num)))
                    .map(|at| self.text(at))
                    .collect();
                found = Some(priority(&words));
            }
            self.index = end;
        }
        found
    }

    /// Whether the token is glued to the one before it.
    fn is_glued(&self, index: usize) -> bool {
        index > 0 && self.tokens[index].start == self.tokens[index - 1].end
    }

    /// Whether a bracket at `index` opens a syntax quotation `` `( ``.
    fn is_quotation(&self, index: usize) -> bool {
        let start = self.tokens[index].start as usize;
        self.kind(index) == Some(Kind::Open) && start > 0 && self.src[start - 1] == b'`'
    }

    /// The tokens inside the quotation that opens at `index` if it quotes a term: the
    /// range after a `term|` prefix. A quotation of another category gives `None`.
    fn quoted_term(&self, index: usize) -> Option<(usize, usize)> {
        let end = self.group_end(index) - 1;
        let has_category = self.kind(index + 1) == Some(Kind::Ident)
            && self.kind(index + 2) == Some(Kind::Bar)
            && self.is_glued(index + 2);
        match has_category {
            true if self.text(index + 1) == b"term" => Some((index + 3, end)),
            true => None,
            false => Some((index + 1, end)),
        }
    }

    /// Reads the pattern of a `syntax`, `macro` or `elab` command up to its `: category`
    /// and returns it if the category is `term`. String literals are atoms, glued if only
    /// `noWs` is between them; everything else is a hole.
    fn syntax_pattern(&mut self, written: &mut Written) -> Option<Vec<Part>> {
        let mut pattern: Vec<Part> = Vec::new();
        let mut glue = false;
        loop {
            match self.kind(self.index)? {
                Kind::Colon => break,
                Kind::Str => {
                    let atom = atom_text(self.text(self.index));
                    match (pattern.last_mut(), atom) {
                        (Some(Part::Atom(last)), Some(atom)) if glue => last.push_str(&atom),
                        (_, Some(atom)) => pattern.push(Part::Atom(atom)),
                        _ => {}
                    }
                    self.index += 1;
                }
                Kind::Ident if self.text(self.index) == b"noWs" => {
                    glue = true;
                    self.index += 1;
                    continue;
                }
                Kind::Open => {
                    let end = self.group_end(self.index);
                    self.bound_before_colons(self.index, end, written);
                    self.index = end;
                    self.hole(&mut pattern);
                }
                Kind::Ident => {
                    // `x:term` names its hole; `term:max` is a parser with a precedence,
                    // and `x:term:max` has both.
                    let glued_colon = |reader: &Self| {
                        reader.kind(reader.index + 1) == Some(Kind::Colon)
                            && reader.is_glued(reader.index + 1)
                    };
                    let mut category = self.index;
                    if glued_colon(self) {
                        let name = String::from_utf8_lossy(self.text(self.index)).into_owned();
                        written.bound.push(name);
                        category += 2;
                    }
                    // `(x : α) × β x` starts with binders, not with a term.
                    let category = String::from_utf8_lossy(self.text(category)).to_lowercase();
                    written.after_binder |= pattern.is_empty() && category.contains("binder");
                    while glued_colon(self) {
                        self.index += 2;
                        if self.kind(self.index) == Some(Kind::Open) {
                            self.index = self.group_end(self.index) - 1;
                        }
                    }
                    self.index += 1;
                    self.hole(&mut pattern);
                }
                _ => {
                    self.index += 1;
                    continue;
                }
            }
            glue = false;
        }
        let is_term = self.text(self.index + 1) == b"term";
        self.index += 2;
        is_term.then_some(pattern)
    }

    /// Adds a hole to a pattern unless it ends with one.
    fn hole(&self, pattern: &mut Vec<Part>) {
        if pattern.last() != Some(&Part::Hole) {
            pattern.push(Part::Hole);
        }
    }

    /// Adds the names of the holes written as `x:term` inside a group.
    fn bound_before_colons(&self, start: usize, end: usize, written: &mut Written) {
        for at in start..end.saturating_sub(1) {
            if self.kind(at) == Some(Kind::Ident)
                && self.kind(at + 1) == Some(Kind::Colon)
                && self.is_glued(at + 1)
            {
                written
                    .bound
                    .push(String::from_utf8_lossy(self.text(at)).into_owned());
            }
        }
    }

    /// Reads the pattern written as the term quotation with tokens `[start, end)`: an
    /// antiquotation `$x` is a hole, and tokens glued to each other are one atom.
    fn quoted_pattern(&self, start: usize, end: usize, written: &mut Written) -> Vec<Part> {
        let mut pattern: Vec<Part> = Vec::new();
        let mut at = start;
        // Whether the token before is the end of an antiquotation.
        let mut after_hole = false;
        while at < end {
            let text = self.text(at);
            if text == b"$" {
                at += 1;
                match self.kind(at) {
                    Some(Kind::Open) => {
                        let close = self.group_end(at);
                        let names = (at..close).filter(|i| self.kind(*i) == Some(Kind::Ident));
                        for name in names {
                            written
                                .bound
                                .push(String::from_utf8_lossy(self.text(name)).into_owned());
                        }
                        at = close;
                    }
                    Some(Kind::Ident) => {
                        written
                            .bound
                            .push(String::from_utf8_lossy(self.text(at)).into_owned());
                        at += 1;
                        // `$x:term` gives the category of the hole.
                        if self.kind(at) == Some(Kind::Colon) && self.is_glued(at) {
                            at += 2;
                        }
                    }
                    _ => {}
                }
                self.hole(&mut pattern);
                after_hole = true;
                continue;
            }
            // `$xs*`, `$xs,*` and `$[...]?` end an antiquotation.
            let splices = text == b"," && self.text(at + 1) == b"*" && self.is_glued(at + 1);
            let is_suffix =
                after_hole && self.is_glued(at) && (matches!(text, b"*" | b"?") || splices);
            if is_suffix {
                at += 1;
                continue;
            }
            let piece = String::from_utf8_lossy(text);
            match pattern.last_mut() {
                Some(Part::Atom(last)) if self.is_glued(at) && !after_hole => last.push_str(&piece),
                _ => pattern.push(Part::Atom(piece.into_owned())),
            }
            after_hole = false;
            at += 1;
        }
        pattern
    }

    /// Reads what the right-hand side with tokens `[start, end)` of a macro expands to:
    /// the term quotations in it and the names it quotes.
    fn expansion(&self, start: usize, end: usize, written: &mut Written) {
        let mut at = start;
        while at < end.min(self.tokens.len()) {
            let token = &self.tokens[at];
            let begin = token.start as usize;
            if self.is_quotation(at) {
                let close = self.group_end(at);
                if let Some((first, last)) = self.quoted_term(at)
                    && first < last
                {
                    let span = Span::new(self.tokens[first].start, self.tokens[last - 1].end);
                    written.expansions.push(span);
                }
                at = close;
                continue;
            }
            if token.kind == Kind::NameLit && begin > 0 && self.src[begin - 1] == b'`' {
                let name = String::from_utf8_lossy(&token.text(self.src)[1..]).into_owned();
                written.named.push(name);
            }
            at += 1;
        }
    }

    /// Whether every hole of a macro's pattern is written again in its quotations.
    fn quotes_every_hole(&self, written: &Written) -> bool {
        let quoted = |name: &String| {
            written.expansions.iter().any(|span| {
                let text = &self.src[span.start as usize..span.end as usize];
                let wanted = format!("${name}");
                text.windows(wanted.len())
                    .any(|window| window == wanted.as_bytes())
            })
        };
        written.bound.iter().all(quoted)
    }

    /// Reads the alternatives of a `macro_rules` command: one notation per term pattern.
    fn macro_rules(&mut self, template: &Written) -> Vec<Written> {
        let mut found: Vec<Written> = Vec::new();
        // The patterns of the alternative being read, which share a right-hand side.
        let mut open: Vec<Written> = Vec::new();
        let mut at = self.index;
        while at < self.tokens.len() {
            let starts_pattern = self.kind(at) == Some(Kind::Bar)
                && self.tokens.get(at + 2).is_some()
                && self.is_quotation(at + 2);
            if !starts_pattern {
                at += 1;
                continue;
            }
            let close = self.group_end(at + 2);
            let mut written = template.clone();
            if let Some((first, last)) = self.quoted_term(at + 2) {
                let pattern = self.quoted_pattern(first, last, &mut written);
                written.patterns = vec![pattern];
                open.push(written);
            }
            at = close;
            if self.kind(at) != Some(Kind::FatArrow) {
                continue;
            }
            // The right-hand side ends where the next alternative starts.
            let mut end = at + 1;
            while end < self.tokens.len() {
                let next = self.kind(end) == Some(Kind::Bar)
                    && self.tokens[end].lead
                    && self.tokens.get(end + 2).is_some()
                    && self.is_quotation(end + 2);
                if next {
                    break;
                }
                end += 1;
            }
            for mut written in open.drain(..) {
                self.expansion(at + 1, end, &mut written);
                written.is_opaque = written.expansions.is_empty() && written.named.is_empty();
                written.hides_binders = !self.quotes_every_hole(&written);
                found.push(written);
            }
            at = end;
        }
        found
    }

    /// Reads the parts of a `notation` or `notation3` pattern up to `=>`.
    fn pattern(&mut self, written: &mut Written) -> Vec<Vec<Part>> {
        let mut patterns: Vec<Vec<Part>> = vec![Vec::new()];
        while self.index < self.tokens.len() && self.kind(self.index) != Some(Kind::FatArrow) {
            if let Some(spellings) = self.atoms() {
                let Some(first) = spellings.first() else {
                    continue;
                };
                // Only the first atom of a pattern is looked up, so only there does a second
                // spelling make a second pattern.
                if patterns.len() == 1 && !patterns[0].iter().any(|p| matches!(p, Part::Atom(_))) {
                    let before = patterns[0].clone();
                    for other in &spellings[1..] {
                        let mut pattern = before.clone();
                        pattern.push(Part::Atom(other.clone()));
                        patterns.push(pattern);
                    }
                    patterns[0].push(Part::Atom(first.clone()));
                } else {
                    for pattern in &mut patterns {
                        pattern.push(Part::Atom(first.clone()));
                    }
                }
                continue;
            }
            match self.kind(self.index) {
                Some(Kind::Open) => {
                    let end = self.group_end(self.index);
                    let inside = (self.index + 1..end.saturating_sub(1))
                        .map(|at| self.text(at))
                        .collect::<Vec<_>>()
                        .concat();
                    // `(...)` stands for the binders of a `notation3`.
                    if inside == b"..." {
                        written.binds = true;
                        patterns.iter_mut().for_each(|p| p.push(Part::Hole));
                    } else {
                        self.expansion_group(self.index, end, written);
                    }
                    self.index = end;
                }
                Some(Kind::Ident) => {
                    let name = String::from_utf8_lossy(self.text(self.index)).into_owned();
                    written.bound.push(name);
                    patterns.iter_mut().for_each(|p| p.push(Part::Hole));
                    self.index += 1;
                    while self.kind(self.index) == Some(Kind::Colon) {
                        self.index += 1;
                        if self.kind(self.index) == Some(Kind::Open) {
                            let end = self.group_end(self.index);
                            self.expansion_group(self.index, end, written);
                            self.index = end;
                        } else {
                            self.index += 1;
                        }
                    }
                }
                _ => self.index += 1,
            }
        }
        patterns
    }

    /// Notes a `notation3` group such as `(scoped f => body)` or
    /// `(foldr (x y => body) init)`, which holds constants: it is part of the expansion,
    /// and the identifiers written before a `=>`, among them the word `scoped`, are bound.
    fn expansion_group(&self, start: usize, end: usize, written: &mut Written) {
        if start + 1 < end {
            let span = Span::new(self.tokens[start].end, self.tokens[end - 1].start);
            written.expansions.push(span);
        }
        let mut run: Vec<String> = Vec::new();
        for at in start..end {
            match self.tokens[at].kind {
                Kind::Ident => run.push(String::from_utf8_lossy(self.text(at)).into_owned()),
                Kind::FatArrow => written.bound.append(&mut run),
                _ => run.clear(),
            }
        }
    }
}

/// Whether the expansion binds one of the pattern's names with `fun`, as
/// `Filter.Eventually (fun x => p) f` does for `∀ᶠ x in f, p`.
fn binds_a_hole(reader: &Reader<'_>, start: usize, bound: &[String]) -> bool {
    let is_bound = |at: usize| {
        let name = String::from_utf8_lossy(reader.text(at));
        bound.iter().any(|bound| *bound == name)
    };
    (start..reader.tokens.len().saturating_sub(1)).any(|at| {
        let is_function = matches!(reader.text(at), b"fun" | b"\xce\xbb");
        let antiquoted = reader.text(at + 1) == b"$";
        is_function && is_bound(at + 1 + usize::from(antiquoted))
    })
}

/// Reads the notations declared by one command of a file.
///
/// `buffer` is used as the lexer's token buffer.
pub fn read_command(src: &[u8], command: &Command, buffer: &mut Vec<lexer::Token>) -> Vec<Written> {
    let keyword = src.get(command.head.start as usize..command.head.end as usize);
    let is_notation = matches!(
        keyword.unwrap_or(&[]),
        b"notation"
            | b"notation3"
            | b"infix"
            | b"infixl"
            | b"infixr"
            | b"prefix"
            | b"postfix"
            | b"syntax"
            | b"macro"
            | b"macro_rules"
            | b"elab"
    );
    if command.kind != CommandKind::Declaration || !is_notation {
        return Vec::new();
    }
    read_declaration(src, command, buffer).unwrap_or_default()
}

fn read_declaration(
    src: &[u8],
    command: &Command,
    buffer: &mut Vec<lexer::Token>,
) -> Option<Vec<Written>> {
    let keyword = src.get(command.head.start as usize..command.head.end as usize)?;
    let start = command.range.start as usize;
    let text = src.get(start..command.range.end as usize)?;
    buffer.clear();
    lexer::lex(text, buffer);
    let head = command.head.start as usize - start;
    let at_keyword = buffer
        .iter()
        .position(|token| token.start as usize == head)?;
    let mut reader = Reader {
        src: text,
        tokens: buffer,
        index: at_keyword + 1,
    };
    let mut written = Written {
        offset: command.range.start,
        patterns: Vec::new(),
        precedence: None,
        bound: Vec::new(),
        expansions: Vec::new(),
        named: Vec::new(),
        priority: 1000,
        is_opaque: false,
        is_macro: matches!(keyword, b"macro" | b"macro_rules"),
        hides_binders: false,
        after_binder: false,
        reach: Reach::Everywhere,
        namespace: None,
        binds: false,
    };
    for at in 0..at_keyword {
        match reader.text(at) {
            b"local" => written.reach = Reach::Local { end: u32::MAX },
            b"scoped" => {
                // `scoped[NS]` names the namespace; a bare `scoped` takes the current one,
                // which the scope tracker fills in.
                let named = reader.kind(at + 1) == Some(Kind::Open) && at + 2 < at_keyword;
                let namespace =
                    named.then(|| String::from_utf8_lossy(reader.text(at + 2)).into_owned());
                written.reach = Reach::Scoped(namespace.clone().unwrap_or_default());
                written.namespace = namespace;
            }
            _ => {}
        }
    }
    written.precedence = reader.skip_precedence();
    written.priority = reader.skip_options().unwrap_or(1000);
    if keyword == b"macro_rules" {
        let mut found = reader.macro_rules(&written);
        for written in &mut found {
            written.binds = binds_a_hole(&reader, at_keyword, &written.bound);
            shift(written, command.range.start);
        }
        return Some(found);
    }
    written.patterns = match (keyword, short_form(keyword)) {
        (_, Some((before, after))) => {
            let spellings = reader.atoms()?;
            let pattern = |atom: &String| {
                let mut parts = Vec::new();
                parts.extend(before.then_some(Part::Hole));
                parts.push(Part::Atom(atom.clone()));
                parts.extend(after.then_some(Part::Hole));
                parts
            };
            spellings.iter().map(pattern).collect()
        }
        (b"notation" | b"notation3", _) => reader.pattern(&mut written),
        _ => vec![reader.syntax_pattern(&mut written)?],
    };
    match keyword {
        b"syntax" | b"elab" => written.is_opaque = true,
        b"macro" => {
            let arrow =
                (reader.index..buffer.len()).find(|at| buffer[*at].kind == Kind::FatArrow)?;
            reader.expansion(arrow + 1, buffer.len(), &mut written);
            written.is_opaque = written.expansions.is_empty() && written.named.is_empty();
            written.hides_binders = !written.expansions.is_empty();
            written.binds = binds_a_hole(&reader, arrow, &written.bound);
        }
        _ => {
            let arrow =
                (reader.index..buffer.len()).find(|at| buffer[*at].kind == Kind::FatArrow)?;
            written.binds |= binds_a_hole(&reader, arrow, &written.bound);
            written
                .expansions
                .push(Span::new(buffer[arrow].end, text.len() as u32));
        }
    }
    shift(&mut written, command.range.start);
    let has_atom = |pattern: &Vec<Part>| pattern.iter().any(|part| matches!(part, Part::Atom(_)));
    written.patterns.retain(has_atom);
    (!written.patterns.is_empty()).then(|| vec![written])
}

/// Turns the expansion spans of a notation from offsets in its command to offsets in the
/// file.
fn shift(written: &mut Written, command_start: u32) {
    for span in &mut written.expansions {
        *span = Span::new(span.start + command_start, span.end + command_start);
    }
}

/// The first atom of each pattern of a notation.
fn first_atoms(written: &Written) -> impl Iterator<Item = &str> {
    written.patterns.iter().filter_map(|pattern| {
        pattern.iter().find_map(|part| match part {
            Part::Atom(atom) => Some(atom.as_str()),
            Part::Hole => None,
        })
    })
}

/// Reads the notations that the commands of a file declare, in order.
///
/// A `syntax` command and the `macro_rules` for it in the same file are one notation: the
/// rules take the reach, precedence and priority of the syntax, which is then left out.
pub fn read_notations(src: &[u8], commands: &[Command]) -> Vec<Written> {
    let mut buffer = Vec::new();
    let mut found: Vec<Written> = commands
        .iter()
        .flat_map(|command| read_command(src, command, &mut buffer))
        .collect();
    let mut syntax: HashMap<String, usize> = HashMap::new();
    for (index, written) in found.iter().enumerate() {
        if written.is_opaque {
            syntax.extend(first_atoms(written).map(|atom| (atom.to_string(), index)));
        }
    }
    if syntax.is_empty() {
        return found;
    }
    let mut expanded = vec![false; found.len()];
    for index in 0..found.len() {
        let declared = first_atoms(&found[index]).find_map(|atom| syntax.get(atom).copied());
        let is_rule = found[index].is_macro && !found[index].is_opaque;
        let Some(declared) = declared.filter(|_| is_rule) else {
            continue;
        };
        expanded[declared] = true;
        let (reach, namespace) = (
            found[declared].reach.clone(),
            found[declared].namespace.clone(),
        );
        let (precedence, priority) = (found[declared].precedence, found[declared].priority);
        let rules = &mut found[index];
        if rules.reach == Reach::Everywhere {
            rules.reach = reach;
            rules.namespace = namespace;
        }
        rules.precedence = rules.precedence.or(precedence);
        rules.priority = priority;
    }
    let mut index = 0;
    found.retain(|_| {
        index += 1;
        !expanded[index - 1]
    });
    found
}

/// A notation with the constants it stands for.
#[derive(Clone, Debug)]
pub struct Notation<'t> {
    /// Index of the file that declares it.
    pub file: u32,
    /// Byte offset of the command in its file.
    pub offset: u32,
    pub reach: Reach,
    /// Whether the pattern starts with its first atom, as a prefix does.
    pub leading: bool,
    /// The atoms after the first one, as tokens show them.
    pub later_atoms: Vec<String>,
    pub precedence: Option<u32>,
    /// The priority among notations for the same token.
    pub priority: u32,
    /// Whether what the notation expands to is unknown.
    pub is_opaque: bool,
    /// Whether what its binder predicates stand for is unknown.
    pub hides_binders: bool,
    /// Whether the pattern starts with binders and not with a term.
    pub after_binder: bool,
    pub binds: bool,
    /// The constants of the expansion, in order of first appearance.
    pub constants: Vec<&'t str>,
    /// The constant that the expansion applies, if it starts with one.
    pub head: Option<&'t str>,
}

/// What a notation written in a signature contributes.
#[derive(Clone, Debug)]
pub struct Fired<'n, 't> {
    /// The constants it stands for. If several notations fit, the ones they all have.
    pub constants: std::borrow::Cow<'n, [&'t str]>,
    /// The atoms of the pattern after the first one, as tokens show them.
    pub later_atoms: &'n [String],
    /// The constant that the expansion applies, if that is known.
    pub head: Option<&'t str>,
    /// Whether nothing is known about the expansion.
    pub is_opaque: bool,
    pub binds: bool,
    pub hides_binders: bool,
    pub leading: bool,
    pub precedence: Option<u32>,
}

/// What comes just before an atom in a term.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Before {
    /// A term ends there, so the atom is not a prefix.
    pub term: bool,
    /// That term is a bracketed binder such as `(x : α)`.
    pub binder: bool,
}

/// Every notation of a project, by first atom.
#[derive(Debug, Default)]
pub struct NotationTable<'t> {
    pub notations: Vec<Notation<'t>>,
    /// Per atom number, the notations whose first atom it is.
    by_atom: Vec<Vec<u32>>,
    /// Per atom number, whether some pattern ends with it.
    closers: Vec<bool>,
    /// The namespaces that have scoped notations.
    pub namespaces: HashSet<String>,
}

/// The position of the first of the tokens with this text among items, not in brackets.
fn position_of(items: &[Item<'_>], text: &str) -> Option<usize> {
    items
        .iter()
        .position(|item| item.token().is_some_and(|token| token.text == text))
}

impl<'t> NotationTable<'t> {
    /// A table for the given number of atoms.
    pub fn new(atoms: usize) -> Self {
        NotationTable {
            notations: Vec::new(),
            by_atom: vec![Vec::new(); atoms + 1],
            closers: vec![false; atoms + 1],
            namespaces: HashSet::new(),
        }
    }

    /// Adds a notation whose first atom has the given number.
    pub fn add(&mut self, atom: u32, last_atom: Option<u32>, notation: Notation<'t>) {
        if let Reach::Scoped(namespace) = &notation.reach {
            self.namespaces.insert(namespace.clone());
        }
        if let Some(last) = last_atom {
            self.closers[last as usize] = true;
        }
        self.by_atom[atom as usize].push(self.notations.len() as u32);
        self.notations.push(notation);
    }

    /// Whether some pattern ends with the atom, so that a term can end with it.
    pub fn is_closer(&self, atom: u32) -> bool {
        self.closers.get(atom as usize).copied().unwrap_or(false)
    }

    /// The notation that an atom written in a term stands for.
    ///
    /// `is_active` tells whether a notation can be used there, `before` what comes just
    /// before the atom, and `rest` are the items after it. Among the active notations, the
    /// ones with the highest priority are kept, then the ones that fit the position (`-x`
    /// against `a - b`, `(x : α) × β x` against `α × β`), then the ones whose next atom
    /// comes first in `rest`
    /// (`‖x‖₊` against `‖x‖`). If several notations are left, Lean would choose by type:
    /// only the constants that all of them stand for are sure, and none is if one of them
    /// expands to something unknown.
    pub fn fire<'n>(
        &'n self,
        atom: u32,
        before: Before,
        rest: &[Item<'_>],
        is_active: &dyn Fn(&Notation<'t>) -> bool,
    ) -> Option<Fired<'n, 't>> {
        let all = self.by_atom.get(atom as usize)?;
        let mut candidates: Vec<&Notation<'t>> = all
            .iter()
            .map(|index| &self.notations[*index as usize])
            .filter(|notation| is_active(notation))
            .collect();
        let highest = candidates.iter().map(|notation| notation.priority).max()?;
        candidates.retain(|notation| notation.priority == highest);
        let fits = |notation: &&Notation<'t>| notation.leading != before.term;
        if candidates.iter().any(fits) {
            candidates.retain(fits);
        }
        // A notation over binders is the one meant after a binder, and only there.
        let binds = |notation: &&Notation<'t>| notation.after_binder;
        if before.binder && candidates.iter().any(binds) {
            candidates.retain(binds);
        } else {
            candidates.retain(|notation| !notation.after_binder);
        }
        if candidates.len() > 1 {
            let next = |notation: &Notation<'t>| match notation.later_atoms.first() {
                Some(atom) => position_of(rest, atom),
                None => Some(usize::MAX),
            };
            if let Some(nearest) = candidates.iter().filter_map(|n| next(n)).min() {
                candidates.retain(|notation| next(notation) == Some(nearest));
            }
        }
        let first = *candidates.first()?;
        let others = &candidates[1..];
        let is_opaque = candidates.iter().any(|notation| notation.is_opaque);
        let constants = match others {
            [] => std::borrow::Cow::Borrowed(first.constants.as_slice()),
            _ if is_opaque => std::borrow::Cow::Borrowed(&[][..]),
            _ => {
                let mut shared = first.constants.clone();
                shared.retain(|constant| {
                    others
                        .iter()
                        .all(|other| other.constants.contains(constant))
                });
                std::borrow::Cow::Owned(shared)
            }
        };
        Some(Fired {
            constants,
            later_atoms: &first.later_atoms,
            head: first
                .head
                .filter(|_| others.iter().all(|other| other.head == first.head)),
            is_opaque: is_opaque && others.is_empty(),
            binds: candidates.iter().any(|notation| notation.binds),
            hides_binders: candidates.iter().any(|notation| notation.hides_binders),
            leading: first.leading,
            precedence: first.precedence.filter(|_| {
                others
                    .iter()
                    .all(|other| other.precedence == first.precedence)
            }),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::parse;

    fn written(source: &str) -> Vec<Written> {
        let parsed = parse(source.as_bytes());
        read_notations(source.as_bytes(), &parsed.commands)
    }

    fn atoms(pattern: &[Part]) -> Vec<&str> {
        pattern
            .iter()
            .map(|part| match part {
                Part::Atom(atom) => atom.as_str(),
                Part::Hole => "_",
            })
            .collect()
    }

    fn expansion<'s>(source: &'s str, written: &Written) -> &'s str {
        let span = written.expansions[0];
        source[span.start as usize..span.end as usize].trim()
    }

    #[test]
    fn short_forms_have_one_atom_between_holes() {
        let source = "infixl:65 \" + \" => HAdd.hAdd\nprefix:max \"-\" => Neg.neg\n\
                      postfix:max \"⁻¹\" => Inv.inv\n";
        let found = written(source);
        assert_eq!(atoms(&found[0].patterns[0]), ["_", "+", "_"]);
        assert_eq!(found[0].precedence, Some(65));
        assert_eq!(expansion(source, &found[0]), "HAdd.hAdd");
        assert_eq!(atoms(&found[1].patterns[0]), ["-", "_"]);
        assert_eq!(found[1].precedence, Some(1024));
        assert_eq!(atoms(&found[2].patterns[0]), ["_", "⁻¹"]);
    }

    #[test]
    fn modifiers_attributes_and_options_are_read() {
        let source = "@[inherit_doc] scoped infix:50 (priority := low) \" ≃' \" => Equiv\n\
                      local notation \"ℝ'\" => Real\n\
                      scoped[Topology] notation \"𝓝\" => nhds\n\
                      /-- doc -/\nnotation (name := mem) a:50 \" ∈' \" b:50 => Membership.mem b a\n";
        let found = written(source);
        assert_eq!(found[0].reach, Reach::Scoped(String::new()));
        assert_eq!(atoms(&found[0].patterns[0]), ["_", "≃'", "_"]);
        assert_eq!(found[1].reach, Reach::Local { end: u32::MAX });
        assert_eq!(found[2].reach, Reach::Scoped("Topology".to_string()));
        assert_eq!(found[2].namespace.as_deref(), Some("Topology"));
        assert_eq!(atoms(&found[3].patterns[0]), ["_", "∈'", "_"]);
        assert_eq!(found[3].bound, ["a", "b"]);
        assert_eq!(expansion(source, &found[3]), "Membership.mem b a");
    }

    #[test]
    fn unicode_spellings_make_one_pattern_each() {
        let found = written("infix:50 unicode(\" ≤ \", \" <= \") => LE.le\n");
        let patterns: Vec<Vec<&str>> = found[0].patterns.iter().map(|p| atoms(p)).collect();
        assert_eq!(patterns, [["_", "≤", "_"], ["_", "<=", "_"]]);
    }

    #[test]
    fn mixfix_patterns_keep_every_atom() {
        let found = written("notation \"‖\" e \"‖₊\" => nnnorm e\nnotation \"{\" \"}\" => Empty\n");
        assert_eq!(atoms(&found[0].patterns[0]), ["‖", "_", "‖₊"]);
        assert_eq!(atoms(&found[1].patterns[0]), ["{", "}"]);
    }

    #[test]
    fn notation3_binders_and_folds() {
        let source = "notation3 \"∑ \"(...)\", \"r:67:(scoped f => Finset.sum Finset.univ f) => r\n\
                      notation3 \"[\" (l\", \"* => foldr (h t => List.cons h t) List.nil) \"]\" => l\n";
        let found = written(source);
        assert_eq!(atoms(&found[0].patterns[0]), ["∑", "_", ",", "_"]);
        assert!(found[0].binds);
        assert_eq!(found[0].bound, ["r", "scoped", "f"]);
        assert_eq!(
            expansion(source, &found[0]),
            "scoped f => Finset.sum Finset.univ f"
        );
        assert_eq!(found[0].expansions.len(), 2);
        assert!(found[1].bound.contains(&"h".to_string()));
    }

    #[test]
    fn a_function_over_a_hole_binds() {
        let found =
            written("notation \"∀ᶠ \" x \" in \" f \", \" p => Filter.Eventually (fun x => p) f\n");
        assert!(found[0].binds);
        assert!(!written("notation \"ℝ\" => Real\n")[0].binds);
    }

    #[test]
    fn other_commands_declare_no_notation() {
        assert!(
            written("def f := 1\nsyntax \"a\" : tactic\nmacro \"b\" : command => `(1)\n")
                .is_empty()
        );
        assert!(written("notation\nmacro_rules | `(tactic| a $x) => `(tactic| b)\n").is_empty());
    }

    #[test]
    fn syntax_is_a_pattern_with_an_unknown_expansion() {
        let source = "syntax:50 (name := sub) (priority := high + 1) term:51 \" ⊆ \" term:51 : term\n\
                      scoped syntax:9000 term:max noWs \"[\" noWs &\"X\" noWs \"]\" : term\n\
                      elab \"here% \" x:term : term => f x\n";
        let found = written(source);
        assert_eq!(atoms(&found[0].patterns[0]), ["_", "⊆", "_"]);
        assert!(found[0].is_opaque && found[0].expansions.is_empty());
        assert_eq!((found[0].precedence, found[0].priority), (Some(50), 10001));
        assert_eq!(atoms(&found[1].patterns[0]), ["_", "[X]"]);
        assert_eq!(found[1].reach, Reach::Scoped(String::new()));
        assert_eq!(atoms(&found[2].patterns[0]), ["here%", "_"]);
        assert_eq!(found[2].bound, ["x"]);
        let star = written("elab \"Category*\" ppSpace C:term:arg : term => f C\n");
        assert_eq!(atoms(&star[0].patterns[0]), ["Category*", "_"]);
    }

    #[test]
    fn macro_rules_give_one_notation_per_term_pattern() {
        let source = "macro_rules (kind := big)\n  | `(∑ $x ∈ $s, $f) => do\n    let y ← helper x\n    \
                      `(Finset.sum $s (fun $x ↦ $f))\n  | `($R[X]) => `(Polynomial $R)\n  \
                      | `(tactic| foo) => `(tactic| bar)\n";
        let found = written(source);
        assert_eq!(found.len(), 2);
        assert_eq!(atoms(&found[0].patterns[0]), ["∑", "_", "∈", "_", ",", "_"]);
        assert_eq!(found[0].bound, ["x", "s", "f"]);
        assert_eq!(expansion(source, &found[0]), "Finset.sum $s (fun $x ↦ $f)");
        assert!(found[0].binds && !found[0].is_opaque && !found[0].hides_binders);
        let coded = written("macro_rules | `(∑ $bs, $f) => do `(Finset.sum (fun x ↦ $f))\n");
        assert!(coded[0].hides_binders);
        assert_eq!(atoms(&found[1].patterns[0]), ["_", "[X]"]);
        assert_eq!(expansion(source, &found[1]), "Polynomial $R");
    }

    #[test]
    fn macros_expand_to_quotations_and_quoted_names() {
        let source = "macro \"∃\" xs:explicitBinders \", \" b:term : term => expand ``Exists xs b\n\
                      macro:max \"‖\" x:term \"‖₊\" : term => `(nnnorm $x)\n\
                      macro \"here\" : term => do return mkNatLit 1\n";
        let found = written(source);
        assert_eq!(atoms(&found[0].patterns[0]), ["∃", "_", ",", "_"]);
        assert_eq!(found[0].named, ["Exists"]);
        assert_eq!(atoms(&found[1].patterns[0]), ["‖", "_", "‖₊"]);
        assert_eq!(expansion(source, &found[1]), "nnnorm $x");
        assert!(found[2].is_opaque);
    }

    #[test]
    fn rules_in_the_same_file_take_the_place_of_their_syntax() {
        let source = "scoped syntax:65 (priority := high) term \" ⊔ \" term : term\n\
                      macro_rules | `($a ⊔ $b) => `(Max.max $a $b)\n\
                      syntax term \" ⊓ \" term : term\n";
        let found = written(source);
        assert_eq!(found.len(), 2);
        assert_eq!(found[0].reach, Reach::Scoped(String::new()));
        assert_eq!((found[0].precedence, found[0].priority), (Some(65), 10000));
        assert!(!found[0].is_opaque && found[1].is_opaque);
    }

    #[test]
    fn atoms_are_matched_longest_first() {
        let mut atoms = Atoms::default();
        for atom in [
            "ℝ",
            "ℝ≥0",
            "ℝ≥0∞",
            "C(",
            "[X]",
            "(",
            ")",
            ",",
            "]ₗ[",
            "[⋀^",
            "‖",
            "‖₊",
        ] {
            atoms.intern(atom);
        }
        atoms.finish();
        let found = |source: &str| -> Vec<(String, usize)> {
            atoms
                .matches(source.as_bytes(), 0)
                .map(|(number, length)| (atoms.core(number).to_string(), length))
                .collect()
        };
        assert_eq!(
            found("ℝ≥0∞ x"),
            [
                ("ℝ≥0∞".to_string(), 10),
                ("ℝ≥0".to_string(), 7),
                ("ℝ".to_string(), 3)
            ]
        );
        assert_eq!(found("C(X, Y)"), [("C".to_string(), 1)]);
        assert_eq!(found("[X]"), [("[X]".to_string(), 3)]);
        assert!(found("(a)").is_empty());
        assert!(found("]ₗ[").is_empty());
        assert!(found(", a").is_empty());
        let after_bracket = |source: &str| -> Vec<String> {
            let matches = atoms.matches(source.as_bytes(), 1);
            matches
                .map(|(number, _)| atoms.core(number).to_string())
                .collect()
        };
        assert_eq!(after_bracket("]ₗ[R]"), ["ₗ"]);
        assert_eq!(after_bracket("[⋀^ι]"), ["⋀^"]);
        assert!(after_bracket(" ₗ[R]").is_empty());
        assert_eq!(found("‖₊")[0].0, "‖₊");
        assert!(found("").is_empty());
    }

    const AFTER_TERM: Before = Before {
        term: true,
        binder: false,
    };

    fn notation(leading: bool, later: &[&str], constants: &[&'static str]) -> Notation<'static> {
        Notation {
            file: 0,
            offset: 0,
            reach: Reach::Everywhere,
            leading,
            later_atoms: later.iter().map(|atom| atom.to_string()).collect(),
            precedence: None,
            priority: 1000,
            is_opaque: false,
            hides_binders: false,
            after_binder: false,
            binds: false,
            constants: constants.to_vec(),
            head: None,
        }
    }

    fn items(source: &str) -> Vec<Item<'_>> {
        super::super::tokens::nest(super::super::tokens::tokenize(source.as_bytes()))
    }

    #[test]
    fn position_and_later_atoms_choose_among_notations() {
        let mut table = NotationTable::new(2);
        table.add(1, None, notation(true, &[], &["Neg.neg"]));
        table.add(1, None, notation(false, &[], &["HSub.hSub"]));
        table.add(2, Some(2), notation(true, &["‖"], &["norm"]));
        table.add(2, None, notation(true, &["‖₊"], &["nnnorm"]));
        let fire = |atom, term, rest: &str| {
            let before = Before {
                term,
                binder: false,
            };
            let fired = table.fire(atom, before, &items(rest), &|_| true);
            fired
                .map(|fired| fired.constants.to_vec())
                .filter(|found| !found.is_empty())
        };
        assert_eq!(fire(1, false, "x"), Some(vec!["Neg.neg"]));
        assert_eq!(fire(1, true, "x"), Some(vec!["HSub.hSub"]));
        assert_eq!(fire(2, false, "x ‖"), Some(vec!["norm"]));
        assert_eq!(fire(2, false, "x"), None);
        assert!(table.is_closer(2));
        assert!(!table.is_closer(1));
    }

    #[test]
    fn several_notations_give_the_constants_they_share() {
        let mut table = NotationTable::new(2);
        table.add(1, None, notation(false, &[], &["A.tensor"]));
        table.add(1, None, notation(false, &[], &["B.tensor"]));
        table.add(1, None, notation(false, &[], &["A.tensor"]));
        table.add(2, None, notation(true, &[","], &["Exists"]));
        table.add(2, None, notation(true, &[","], &["Exists", "And"]));
        assert!(
            table
                .fire(1, AFTER_TERM, &[], &|_| true)
                .unwrap()
                .constants
                .is_empty()
        );
        let only_first = |notation: &Notation<'_>| notation.constants == ["A.tensor"];
        let fired = table.fire(1, AFTER_TERM, &[], &only_first).unwrap();
        assert_eq!(fired.constants.to_vec(), ["A.tensor"]);
        assert!(table.fire(1, AFTER_TERM, &[], &|_| false).is_none());
        let fired = table
            .fire(2, Before::default(), &items("x , p"), &|_| true)
            .unwrap();
        assert_eq!(fired.constants.to_vec(), ["Exists"]);
    }

    #[test]
    fn a_notation_over_binders_is_used_after_a_binder_only() {
        let source =
            "macro:35 xs:bracketedExplicitBinders \" × \" b:term:35 : term => f ``Sigma xs b\n";
        assert!(written(source)[0].after_binder);
        let mut table = NotationTable::new(1);
        let mut sigma = notation(false, &[], &["Sigma"]);
        sigma.after_binder = true;
        table.add(1, None, notation(false, &[], &["Prod"]));
        table.add(1, None, sigma);
        let after_binder = Before {
            term: true,
            binder: true,
        };
        let fired = table.fire(1, AFTER_TERM, &[], &|_| true).unwrap();
        assert_eq!(fired.constants.to_vec(), ["Prod"]);
        let fired = table.fire(1, after_binder, &[], &|_| true).unwrap();
        assert_eq!(fired.constants.to_vec(), ["Sigma"]);
    }

    #[test]
    fn priority_and_unknown_expansions() {
        let mut table = NotationTable::new(2);
        let mut preferred = notation(false, &[], &[]);
        (preferred.priority, preferred.is_opaque) = (10000, true);
        table.add(1, None, notation(false, &[], &["HasSubset.Subset"]));
        table.add(1, None, preferred.clone());
        let fired = table.fire(1, AFTER_TERM, &[], &|_| true).unwrap();
        assert!(fired.constants.is_empty() && fired.is_opaque);
        preferred.priority = 1000;
        table.add(2, None, notation(false, &[], &["HasSubset.Subset"]));
        table.add(2, None, preferred);
        let fired = table.fire(2, AFTER_TERM, &[], &|_| true).unwrap();
        assert!(fired.constants.is_empty() && !fired.is_opaque);
    }
}
