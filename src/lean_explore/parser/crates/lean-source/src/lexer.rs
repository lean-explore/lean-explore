//! Lean 4 lexer over a byte slice.
//!
//! The lexer follows `Lean/Parser/Basic.lean`: nested block comments, doc and module-doc
//! comments as tokens, string, raw string, interpolated string, character, name and number
//! literals, and identifiers with `.`-separated and `«»`-escaped components. Ordinary comments
//! and whitespace produce no tokens. It needs no token table: keywords are identifiers, and
//! symbols are split only as far as the declaration scanner needs.

use crate::chars::{
    ASCII, DIGIT, ID_FIRST, ID_REST, decode, is_id_first_wide, is_id_rest_wide, utf8_len,
};
use memchr::{memchr, memchr2, memchr3, memrchr};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u8)]
pub enum Kind {
    /// Identifier or keyword, possibly dotted.
    Ident,
    /// `#check`, `#guard_msgs` and other `#`-commands.
    Hash,
    /// `` `name `` literal.
    NameLit,
    Str,
    Char,
    Num,
    /// `/-- ... -/`
    Doc,
    /// `/-! ... -/`
    ModDoc,
    /// `(`, `[`, `{`, `⟨`, `⦃`, `⟦`, `⟪`, `⟮`, `⦋`, `⁅`, `‹`, `⌜`, `@[`
    Open,
    /// `)`, `]`, `}`, `⟩`, `⦄`, `⟧`, `⟫`, `⟯`, `⦌`, `⁆`, `›`, `⌝`
    Close,
    /// `:=`
    Assign,
    /// `:`
    Colon,
    /// `::`
    ColonColon,
    /// `|`
    Bar,
    /// `=>`
    FatArrow,
    Comma,
    /// `.`
    Dot,
    /// Any other symbol.
    Sym,
}

/// One token. Offsets are bytes into the source.
#[derive(Clone, Copy, Debug)]
pub struct Token {
    pub start: u32,
    pub end: u32,
    /// Byte column of `start` within its line.
    pub col: u32,
    pub kind: Kind,
    /// First token on its line.
    pub lead: bool,
    /// For `Open`: true for `@[`.
    pub attr: bool,
}

impl Token {
    #[inline]
    pub fn text<'a>(&self, src: &'a [u8]) -> &'a [u8] {
        &src[self.start as usize..self.end as usize]
    }
}

/// Lexes `src`, appending tokens to `out`.
pub fn lex(src: &[u8], out: &mut Vec<Token>) {
    let mut lx = Lexer {
        b: src,
        i: 0,
        line_start: 0,
        lead: true,
        interp_pending: false,
        out,
    };
    lx.run();
}

struct Lexer<'a, 'o> {
    b: &'a [u8],
    i: usize,
    line_start: usize,
    lead: bool,
    /// A string literal expected next is interpolated (`throwErrorAt`, `trace[...]`).
    interp_pending: bool,
    out: &'o mut Vec<Token>,
}

impl<'a, 'o> Lexer<'a, 'o> {
    #[inline]
    fn at(&self, j: usize) -> u8 {
        if j < self.b.len() { self.b[j] } else { 0 }
    }

    #[inline]
    fn push(&mut self, start: usize, end: usize, kind: Kind) {
        self.out.push(Token {
            start: start as u32,
            end: end as u32,
            col: (start - self.line_start) as u32,
            kind,
            lead: self.lead,
            attr: false,
        });
        self.lead = false;
    }

    /// Records the newlines inside `[start, end)` for tokens and comments spanning lines.
    #[inline]
    fn cover(&mut self, start: usize, end: usize) {
        if let Some(p) = memrchr(b'\n', &self.b[start..end]) {
            self.line_start = start + p + 1;
        }
    }

