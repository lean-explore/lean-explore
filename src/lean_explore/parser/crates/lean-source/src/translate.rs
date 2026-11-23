//! Port of Mathlib's name translation for `to_additive` and `to_dual`
//! (Mathlib/Tactic/Translate/GuessName.lean and `targetName` in Core.lean).
//!
//! `guess_name` turns one name component into its translation, word by word through a
//! dictionary and then through a table of abbreviations. `target_name` combines it with the
//! namespace rule Mathlib applies to a name written in the attribute.

use crate::translate_tables as tables;
use std::collections::HashMap;

/// The dictionaries of one attribute, plus hints added in the current file.
#[derive(Clone, Debug)]
pub struct Dict {
    names: HashMap<String, Vec<String>>,
    abbreviations: HashMap<String, String>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Attr {
    Additive,
    Dual,
}

impl Dict {
    pub fn new(attr: Attr) -> Dict {
        let (names, abbreviations) = match attr {
            Attr::Additive => (tables::ADDITIVE_NAMES, tables::ADDITIVE_ABBREVIATIONS),
            Attr::Dual => (tables::DUAL_NAMES, tables::DUAL_ABBREVIATIONS),
        };
        Dict {
            names: names
                .iter()
                .map(|(k, v)| (k.to_string(), v.iter().map(|s| s.to_string()).collect()))
                .collect(),
            abbreviations: abbreviations
                .iter()
                .map(|(k, v)| (k.to_string(), v.to_string()))
                .collect(),
        }
    }

    /// Mathlib's dictionary for `to_additive` (`k == 0`) or `to_dual` (`k == 1`), built once.
    pub fn shared(k: usize) -> &'static Dict {
        static DICTS: std::sync::OnceLock<[Dict; 2]> = std::sync::OnceLock::new();
        &DICTS.get_or_init(|| [Dict::new(Attr::Additive), Dict::new(Attr::Dual)])[k]
    }

    /// `GuessNameExt.addTranslation`: what `to_additive_name_hint src tgt` does.
    pub fn add_hint(&mut self, src: &str, tgt: &str) {
        let src = decapitalize_seq(src);
        if split_case(&src).len() == 1 {
            self.names.insert(src, split_case(tgt));
        } else {
            self.abbreviations.insert(src, tgt.to_string());
        }
    }
}

#[inline]
fn is_upper(c: char) -> bool {
    c.is_ascii_uppercase()
}

/// First character, or Lean's default character `'A'` for the empty string.
#[inline]
fn first(s: &str) -> char {
    s.chars().next().unwrap_or('A')
}

/// `String.splitCase`: split at `_` (kept as its own part), after the capitalized names
/// `LE`, `LT`, `GE`, `GT`, `WF`, `Coe(TC|T|HTCT)`, and between a lower-case and an upper-case
/// letter.
pub fn split_case(s: &str) -> Vec<String> {
    const END_CAPITAL: &[(&str, &[&str])] = &[
        ("Coe", &["TC", "T", "HTCT"]),
        ("GE", &[""]),
        ("GT", &[""]),
        ("LE", &[""]),
        ("LT", &[""]),
        ("WF", &[""]),
    ];
    let mut out = Vec::new();
    let mut s: String = s.to_string();
    let mut i0 = 0usize; // byte offset of the current character
    loop {
        let c0 = s[i0..].chars().next();
        let i1 = i0 + c0.map_or(0, char::len_utf8);
        if i1 >= s.len() {
            out.push(s);
            return out;
        }
        let c1 = s[i1..].chars().next().unwrap();
        if c0 == Some('_') || c1 == '_' {
            out.push(s[..i1].to_string());
            s = s[i1..].to_string();
            i0 = 0;
            continue;
        }
        if is_upper(c1) {
            if let Some((_, strs)) = END_CAPITAL.iter().find(|(k, _)| *k == &s[..i1])
                && let Some(pref) = strs.iter().find(|x| s[i1..].starts_with(*x))
            {
                out.push(format!("{}{}", &s[..i1], pref));
                s = s[i1 + pref.len()..].to_string();
                i0 = 0;
                continue;
            }
            if !c0.is_some_and(is_upper) {
                out.push(s[..i1].to_string());
                s = s[i1..].to_string();
                i0 = 0;
                continue;
            }
        }
        i0 = i1;
    }
}

/// `String.decapitalizeSeq`: lower-case the leading upper-case letters.
pub fn decapitalize_seq(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut leading = true;
    for c in s.chars() {
        if leading && is_upper(c) {
            out.push(c.to_ascii_lowercase());
        } else {
            leading = false;
            out.push(c);
        }
    }
    out
}

