//! Identifiers and symbols of a span, as the reference resolver reads them.
//!
//! The file's lexer tokens are turned into a simpler stream: comments, string, character,
//! number and name literals are dropped, identifiers are written the way Lean prints them, and
//! symbols are split into single characters except for a few two-character ones. Brackets
//! then arrange the stream into a tree.

use crate::chars::{decode, starts_ident};
use crate::lexer::{self, Kind};
use std::borrow::Cow;

/// Symbols of two characters that are kept whole.
const TWO_CHARACTER_SYMBOLS: [&[u8]; 7] = [b"=>", b"->", b":=", b"//", b"|>", b"<|", b".{"];

/// Identifiers after which a string literal is interpolated.
const INTERPOLATION_PREFIXES: [&str; 6] = ["s!", "m!", "f!", "throwError", "panic!", "dbgTrace"];

/// Brackets that open a group.
const OPENING: [&str; 6] = ["(", "{", ".{", "[", "⦃", "⟨"];

/// Brackets that close a group.
const CLOSING: [&str; 5] = [")", "}", "]", "⦄", "⟩"];

/// Deepest nesting of groups; brackets nested deeper are dropped, which bounds the recursion
/// of everything that walks the tree.
const MAX_DEPTH: usize = 200;

/// Brackets that hold a binder, as `(a b : T)`.
const BINDER_BRACKETS: [&str; 4] = ["(", "{", "[", "⦃"];

/// One identifier or symbol of a piece of source.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Token<'a> {
    /// The identifier with `«»` escapes normalised, or the symbol.
    pub text: Cow<'a, str>,
    /// Whether the token is an identifier rather than a symbol.
    pub is_identifier: bool,
    /// Whether a `.` directly precedes the identifier, as in `(f x).symm`.
    pub after_dot: bool,
}

/// A bracketed run of tokens.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Group<'a> {
    /// The opening bracket.
    pub opening: &'static str,
    /// The tokens and nested groups between the brackets.
    pub items: Vec<Item<'a>>,
}

/// A token or a bracket group.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Item<'a> {
    Token(Token<'a>),
    Group(Group<'a>),
}

impl<'a> Item<'a> {
    /// The token, unless the item is a group.
    pub fn token(&self) -> Option<&Token<'a>> {
        match self {
            Item::Token(token) => Some(token),
            Item::Group(_) => None,
        }
    }

    /// Whether the item is one of the given symbols.
    pub fn is_symbol(&self, symbols: &[&str]) -> bool {
        self.token()
            .is_some_and(|t| !t.is_identifier && symbols.contains(&&*t.text))
    }

    /// Whether the item is one of the given identifiers.
    pub fn is_word(&self, words: &[&str]) -> bool {
        self.token()
            .is_some_and(|t| t.is_identifier && words.contains(&&*t.text))
    }

    /// The name of an unqualified identifier that a binder can bind.
    pub fn plain(&self) -> Option<&str> {
        let token = self.token()?;
        (token.is_identifier && !token.after_dot && !token.text.contains('.')).then(|| &*token.text)
    }
}

fn text(src: &[u8], start: usize, end: usize) -> Cow<'_, str> {
    String::from_utf8_lossy(&src[start..end.min(src.len())])
}

/// Writes an identifier the way Lean prints it: `«exists»` becomes `exists`.
fn normalise(written: Cow<'_, str>) -> Cow<'_, str> {
    match crate::scan::lean_name(&written) {
        Some(name) => Cow::Owned(name),
        None => written,
    }
}

/// The `{...}` terms inside an interpolated string literal, as byte ranges of `src`.
fn interpolated(src: &[u8], start: usize, end: usize) -> Vec<(usize, usize)> {
    let mut terms = Vec::new();
    let mut depth = 0usize;
    let mut term_start = 0;
    for (index, &byte) in src[start..end].iter().enumerate() {
        if byte == b'{' {
            if depth == 0 {
                term_start = start + index + 1;
            }
            depth += 1;
        } else if byte == b'}' && depth > 0 {
            depth -= 1;
            if depth == 0 {
                terms.push((term_start, start + index));
            }
        }
    }
    terms
}

/// Splits the symbol characters of `[start, end)` and returns where reading stopped, which
/// is after `end` when a two-character symbol takes the first character of the next token.
fn read_symbols<'a>(src: &'a [u8], start: usize, end: usize, out: &mut Vec<Token<'a>>) -> usize {
    let mut position = start;
    while position < end {
        let pair = src.get(position..position + 2);
        if pair.is_some_and(|pair| TWO_CHARACTER_SYMBOLS.contains(&pair)) {
            out.push(symbol(text(src, position, position + 2)));
            position += 2;
            continue;
        }
        let (code_point, length) = decode(src, position);
        let is_space = char::from_u32(code_point).is_some_and(char::is_whitespace);
        // The backticks of a doubly quoted name ``` ``Foo ``` belong to the name literal.
        let mut after_backticks = position;
        while src.get(after_backticks) == Some(&b'`') {
            after_backticks += 1;
        }
        let quotes_name = after_backticks > position && starts_ident(src, after_backticks);
        if !is_space && !quotes_name {
            out.push(symbol(text(src, position, position + length)));
        }
        position += length;
    }
    position
}

