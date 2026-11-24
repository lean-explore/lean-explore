//! Character classes of the Lean 4 lexer.
//!
//! These follow `Init/Meta/Defs.lean` in Lean v4.35.0 (`isLetterLike`, `isSubScriptAlnum`,
//! `isIdFirst`, `isIdRest`).

/// `Lean.isLetterLike`.
#[inline]
pub fn is_letter_like(c: u32) -> bool {
    (0x3b1..=0x3c9).contains(&c) && c != 0x3bb
        || (0x391..=0x3a9).contains(&c) && c != 0x3a0 && c != 0x3a3
        || (0x3ca..=0x3fb).contains(&c)
        || (0x1f00..=0x1ffe).contains(&c)
        || (0x2100..=0x214f).contains(&c)
        || (0x1d49c..=0x1d59f).contains(&c)
        || (0x00c0..=0x00ff).contains(&c) && c != 0x00d7 && c != 0x00f7
        || (0x0100..=0x017f).contains(&c)
}

/// `Lean.isSubScriptAlnum`.
#[inline]
pub fn is_subscript_alnum(c: u32) -> bool {
    (0x2080..=0x2089).contains(&c)
        || (0x2090..=0x209c).contains(&c)
        || (0x1d62..=0x1d6a).contains(&c)
        || c == 0x2c7c
}

/// `Lean.isIdFirst` for a non-ASCII code point.
#[inline]
pub fn is_id_first_wide(c: u32) -> bool {
    is_letter_like(c)
}

/// `Lean.isIdRest` for a non-ASCII code point.
#[inline]
pub fn is_id_rest_wide(c: u32) -> bool {
    is_letter_like(c) || is_subscript_alnum(c)
}

pub const ID_FIRST: u8 = 1;
pub const ID_REST: u8 = 2;
pub const DIGIT: u8 = 4;
pub const SPACE: u8 = 8;

/// Classes of ASCII bytes.
pub static ASCII: [u8; 128] = {
    let mut t = [0u8; 128];
    let mut i = 0;
    while i < 128 {
        let b = i as u8;
        let mut v = 0;
        if b.is_ascii_alphabetic() || b == b'_' {
            v |= ID_FIRST | ID_REST;
        }
        if b.is_ascii_digit() {
            v |= DIGIT | ID_REST;
        }
        if b == b'\'' || b == b'!' || b == b'?' {
            v |= ID_REST;
        }
        if b == b' ' || b == b'\t' || b == b'\r' || b == b'\n' {
            v |= SPACE;
        }
        t[i] = v;
        i += 1;
    }
    t
};

/// Decodes the UTF-8 code point starting at `i`. Returns the code point and its length.
/// Invalid bytes decode as U+FFFD with length 1.
#[inline]
pub fn decode(b: &[u8], i: usize) -> (u32, usize) {
    let b0 = b[i] as u32;
    if b0 < 0x80 {
        return (b0, 1);
    }
    let n = b.len();
    if b0 >= 0xf0 && i + 3 < n {
        let c = ((b0 & 0x07) << 18)
            | ((b[i + 1] as u32 & 0x3f) << 12)
            | ((b[i + 2] as u32 & 0x3f) << 6)
            | (b[i + 3] as u32 & 0x3f);
        return (c, 4);
    }
    if b0 >= 0xe0 && i + 2 < n {
        let c = ((b0 & 0x0f) << 12) | ((b[i + 1] as u32 & 0x3f) << 6) | (b[i + 2] as u32 & 0x3f);
        return (c, 3);
    }
    if b0 >= 0xc0 && i + 1 < n {
        let c = ((b0 & 0x1f) << 6) | (b[i + 1] as u32 & 0x3f);
        return (c, 2);
    }
    (0xfffd, 1)
}

/// Length in bytes of the UTF-8 sequence that starts with `b0`.
#[inline]
pub fn utf8_len(b0: u8) -> usize {
    if b0 < 0x80 {
        1
    } else if b0 >= 0xf0 {
        4
    } else if b0 >= 0xe0 {
        3
    } else if b0 >= 0xc0 {
        2
    } else {
        1
    }
}

/// True if the code point at `i` can start an identifier (including `«`).
#[inline]
pub fn starts_ident(b: &[u8], i: usize) -> bool {
    if i >= b.len() {
        return false;
    }
    let b0 = b[i];
    if b0 < 0x80 {
        return ASCII[b0 as usize] & ID_FIRST != 0;
    }
    let (c, _) = decode(b, i);
    c == 0xab || is_id_first_wide(c)
}