    fn run(&mut self) {
        let b = self.b;
        let n = b.len();
        while self.i < n {
            let c = b[self.i];
            match c {
                b'\n' => {
                    self.i += 1;
                    self.line_start = self.i;
                    self.lead = true;
                }
                b' ' | b'\t' | b'\r' => {
                    self.i += 1;
                    while self.i < n && matches!(b[self.i], b' ' | b'\t' | b'\r') {
                        self.i += 1;
                    }
                }
                b'-' if self.at(self.i + 1) == b'-' => {
                    self.i = match memchr(b'\n', &b[self.i..]) {
                        Some(p) => self.i + p,
                        None => n,
                    };
                }
                b'/' if self.at(self.i + 1) == b'-' => {
                    let start = self.i;
                    let k = self.at(self.i + 2);
                    let end = comment_end(b, self.i + 2);
                    if k == b'-' || k == b'!' {
                        self.push(start, end, if k == b'-' { Kind::Doc } else { Kind::ModDoc });
                    }
                    self.cover(start, end);
                    self.i = end;
                }
                b'"' => {
                    let start = self.i;
                    let interp = self.interp_pending || self.prev_wants_interp();
                    self.interp_pending = false;
                    let end = if interp {
                        interp_str_end(b, start + 1)
                    } else {
                        str_end(b, start + 1)
                    };
                    self.push(start, end, Kind::Str);
                    self.cover(start, end);
                    self.i = end;
                }
                b'\'' if self.at(self.i + 1) != b'\'' => {
                    let start = self.i;
                    match char_end(b, start + 1) {
                        Some(end) => {
                            self.push(start, end, Kind::Char);
                            self.i = end;
                        }
                        None => {
                            self.push(start, start + 1, Kind::Sym);
                            self.i += 1;
                        }
                    }
                }
                b'`' if crate::chars::starts_ident(b, self.i + 1) => {
                    let start = self.i;
                    let end = ident_end(b, start + 1);
                    self.push(start, end, Kind::NameLit);
                    self.i = end;
                }
                b'r' if raw_str_start(b, self.i + 1) => {
                    let start = self.i;
                    let end = raw_str_end(b, start + 1);
                    self.push(start, end, Kind::Str);
                    self.cover(start, end);
                    self.i = end;
                }
                b'0'..=b'9' => {
                    let start = self.i;
                    let end = number_end(b, start);
                    self.push(start, end, Kind::Num);
                    self.i = end;
                }
                b'#' if crate::chars::starts_ident(b, self.i + 1) => {
                    let start = self.i;
                    let mut j = start + 1;
                    while j < n {
                        let x = b[j];
                        if x < 0x80 {
                            if ASCII[x as usize] & ID_REST == 0 {
                                break;
                            }
                            j += 1;
                        } else {
                            let (cp, l) = decode(b, j);
                            if !is_id_rest_wide(cp) {
                                break;
                            }
                            j += l;
                        }
                    }
                    self.push(start, j, Kind::Hash);
                    self.i = j;
                }
                b'@' if self.at(self.i + 1) == b'[' => {
                    let start = self.i;
                    self.push(start, start + 2, Kind::Open);
                    self.out.last_mut().unwrap().attr = true;
                    self.i += 2;
                }
                b'(' | b'[' | b'{' => {
                    let start = self.i;
                    self.push(start, start + 1, Kind::Open);
                    self.i += 1;
                }
                b')' | b']' | b'}' => {
                    let start = self.i;
                    self.push(start, start + 1, Kind::Close);
                    self.i += 1;
                }
                b':' => {
                    let start = self.i;
                    match self.at(start + 1) {
                        b'=' => {
                            self.push(start, start + 2, Kind::Assign);
                            self.i += 2;
                        }
                        b':' => {
                            self.push(start, start + 2, Kind::ColonColon);
                            self.i += 2;
                        }
                        _ => {
                            self.push(start, start + 1, Kind::Colon);
                            self.i += 1;
                        }
                    }
                }
                b'=' if self.at(self.i + 1) == b'>' => {
                    let start = self.i;
                    self.push(start, start + 2, Kind::FatArrow);
                    self.i += 2;
                }
                b'|' => {
                    let start = self.i;
                    let mut j = start + 1;
                    while self.at(j) == b'|' {
                        j += 1;
                    }
                    if j == start + 1 && self.at(j) != b'>' {
                        self.push(start, j, Kind::Bar);
                    } else {
                        if self.at(j) == b'>' {
                            j += 1;
                            if self.at(j) == b'.' {
                                j += 1;
                            }
                        }
                        self.push(start, j, Kind::Sym);
                    }
                    self.i = j;
                }
                b'<' if self.at(self.i + 1) == b'|' => {
                    let start = self.i;
                    let end = if self.at(start + 2) == b'>' {
                        start + 3
                    } else {
                        start + 2
                    };
                    self.push(start, end, Kind::Sym);
                    self.i = end;
                }
                b',' => {
                    let start = self.i;
                    self.push(start, start + 1, Kind::Comma);
                    self.i += 1;
                }
                b'.' => {
                    let start = self.i;
                    if self.at(start + 1) == b'.' {
                        let mut j = start + 2;
                        while self.at(j) == b'.' {
                            j += 1;
                        }
                        self.push(start, j, Kind::Sym);
                        self.i = j;
                    } else {
                        self.push(start, start + 1, Kind::Dot);
                        self.i += 1;
                    }
                }
                _ if c < 0x80 => {
                    let start = self.i;
                    if ASCII[c as usize] & ID_FIRST != 0 {
                        let end = ident_end(b, start);
                        self.push(start, end, Kind::Ident);
                        self.i = end;
                        self.after_ident(start, end);
                    } else {
                        self.push(start, start + 1, Kind::Sym);
                        self.i += 1;
                    }
                }
                _ => {
                    let start = self.i;
                    let (cp, l) = decode(b, start);
                    if cp == 0xab || is_id_first_wide(cp) {
                        let end = ident_end(b, start);
                        self.push(start, end, Kind::Ident);
                        self.i = end;
                    } else {
                        let kind = match cp {
                            0x27e8 | 0x2983 | 0x27e6 | 0x27ea | 0x27ee | 0x298b | 0x2045
                            | 0x2039 | 0x231c => Kind::Open,
                            0x27e9 | 0x2984 | 0x27e7 | 0x27eb | 0x27ef | 0x298c | 0x2046
                            | 0x203a | 0x231d => Kind::Close,
                            _ => Kind::Sym,
                        };
                        self.push(start, start + l, kind);
                        self.i += l;
                    }
                }
            }
        }
    }