fn symbol(text: Cow<'_, str>) -> Token<'_> {
    Token {
        text,
        is_identifier: false,
        after_dot: false,
    }
}

/// Reads the identifiers and symbols of the lexer tokens `toks` of `src`, appending to `out`.
fn read_tokens<'a>(src: &'a [u8], toks: &[lexer::Token], out: &mut Vec<Token<'a>>) {
    let mut consumed = 0usize;
    for token in toks {
        let (start, end) = (token.start as usize, token.end as usize);
        if end <= consumed {
            continue;
        }
        match token.kind {
            Kind::Ident => out.push(Token {
                text: normalise(text(src, start, end)),
                is_identifier: true,
                after_dot: start > 0 && src[start - 1] == b'.',
            }),
            Kind::Hash => {
                out.push(symbol(text(src, start, start + 1)));
                out.push(Token {
                    text: normalise(text(src, start + 1, end)),
                    is_identifier: true,
                    after_dot: false,
                });
            }
            Kind::Str => {
                let prefixed = out
                    .last()
                    .is_some_and(|t| INTERPOLATION_PREFIXES.contains(&&*t.text));
                if prefixed && src[start] == b'"' {
                    for (term_start, term_end) in interpolated(src, start, end) {
                        out.extend(tokenize(&src[term_start..term_end]));
                    }
                }
            }
            Kind::Doc | Kind::ModDoc | Kind::Char | Kind::Num | Kind::NameLit => {}
            _ => consumed = read_symbols(src, start.max(consumed), end, out),
        }
    }
}

/// Reads the identifiers and symbols of a piece of Lean source, such as a signature or a
/// command.
pub fn tokenize(src: &[u8]) -> Vec<Token<'_>> {
    tokenize_with(src, &mut Vec::new())
}

/// Like [`tokenize`], with `buffer` as the lexer's token buffer, to reuse its allocation.
pub fn tokenize_with<'a>(src: &'a [u8], buffer: &mut Vec<lexer::Token>) -> Vec<Token<'a>> {
    buffer.clear();
    lexer::lex(src, buffer);
    let mut out = Vec::with_capacity(buffer.len());
    read_tokens(src, buffer, &mut out);
    out
}