fn decapitalize_like(r: &str, s: &str) -> String {
    if is_upper(first(r)) {
        s.to_string()
    } else {
        decapitalize_seq(s)
    }
}

fn apply_name_dict(d: &Dict, parts: &[String]) -> Vec<String> {
    let mut out = Vec::new();
    for x in parts {
        match d.names.get(&x.to_ascii_lowercase()) {
            Some(y) => {
                for (k, part) in y.iter().enumerate() {
                    out.push(if k == 0 {
                        decapitalize_like(x, part)
                    } else {
                        part.clone()
                    });
                }
            }
            None => out.push(x.clone()),
        }
    }
    out
}

/// `fixAbbreviationAux`.
fn fix_abbreviation_aux(d: &Dict, l: &[String], s: Vec<String>) -> String {
    match l.split_first() {
        None => match s.split_first() {
            None => String::new(),
            Some((first, rest)) => first.clone() + &fix_abbreviation_aux(d, rest, Vec::new()),
        },
        Some((pre, rest)) => {
            let mut s = s;
            s.push(pre.clone());
            let t = s.concat();
            if pre == "_" && is_upper(first(&t)) {
                let head = s[0].clone();
                let mut next: Vec<String> = s[1..].to_vec();
                next.extend_from_slice(rest);
                head + &fix_abbreviation_aux(d, &next, Vec::new())
            } else {
                match d.abbreviations.get(&decapitalize_seq(&t)) {
                    Some(post) => {
                        decapitalize_like(&t, post) + &fix_abbreviation_aux(d, rest, Vec::new())
                    }
                    None => fix_abbreviation_aux(d, rest, s),
                }
            }
        }
    }
}

/// `guessName`: the translation of one name component.
pub fn guess_name(d: &Dict, s: &str) -> String {
    s.split('\'')
        .map(|tok| fix_abbreviation_aux(d, &apply_name_dict(d, &split_case(tok)), Vec::new()))
        .collect::<Vec<_>>()
        .join("'")
}

/// The name a translation attribute gives to a declaration whose last name component is
/// `last` and whose namespace translates to `ns`, following Mathlib's `targetName`. `target` is
/// the name written in the attribute, if any: `_root_.x` is absolute; otherwise the written
/// name replaces as many trailing namespace components as it has components beyond its last.
pub fn target_name(last: &str, ns: &[String], target: Option<&str>, d: &Dict) -> String {
    match target {
        Some(t) if t.starts_with("_root_.") => t["_root_.".len()..].to_string(),
        Some(t) => {
            let n = crate::scan::name_components(t).len();
            let keep = ns.len().saturating_sub(n.saturating_sub(1));
            let mut out = ns[..keep].join(".");
            if !out.is_empty() {
                out.push('.');
            }
            out.push_str(t);
            out
        }
        None => {
            let mut out = ns.join(".");
            if !out.is_empty() {
                out.push('.');
            }
            out.push_str(&guess_name(d, last));
            out
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn split() {
        assert_eq!(
            split_case("InvHMulLEConjugate₂SMul_ne_top"),
            [
                "Inv",
                "HMul",
                "LE",
                "Conjugate₂",
                "SMul",
                "_",
                "ne",
                "_",
                "top"
            ]
        );
        assert_eq!(split_case("CoeTC_foo"), ["CoeTC", "_", "foo"]);
    }

    #[test]
    fn abbreviations_are_found_after_unmatched_prefixes() {
        let dual = Dict::new(Attr::Dual);
        assert_eq!(guess_name(&dual, "instWellFoundedLT"), "instWellFoundedGT");
        assert_eq!(guess_name(&dual, "instWellFoundedGT"), "instWellFoundedLT");
        let mut custom = Dict::new(Attr::Dual);
        custom.add_hint("TwoWords", "Replacement");
        assert_eq!(
            guess_name(&custom, "beforeTwoWordsAfter"),
            "beforeReplacementAfter"
        );
        assert_eq!(
            guess_name(&custom, "beforeTwoWordsTwoWords"),
            "beforeReplacementReplacement"
        );
    }

    #[test]
    fn additive_names() {
        let d = Dict::new(Attr::Additive);
        for (m, a) in [
            ("mul_comm", "add_comm"),
            ("MulEquiv", "AddEquiv"),
            ("Subgroup", "AddSubgroup"),
            ("prod_mul_distrib", "sum_add_distrib"),
            ("inv_inv", "neg_neg"),
            ("mul_one'", "add_zero'"),
        ] {
            assert_eq!(guess_name(&d, m), a, "{m}");
        }
    }
}