    /// True if the previous token takes an interpolated string (`s!`, `m!`, `throwError`, ...).
    #[inline]
    fn prev_wants_interp(&self) -> bool {
        let Some(t) = self.out.last() else {
            return false;
        };
        if t.kind != Kind::Ident {
            return false;
        }
        matches!(
            t.text(self.b),
            b"s!"
                | b"m!"
                | b"f!"
                | b"println!"
                | b"throwError"
                | b"reportIssue!"
                | b"reportEMatchIssue!"
                | b"reportDbgIssue!"
        )
    }

    #[inline]
    fn after_ident(&mut self, start: usize, end: usize) {
        let t = &self.b[start..end];
        if t == b"throwErrorAt"
            || (self.at(end) == b'['
                && (t == b"trace" || t == b"trace_goal" || t == b"Macro.trace"))
        {
            self.interp_pending = true;
        }
    }
}

/// End of a block comment whose body starts at `i` (just after `/-`), honouring nesting.
#[inline]
pub fn comment_end(b: &[u8], mut i: usize) -> usize {
    let n = b.len();
    let mut depth = 1usize;
    while i < n {
        match memchr2(b'-', b'/', &b[i..]) {
            None => return n,
            Some(p) => {
                let j = i + p;
                if b[j] == b'-' {
                    if j + 1 < n && b[j + 1] == b'/' {
                        depth -= 1;
                        if depth == 0 {
                            return j + 2;
                        }
                        i = j + 2;
                    } else {
                        i = j + 1;
                    }
                } else if j + 1 < n && b[j + 1] == b'-' {
                    depth += 1;
                    i = j + 2;
                } else {
                    i = j + 1;
                }
            }
        }
    }
    n
}

/// End of a string literal whose body starts at `i` (just after the opening quote).
#[inline]
pub fn str_end(b: &[u8], mut i: usize) -> usize {
    let n = b.len();
    while i < n {
        match memchr2(b'"', b'\\', &b[i..]) {
            None => return n,
            Some(p) => {
                let j = i + p;
                if b[j] == b'"' {
                    return j + 1;
                }
                i = j + 1 + if j + 1 < n { utf8_len(b[j + 1]) } else { 0 };
            }
        }
    }
    n
}

/// End of an interpolated string literal: `{ ... }` holds terms that may contain strings.
pub fn interp_str_end(b: &[u8], mut i: usize) -> usize {
    let n = b.len();
    while i < n {
        match memchr3(b'"', b'\\', b'{', &b[i..]) {
            None => return n,
            Some(p) => {
                let j = i + p;
                match b[j] {
                    b'"' => return j + 1,
                    b'\\' => i = j + 1 + if j + 1 < n { utf8_len(b[j + 1]) } else { 0 },
                    _ => i = term_in_braces_end(b, j + 1),
                }
            }
        }
    }
    n
}