/// Arranges tokens into a tree by their brackets.
///
/// Unbalanced brackets do not fail: a stray closing bracket is dropped and an unclosed group
/// ends at the end of the tokens.
pub fn nest(tokens: Vec<Token<'_>>) -> Vec<Item<'_>> {
    let mut stack: Vec<(&'static str, Vec<Item>)> = vec![("", Vec::new())];
    // Brackets opened beyond the deepest nesting and not closed yet.
    let mut dropped = 0usize;
    for token in tokens {
        let opening = OPENING
            .into_iter()
            .find(|opening| !token.is_identifier && **opening == *token.text);
        if let Some(opening) = opening {
            if stack.len() > MAX_DEPTH {
                dropped += 1;
            } else {
                stack.push((opening, Vec::new()));
            }
        } else if !token.is_identifier && CLOSING.contains(&&*token.text) {
            if dropped > 0 {
                dropped -= 1;
            } else if stack.len() > 1 {
                close(&mut stack);
            }
        } else {
            stack.last_mut().unwrap().1.push(Item::Token(token));
        }
    }
    while stack.len() > 1 {
        close(&mut stack);
    }
    stack.pop().unwrap().1
}

fn close<'a>(stack: &mut Vec<(&'static str, Vec<Item<'a>>)>) {
    let (opening, items) = stack.pop().unwrap();
    let group = Item::Group(Group { opening, items });
    stack.last_mut().unwrap().1.push(group);
}

/// Collects the unqualified identifiers in items, including nested ones.
pub fn plain_names<'i>(items: &'i [Item<'_>], names: &mut Vec<&'i str>) {
    for item in items {
        match item {
            Item::Group(group) => plain_names(&group.items, names),
            Item::Token(_) => names.extend(item.plain()),
        }
    }
}

/// The index of the first `:` among items, not inside brackets.
pub fn colon_index(items: &[Item<'_>]) -> Option<usize> {
    items.iter().position(|item| item.is_symbol(&[":"]))
}

/// The names a bracketed binder such as `(a b : T)` introduces: the names before the colon,
/// the names of an untyped `{a b}` or `(a b)` binder, or nothing for an instance binder
/// `[Foo a]`. A pattern such as `⟨a, b⟩` binds every name in it.
pub fn group_binder_names<'i>(group: &'i Group<'_>, names: &mut Vec<&'i str>) {
    if !BINDER_BRACKETS.contains(&group.opening) {
        return plain_names(&group.items, names);
    }
    match colon_index(&group.items) {
        Some(colon) => plain_names(&group.items[..colon], names),
        None if group.opening == "[" => {}
        None => plain_names(&group.items, names),
    }
}

/// The names a run of binders introduces, as written after `variable`, `∀` or `fun`:
/// bracketed binders, patterns and bare names.
pub fn binder_names<'i>(items: &'i [Item<'_>], names: &mut Vec<&'i str>) {
    for item in items {
        match item {
            Item::Group(group) => group_binder_names(group, names),
            Item::Token(_) => names.extend(item.plain()),
        }
    }
}

/// A key that identifies an instance binder by the names in it.
pub fn instance_key(items: &[Item<'_>]) -> String {
    let mut names = Vec::new();
    plain_names(items, &mut names);
    format!("[{}]", names.join(" "))
}

/// The identifiers among items, not looking inside brackets.
pub fn identifier_texts<'i>(items: &'i [Item<'_>]) -> Vec<&'i str> {
    items
        .iter()
        .filter_map(Item::token)
        .filter(|token| token.is_identifier)
        .map(|token| &*token.text)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn texts(source: &str) -> Vec<String> {
        tokenize(source.as_bytes())
            .into_iter()
            .map(|token| token.text.into_owned())
            .collect()
    }

    fn items(source: &str) -> Vec<Item<'_>> {
        nest(tokenize(source.as_bytes()))
    }

    fn word(text: &str) -> Item<'_> {
        Item::Token(Token {
            text: Cow::Borrowed(text),
            is_identifier: true,
            after_dot: false,
        })
    }

    #[test]
    fn identifiers_keep_dots_primes_and_unicode() {
        assert_eq!(
            texts("Nat.succ x' α₁ ℘ foo? bar!"),
            ["Nat.succ", "x'", "α₁", "℘", "foo?", "bar!"]
        );
    }

    #[test]
    fn lambda_and_big_operators_are_symbols() {
        let symbols: Vec<String> = tokenize("λ x, Σ y, Π z".as_bytes())
            .into_iter()
            .filter(|token| !token.is_identifier)
            .map(|token| token.text.into_owned())
            .collect();
        assert_eq!(symbols, ["λ", ",", "Σ", ",", "Π"]);
    }

    #[test]
    fn guillemets_are_one_component() {
        assert_eq!(
            texts("A.«two words».b «exists» Foo.«x y».bar"),
            ["A.«two words».b", "exists", "Foo.«x y».bar"]
        );
        assert_eq!(texts("Nat.succ"), ["Nat.succ"]);
    }

    #[test]
    fn comments_are_skipped_including_nested_ones() {
        assert_eq!(texts("a /- b /- c -/ d -/ e -- f\ng"), ["a", "e", "g"]);
        assert_eq!(texts("a /- never closed"), ["a"]);
        assert_eq!(texts("a /-- doc -/ b /-! module doc -/ c"), ["a", "b", "c"]);
    }

    #[test]
    fn literals_are_skipped() {
        let source = "a \"b c\" 'd' '\\n' 12 0xFF 1.5e3 `Nat.succ ``Foo e";
        assert_eq!(texts(source), ["a", "e"]);
        assert_eq!(texts("a \"unterminated"), ["a"]);
    }

    #[test]
    fn raw_strings_are_skipped() {
        assert_eq!(texts("r#\"say \"A\"\"# b r\"c\" d"), ["b", "d"]);
        assert!(texts("r#\"never closed").is_empty());
        assert_eq!(texts("r x"), ["r", "x"]);
    }

    #[test]
    fn interpolated_strings_keep_their_terms() {
        assert_eq!(
            texts("s!\"text {marker} and {f {x}}\" tail"),
            ["s!", "marker", "f", "{", "x", "}", "tail"]
        );
        assert_eq!(texts("g \"text {marker}\" tail"), ["g", "tail"]);
    }

    #[test]
    fn identifier_after_a_dot_is_marked() {
        let found: Vec<(String, bool)> = tokenize("(f x).symm = .zero h.1.le".as_bytes())
            .into_iter()
            .filter(|token| token.is_identifier)
            .map(|token| (token.text.into_owned(), token.after_dot))
            .collect();
        let expected = [
            ("f", false),
            ("x", false),
            ("symm", true),
            ("zero", true),
            ("h", false),
            ("le", true),
        ];
        let expected: Vec<(String, bool)> = expected
            .iter()
            .map(|(text, after_dot)| (text.to_string(), *after_dot))
            .collect();
        assert_eq!(found, expected);
    }

    #[test]
    fn two_character_symbols() {
        assert_eq!(
            texts("a := b => c -> d .{u} // e |> f <| g"),
            [
                "a", ":=", "b", "=>", "c", "->", "d", ".{", "u", "}", "//", "e", "|>", "f", "<|",
                "g"
            ]
        );
    }

    #[test]
    fn other_symbols_are_single_characters() {
        assert_eq!(
            texts("a :: b || c <|> d @[e] ∀ x"),
            [
                "a", ":", ":", "b", "|", "|", "c", "<|", ">", "d", "@", "[", "e", "]", "∀", "x"
            ]
        );
        assert_eq!(texts("#check a"), ["#", "check", "a"]);
    }

    #[test]
    fn nest_groups_by_brackets() {
        let nested = items("a (b {c}) d");
        assert_eq!(nested.len(), 3);
        assert_eq!(nested[0], word("a"));
        assert_eq!(nested[2], word("d"));
        let Item::Group(group) = &nested[1] else {
            panic!("expected a group");
        };
        assert_eq!(group.opening, "(");
        assert!(matches!(&group.items[1], Item::Group(inner) if inner.opening == "{"));
        let unbalanced = items("a ) b ( c");
        assert_eq!(unbalanced[0], word("a"));
        assert_eq!(unbalanced.len(), 3);
    }

    #[test]
    fn deep_nesting_is_bounded() {
        let source = format!("{}a{} b", "(".repeat(100_000), ")".repeat(100_000));
        let mut nested = items(&source);
        assert_eq!(nested.len(), 2);
        let mut depth = 0;
        while let Some(Item::Group(group)) = nested.into_iter().next() {
            nested = group.items;
            depth += 1;
        }
        assert_eq!(depth, MAX_DEPTH);
    }

    #[test]
    fn long_whitespace_is_linear() {
        let source = format!("def t : hfun {}10", " ".repeat(200_000));
        let start = std::time::Instant::now();
        tokenize(source.as_bytes());
        assert!(start.elapsed().as_secs() < 2);
    }

    #[test]
    fn binder_names_cover_the_binder_forms() {
        let nested = items("(a b : T) {c} ⦃d : T⦄ [inst : C a] [C b] (⟨p, q⟩ : P) e");
        let mut names = Vec::new();
        binder_names(&nested, &mut names);
        assert_eq!(names, ["a", "b", "c", "d", "inst", "p", "q", "e"]);
    }

    #[test]
    fn group_binder_names_of_a_pattern() {
        let nested = items("⟨a, ⟨b, c⟩⟩");
        let [Item::Group(group)] = nested.as_slice() else {
            panic!("expected one group");
        };
        let mut names = Vec::new();
        group_binder_names(group, &mut names);
        assert_eq!(names, ["a", "b", "c"]);
    }

    #[test]
    fn colon_index_ignores_definitions() {
        assert_eq!(colon_index(&items("x := 1")), None);
        assert_eq!(colon_index(&items("x : T")), Some(1));
    }

    #[test]
    fn instance_key_lists_the_names() {
        assert_eq!(instance_key(&items("Good a")), "[Good a]");
    }
}
