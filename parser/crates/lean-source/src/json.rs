//! JSON Lines output of parsed files.

use crate::keywords::Modifier;
use crate::pos::{LineIndex, Pos};
use crate::scan::{Parsed, Span};

/// Options for [`write_decls`].
#[derive(Clone, Copy, Debug, Default)]
pub struct JsonOptions {
    /// Include the source text of the signature, type, body and doc comment.
    pub text: bool,
}

/// Appends `s` to `out` as a JSON string literal.
pub fn push_str(out: &mut String, s: &str) {
    out.push('"');
    let b = s.as_bytes();
    let mut last = 0;
    for (i, &c) in b.iter().enumerate() {
        let esc: Option<&str> = match c {
            b'"' => Some("\\\""),
            b'\\' => Some("\\\\"),
            b'\n' => Some("\\n"),
            b'\r' => Some("\\r"),
            b'\t' => Some("\\t"),
            0..=0x1f => None,
            _ => continue,
        };
        out.push_str(&s[last..i]);
        match esc {
            Some(e) => out.push_str(e),
            None => out.push_str(&format!("\\u{:04x}", c)),
        }
        last = i + 1;
    }
    out.push_str(&s[last..]);
    out.push('"');
}

fn push_pos(out: &mut String, p: Pos) {
    use std::fmt::Write;
    let _ = write!(out, "{},{},{}", p.byte, p.line, p.col);
}

/// `[start_byte, start_line, start_col, end_byte, end_line, end_col]`.
fn push_span(out: &mut String, src: &[u8], idx: &LineIndex, s: Span) {
    out.push('[');
    push_pos(out, idx.pos(src, s.start));
    out.push(',');
    push_pos(out, idx.pos(src, s.end));
    out.push(']');
}

fn push_opt_span(out: &mut String, src: &[u8], idx: &LineIndex, s: Option<Span>) {
    match s {
        Some(s) => push_span(out, src, idx, s),
        None => out.push_str("null"),
    }
}

fn slice(src: &[u8], s: Span) -> &str {
    std::str::from_utf8(&src[s.start as usize..s.end as usize]).unwrap_or("")
}

/// Writes one JSON object per declaration of `parsed` to `out`.
pub fn write_decls(
    out: &mut String,
    file: &str,
    module: &str,
    src: &[u8],
    parsed: &Parsed,
    opts: JsonOptions,
) {
    use std::fmt::Write;
    let idx = LineIndex::new(src);
    for (i, d) in parsed.decls.iter().enumerate() {
        out.push_str("{\"file\":");
        push_str(out, file);
        out.push_str(",\"module\":");
        push_str(out, module);
        let _ = write!(
            out,
            ",\"index\":{i},\"kind\":\"{}\",\"name\":",
            d.kind.as_str()
        );
        match &d.name {
            Some(n) => push_str(out, n),
            None => out.push_str("null"),
        }
        let _ = write!(
            out,
            ",\"generated\":{},\"approximate\":{}",
            d.generated, d.approximate
        );
        out.push_str(",\"origin\":");
        match d.origin {
            Some(s) => push_str(out, slice(src, s)),
            None => out.push_str("null"),
        }
        out.push_str(",\"parent\":");
        match d.parent {
            Some(p) => {
                let _ = write!(out, "{p}");
            }
            None => out.push_str("null"),
        }
        out.push_str(",\"modifiers\":[");
        let mut first = true;
        for m in Modifier::ALL {
            if d.modifiers & m.bit() != 0 {
                if !first {
                    out.push(',');
                }
                first = false;
                let _ = write!(out, "\"{}\"", m.name());
            }
        }
        out.push_str("],\"attributes\":[");
        for (k, a) in parsed.attrs[d.attrs.0 as usize..d.attrs.1 as usize]
            .iter()
            .enumerate()
        {
            if k > 0 {
                out.push(',');
            }
            push_str(out, slice(src, a.span));
        }
        out.push_str("],\"range\":");
        push_span(out, src, &idx, d.range);
        out.push_str(",\"selection\":");
        push_span(out, src, &idx, d.selection);
        out.push_str(",\"signature\":");
        push_span(out, src, &idx, d.signature);
        out.push_str(",\"type\":");
        push_opt_span(out, src, &idx, d.type_);
        out.push_str(",\"body\":");
        push_opt_span(out, src, &idx, d.body);
        out.push_str(",\"doc\":");
        push_opt_span(out, src, &idx, d.doc);
        if opts.text {
            out.push_str(",\"signature_text\":");
            push_str(out, slice(src, d.signature));
            out.push_str(",\"doc_text\":");
            match d.doc {
                Some(s) => push_str(out, slice(src, s)),
                None => out.push_str("null"),
            }
        }
        out.push_str("}\n");
    }
}

/// Writes one JSON object per command of `parsed` to `out`: its kind, range, head keyword and
/// the namespace in effect.
pub fn write_commands(out: &mut String, file: &str, module: &str, src: &[u8], parsed: &Parsed) {
    let idx = LineIndex::new(src);
    for c in &parsed.commands {
        out.push_str("{\"file\":");
        push_str(out, file);
        out.push_str(",\"module\":");
        push_str(out, module);
        out.push_str(",\"command\":\"");
        out.push_str(c.kind.as_str());
        out.push_str("\",\"head\":");
        push_str(out, slice(src, c.head));
        out.push_str(",\"namespace\":");
        push_str(out, &parsed.namespaces[c.namespace as usize]);
        out.push_str(",\"range\":");
        push_span(out, src, &idx, c.range);
        out.push_str("}\n");
    }
}