/// End of a term inside `{ ... }` of an interpolated string, just after the closing brace.
fn term_in_braces_end(b: &[u8], mut i: usize) -> usize {
    let n = b.len();
    let mut depth = 1usize;
    while i < n {
        // Identifiers go whole, so a prime in `open'` does not start a character literal.
        if crate::chars::starts_ident(b, i) && b[i] != b'_'
            || (b[i] == b'_' && i + 1 < n && crate::chars::starts_ident(b, i + 1))
        {
            i = ident_end(b, i);
            continue;
        }
        match b[i] {
            b'{' => {
                depth += 1;
                i += 1;
            }
            b'}' => {
                depth -= 1;
                i += 1;
                if depth == 0 {
                    return i;
                }
            }
            b'"' => i = str_end(b, i + 1),
            b'\'' if i + 1 < n && b[i + 1] != b'\'' => i = char_end(b, i + 1).unwrap_or(i + 1),
            b'-' if i + 1 < n && b[i + 1] == b'-' => {
                i = memchr(b'\n', &b[i..]).map_or(n, |p| i + p);
            }
            b'/' if i + 1 < n && b[i + 1] == b'-' => i = comment_end(b, i + 2),
            b'\n' => return i,
            _ => i += 1,
        }
    }
    n
}

/// End of a character literal whose body starts at `i`, if it is one.
#[inline]
pub fn char_end(b: &[u8], i: usize) -> Option<usize> {
    let n = b.len();
    if i >= n {
        return None;
    }
    let mut j = i;
    if b[j] == b'\\' {
        j += 1;
        if j >= n {
            return None;
        }
        j += match b[j] {
            b'x' => 3,
            b'u' => 5,
            x => utf8_len(x),
        };
    } else {
        if b[j] == b'\n' {
            return None;
        }
        j += utf8_len(b[j]);
    }
    if j < n && b[j] == b'\'' {
        Some(j + 1)
    } else {
        None
    }
}

/// True if a raw string literal starts after an `r` at `i - 1`.
#[inline]
fn raw_str_start(b: &[u8], mut i: usize) -> bool {
    while i < b.len() && b[i] == b'#' {
        i += 1;
    }
    i < b.len() && b[i] == b'"'
}

/// End of a raw string literal; `i` is just after the `r`.
fn raw_str_end(b: &[u8], mut i: usize) -> usize {
    let n = b.len();
    let mut hashes = 0;
    while i < n && b[i] == b'#' {
        hashes += 1;
        i += 1;
    }
    i += 1;
    while i < n {
        match memchr(b'"', &b[i..]) {
            None => return n,
            Some(p) => {
                let j = i + p + 1;
                let mut k = 0;
                while k < hashes && j + k < n && b[j + k] == b'#' {
                    k += 1;
                }
                if k == hashes {
                    return j + k;
                }
                i = j;
            }
        }
    }
    n
}

/// End of a numeric literal starting at `i`.
#[inline]
fn number_end(b: &[u8], i: usize) -> usize {
    let n = b.len();
    let mut j = i + 1;
    let alnum = |x: u8| x.is_ascii_alphanumeric() || x == b'_';
    while j < n && alnum(b[j]) {
        j += 1;
    }
    if j + 1 < n && b[j] == b'.' && b[j + 1].is_ascii_digit() {
        j += 1;
        while j < n && alnum(b[j]) {
            j += 1;
        }
    }
    if j + 1 < n
        && (b[j - 1] == b'e' || b[j - 1] == b'E')
        && (b[j] == b'-' || b[j] == b'+')
        && b[j + 1].is_ascii_digit()
    {
        j += 1;
        while j < n && b[j].is_ascii_digit() {
            j += 1;
        }
    }
    j
}

/// 1 for ASCII bytes that continue an identifier, 0 otherwise (all bytes from 0x80 included).
static ID_REST_BYTE: [u8; 256] = {
    let mut t = [0u8; 256];
    let mut i = 0;
    while i < 128 {
        if ASCII[i] & ID_REST != 0 {
            t[i] = 1;
        }
        i += 1;
    }
    t
};

/// End of an identifier starting at `i` (`«`-escaped and dotted components included).
#[inline]
pub fn ident_end(b: &[u8], mut i: usize) -> usize {
    let n = b.len();
    loop {
        if i + 1 < n && b[i] == 0xc2 && b[i + 1] == 0xab {
            // `«...»`
            let mut j = i + 2;
            loop {
                match memchr(0xbb, &b[j..]) {
                    None => return n,
                    Some(p) => {
                        let k = j + p;
                        if k > 0 && b[k - 1] == 0xc2 {
                            i = k + 1;
                            break;
                        }
                        j = k + 1;
                    }
                }
            }
        } else {
            i += utf8_len(b[i]);
            while i < n {
                let x = b[i];
                if ID_REST_BYTE[x as usize] != 0 {
                    i += 1;
                    continue;
                }
                if x < 0x80 {
                    break;
                }
                let (cp, l) = decode(b, i);
                if !is_id_rest_wide(cp) {
                    break;
                }
                i += l;
            }
        }
        if i + 1 < n && b[i] == b'.' && crate::chars::starts_ident(b, i + 1) {
            i += 1;
            continue;
        }
        return i;
    }
}

#[allow(dead_code)]
#[inline]
fn is_digit(x: u8) -> bool {
    ASCII[x as usize & 0x7f] & DIGIT != 0 && x < 0x80
}

#[cfg(test)]
mod tests {
    use super::*;

    fn kinds(s: &str) -> Vec<(Kind, String)> {
        let mut v = Vec::new();
        lex(s.as_bytes(), &mut v);
        v.iter()
            .map(|t| (t.kind, s[t.start as usize..t.end as usize].to_string()))
            .collect()
    }

    #[test]
    fn comments_and_docs() {
        let k = kinds("a /- x /- y -/ z -/ b -- c\n/-- d -/ /-! e -/ f");
        let texts: Vec<_> = k.iter().map(|x| x.1.as_str()).collect();
        assert_eq!(texts, ["a", "b", "/-- d -/", "/-! e -/", "f"]);
        assert_eq!(k[2].0, Kind::Doc);
        assert_eq!(k[3].0, Kind::ModDoc);
    }

    #[test]
    fn literals() {
        let k =
            kinds(r##"'a' '\n' 'λ' "s -- t" s!"x {f "y"} z" `foo.bar r#"q"q"# 0x1F 1.5e-3 h'"##);
        let texts: Vec<_> = k.iter().map(|x| x.1.as_str()).collect();
        assert_eq!(
            texts,
            [
                "'a'",
                "'\\n'",
                "'λ'",
                "\"s -- t\"",
                "s!",
                "\"x {f \"y\"} z\"",
                "`foo.bar",
                "r#\"q\"q\"#",
                "0x1F",
                "1.5e-3",
                "h'"
            ]
        );
    }

    #[test]
    fn interpolation_with_primes() {
        let k = kinds("s!\"'{x.f open'}' and '{y}'\" theorem");
        let texts: Vec<_> = k.iter().map(|x| x.1.as_str()).collect();
        assert_eq!(texts, ["s!", "\"'{x.f open'}' and '{y}'\"", "theorem"]);
    }

    #[test]
    fn identifiers() {
        let k = kinds("Nat.succ_le «term⟪_,_⟫».x α₁ ℕ h.1 x.{u}");
        let texts: Vec<_> = k.iter().map(|x| x.1.as_str()).collect();
        assert_eq!(
            texts,
            [
                "Nat.succ_le",
                "«term⟪_,_⟫».x",
                "α₁",
                "ℕ",
                "h",
                ".",
                "1",
                "x",
                ".",
                "{",
                "u",
                "}"
            ]
        );
    }

    #[test]
    fn symbols() {
        let k = kinds("a := b :: c | d || e |>. f <|> g => @[simp]");
        let ks: Vec<_> = k.iter().map(|x| x.0).collect();
        assert_eq!(
            ks,
            [
                Kind::Ident,
                Kind::Assign,
                Kind::Ident,
                Kind::ColonColon,
                Kind::Ident,
                Kind::Bar,
                Kind::Ident,
                Kind::Sym,
                Kind::Ident,
                Kind::Sym,
                Kind::Ident,
                Kind::Sym,
                Kind::Ident,
                Kind::FatArrow,
                Kind::Open,
                Kind::Ident,
                Kind::Close
            ]
        );
    }
}
