//! Command segmentation and declaration extraction.
//!
//! The scanner works on the token stream in two passes. The first pass splits the file into
//! commands. A command starts at a keyword that cannot occur inside a term (`theorem`, `def`,
//! `namespace`, `attribute`, ...) at bracket depth zero, at the doc comment, attributes and
//! modifiers in front of such a keyword, after the `in` of an `open ... in` style prefix, or at a
//! keyword or unknown command that begins a line at command indentation after a complete term.
//! The second pass reads each command: it tracks `namespace`, `section` and `end`, and for a
//! declaration it records the modifiers, the name, and the spans of the signature, type and
//! body, together with the nested declarations that the source names (structure fields,
//! constructors, `where` declarations) and those whose names Lean derives deterministically
//! (parent projections, default constructors).

#![allow(clippy::needless_range_loop)] // Token ranges read clearer as index loops.

use crate::keywords::{DeclKw, Kw, Modifier, ScopeKw, classify, expects_more, is_tactic_word};
use crate::lexer::{Kind, Token, lex};

/// Searchers for `where` and `rec`, built once.
fn finders() -> &'static (
    memchr::memmem::Finder<'static>,
    memchr::memmem::Finder<'static>,
) {
    static F: std::sync::OnceLock<(
        memchr::memmem::Finder<'static>,
        memchr::memmem::Finder<'static>,
    )> = std::sync::OnceLock::new();
    F.get_or_init(|| {
        (
            memchr::memmem::Finder::new(b"where"),
            memchr::memmem::Finder::new(b"rec"),
        )
    })
}

/// Byte span `[start, end)`.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Span {
    pub start: u32,
    pub end: u32,
}

impl Span {
    #[inline]
    pub fn new(start: u32, end: u32) -> Self {
        Span { start, end }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DeclKind {
    Theorem,
    Lemma,
    Def,
    Abbrev,
    Opaque,
    Axiom,
    Example,
    Instance,
    Structure,
    Class,
    ClassInductive,
    ClassAbbrev,
    Inductive,
    Coinductive,
    IrreducibleDef,
    Alias,
    ProofWanted,
    Initialize,
    RegisterOption,
    Syntax,
    Notation,
    Macro,
    Elab,
    Simproc,
    /// A constructor written in an `inductive` or after `::` in a `structure`.
    Constructor,
    /// A structure field, which Lean turns into a projection.
    Field,
    /// A `where` declaration of a definition (`foo.go`).
    WhereDecl,
    /// `toParent` projection of a structure, named by Lean from the `extends` clause.
    ParentProjection,
    /// A `let rec` declaration inside a value (`foo.go`).
    LetRec,
    /// `declare_config_elab`.
    ConfigElab,
    UnifHint,
    SyntaxCategory,
}

impl DeclKind {
    pub fn as_str(self) -> &'static str {
        use DeclKind::*;
        match self {
            Theorem => "theorem",
            Lemma => "lemma",
            Def => "def",
            Abbrev => "abbrev",
            Opaque => "opaque",
            Axiom => "axiom",
            Example => "example",
            Instance => "instance",
            Structure => "structure",
            Class => "class",
            ClassInductive => "class_inductive",
            ClassAbbrev => "class_abbrev",
            Inductive => "inductive",
            Coinductive => "coinductive",
            IrreducibleDef => "irreducible_def",
            Alias => "alias",
            ProofWanted => "proof_wanted",
            Initialize => "initialize",
            RegisterOption => "register_option",
            Syntax => "syntax",
            Notation => "notation",
            Macro => "macro",
            Elab => "elab",
            Simproc => "simproc",
            Constructor => "constructor",
            Field => "field",
            WhereDecl => "where_decl",
            ParentProjection => "parent_projection",
            LetRec => "let_rec",
            ConfigElab => "config_elab",
            UnifHint => "unif_hint",
            SyntaxCategory => "syntax_category",
        }
    }

    /// True if the declaration creates no constant (`example`, `proof_wanted`).
    pub fn is_virtual(self) -> bool {
        matches!(self, DeclKind::Example | DeclKind::ProofWanted)
    }
}

/// One attribute instance inside `@[...]`.
#[derive(Clone, Copy, Debug)]
pub struct Attr {
    /// The whole attribute instance, e.g. `to_additive (attr := simp)`.
    pub span: Span,
    /// The attribute name, e.g. `to_additive`.
    pub name: Span,
    /// Token indices of the instance, `[first, end)`.
    pub toks: (u32, u32),
}

#[derive(Clone, Debug)]
pub struct Decl {
    pub kind: DeclKind,
    /// Fully qualified name, without the `_private` prefix Lean adds to private names.
    pub name: Option<String>,
    /// True if Lean derives the name (parent projections, default constructors).
    pub generated: bool,
    /// The whole declaration, modifiers included: Lean's `DeclarationRanges.range`.
    pub range: Span,
    /// The name as written, or the keyword when there is none: Lean's `selectionRange`.
    pub selection: Span,
    /// The declaration keyword.
    pub keyword: Span,
    /// From the keyword to the end of the signature (binders and type).
    pub signature: Span,
    /// The type after the top-level `:`.
    pub type_: Option<Span>,
    /// The value, proof, fields or constructors, from `:=`, `|` or `where`.
    pub body: Option<Span>,
    pub doc: Option<Span>,
    /// Index range into `Parsed::attrs`.
    pub attrs: (u32, u32),
    /// Bit set of `Modifier::bit`.
    pub modifiers: u16,
    /// Index of the enclosing declaration for nested declarations.
    pub parent: Option<u32>,
    /// The attribute that generates this declaration (`to_additive`, `simps`, `ext`, ...).
    pub origin: Option<Span>,
    /// True if the name comes from a naming rule that may differ from Lean's result.
    pub approximate: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CommandKind {
    Declaration,
    Namespace,
    Section,
    End,
    Mutual,
    Open,
    Variable,
    Universe,
    SetOption,
    Attribute,
    Import,
    Module,
    ModuleDoc,
    Hash,
    Other,
}

impl CommandKind {
    pub fn as_str(self) -> &'static str {
        use CommandKind::*;
        match self {
            Declaration => "declaration",
            Namespace => "namespace",
            Section => "section",
            End => "end",
            Mutual => "mutual",
            Open => "open",
            Variable => "variable",
            Universe => "universe",
            SetOption => "set_option",
            Attribute => "attribute",
            Import => "import",
            Module => "module",
            ModuleDoc => "module_doc",
            Hash => "hash",
            Other => "other",
        }
    }
}

#[derive(Clone, Copy, Debug)]
pub struct Command {
    pub kind: CommandKind,
    pub range: Span,
    /// The keyword that determines the command.
    pub head: Span,
    /// Byte length of the namespace prefix in effect, into `Parsed::namespaces`.
    pub namespace: u32,
}

/// Result of parsing one file.
#[derive(Clone, Debug, Default)]
pub struct Parsed {
    pub decls: Vec<Decl>,
    pub commands: Vec<Command>,
    pub attrs: Vec<Attr>,
    /// Namespaces in effect at each command, deduplicated.
    pub namespaces: Vec<String>,
    /// Translations this file registers: `(0 for to_additive or 1 for to_dual, source, target)`.
    pub translations: Vec<(u8, String, String)>,
}

/// Parses a Lean source file.
pub fn parse(src: &[u8]) -> Parsed {
    thread_local! {
        // Reused across calls: a fresh buffer costs more in page faults than lexing.
        static TOKENS: std::cell::RefCell<Vec<Token>> = const { std::cell::RefCell::new(Vec::new()) };
    }
    TOKENS.with(|cell| match cell.try_borrow_mut() {
        Ok(mut toks) => parse_with(src, &mut toks),
        Err(_) => parse_with(src, &mut Vec::new()),
    })
}

/// Parses a file using `toks` as the token buffer.
pub fn parse_with(src: &[u8], toks: &mut Vec<Token>) -> Parsed {
    toks.clear();
    toks.reserve(src.len() / 5);
    lex(src, toks);
    parse_tokens(src, toks)
}

/// What other files of a project tell about names: the declarations they write and the
/// translations `to_additive` and `to_dual` register. Build it with [`Context::add`] from a
/// first parse of every file, then parse again with [`parse_in`].
#[derive(Clone, Debug, Default)]
pub struct Context {
    /// Names of declarations written in the project (not generated).
    pub declared: std::collections::HashSet<String>,
    /// Registered translations, `[to_additive, to_dual]`, source to target.
    pub translations: [std::collections::HashMap<String, String>; 2],
    /// Record the range of a `to_additive`/`to_dual` translation at the written name, as Mathlib
    /// does from November 2026 (master 3eae40f5), rather than at the attribute instance.
    pub translation_ranges_at_name: bool,
}

impl Context {
    /// The context of a project from a first parse of all its files. Only declared names that
    /// some file also generates are kept, since those are the ones that matter.
    pub fn build<'p>(
        files: impl IntoIterator<Item = &'p Parsed> + Clone,
        translation_ranges_at_name: bool,
    ) -> Context {
        let mut ctx = Context {
            translation_ranges_at_name,
            ..Default::default()
        };
        let mut generated = std::collections::HashSet::new();
        for p in files.clone() {
            for d in &p.decls {
                if let (true, Some(n)) = (d.generated && d.origin.is_some(), &d.name) {
                    generated.insert(n.as_str());
                }
            }
            for (k, src, tgt) in &p.translations {
                ctx.translations[*k as usize].insert(src.clone(), tgt.clone());
            }
        }
        for p in files {
            for d in &p.decls {
                if let (false, Some(n)) = (d.generated, &d.name)
                    && generated.contains(n.as_str())
                {
                    ctx.declared.insert(n.clone());
                }
            }
        }
        ctx
    }

    /// Adds what one parsed file declares and registers.
    pub fn add(&mut self, parsed: &Parsed) {
        for d in &parsed.decls {
            if let (false, Some(n)) = (d.generated, &d.name) {
                self.declared.insert(n.clone());
            }
        }
        for (k, src, tgt) in &parsed.translations {
            self.translations[*k as usize].insert(src.clone(), tgt.clone());
        }
    }
}

/// Parses a file of a project, resolving translated namespaces with `ctx` and leaving out
/// generated declarations that the project writes elsewhere.
pub fn parse_in(src: &[u8], ctx: &Context) -> Parsed {
    let mut toks = Vec::with_capacity(src.len() / 5);
    lex(src, &mut toks);
    let mut parsed = parse_tokens_in(src, &toks, Some(ctx));
    let before = parsed.decls.len();
    for d in parsed.decls.iter_mut() {
        if d.generated
            && d.origin.is_some()
            && d.name.as_ref().is_some_and(|n| ctx.declared.contains(n))
        {
            d.name = None;
        }
    }
    if parsed
        .decls
        .iter()
        .any(|d| d.generated && d.origin.is_some() && d.name.is_none())
    {
        let keep: Vec<bool> = parsed
            .decls
            .iter()
            .map(|d| !(d.generated && d.origin.is_some() && d.name.is_none()))
            .collect();
        remove_decls(&mut parsed.decls, &keep);
    }
    debug_assert!(parsed.decls.len() <= before);
    parsed
}

/// Parses a file whose tokens are already known.
pub fn parse_tokens(src: &[u8], toks: &[Token]) -> Parsed {
    parse_tokens_in(src, toks, None)
}

fn parse_tokens_in(src: &[u8], toks: &[Token], ctx: Option<&Context>) -> Parsed {
    let segs = segment(src, toks);
    let mut r = Reader {
        src,
        toks,
        parsed: Parsed::default(),
        prefix: String::new(),
        scopes: Vec::new(),
        dicts: [None, None],
        registry: Default::default(),
        ctx,
    };
    for (k, seg) in segs.iter().enumerate() {
        let end = segs.get(k + 1).map_or(toks.len(), |s| s.start);
        r.command(seg.start, seg.head, end);
    }
    for d in &mut r.parsed.decls {
        if let Some(n) = d.name.as_deref().and_then(lean_name) {
            d.name = Some(n);
        }
    }
    drop_shadowed(&mut r.parsed.decls);
    r.parsed
}

#[derive(Clone, Copy, Debug)]
struct Seg {
    start: usize,
    head: usize,
}

#[inline]
fn is_ident(src: &[u8], t: &Token, s: &[u8]) -> bool {
    t.kind == Kind::Ident && t.text(src) == s
}

/// Index of the token closing the bracket opened at `i`, or the last token.
fn matching_close(toks: &[Token], i: usize, limit: usize) -> usize {
    let mut depth = 0i32;
    let mut j = i;
    while j < limit {
        match toks[j].kind {
            Kind::Open => depth += 1,
            Kind::Close => {
                depth -= 1;
                if depth <= 0 {
                    return j;
                }
            }
            _ => {}
        }
        j += 1;
    }
    limit.saturating_sub(1).max(i)
}

/// Skips a doc comment, attributes and modifiers starting at `i`; returns the first other token.
fn skip_chain(src: &[u8], toks: &[Token], mut i: usize) -> usize {
    let n = toks.len();
    while i < n {
        let t = &toks[i];
        match t.kind {
            Kind::Doc => i += 1,
            Kind::Open if t.attr => i = matching_close(toks, i, n) + 1,
            Kind::Ident => match classify(t.text(src)) {
                Kw::Modifier(Modifier::Scoped) => {
                    i += 1;
                    if i + 2 < n
                        && toks[i].kind == Kind::Open
                        && src[toks[i].start as usize] == b'['
                        && toks[i + 1].kind == Kind::Ident
                        && toks[i + 2].kind == Kind::Close
                    {
                        i += 3;
                    }
                }
                Kw::Modifier(_) => i += 1,
                _ => return i,
            },
            _ => return i,
        }
    }
    i
}

/// True if the token ends a term, so a token after it at command indentation cannot continue it.
fn ends_term(src: &[u8], t: &Token) -> bool {
    match t.kind {
        Kind::Ident => {
            let s = t.text(src);
            !expects_more(s)
                && !matches!(
                    classify(s),
                    Kw::Decl(_) | Kw::Instance | Kw::Modifier(_) | Kw::Scope(ScopeKw::Namespace)
                )
        }
        Kind::Num
        | Kind::Str
        | Kind::Char
        | Kind::NameLit
        | Kind::Close
        | Kind::ModDoc
        | Kind::Hash => true,
        // The closing bar of `|x|`.
        Kind::Bar => !t.lead,
        Kind::ColonColon => true,
        Kind::Sym => {
            let s = t.text(src);
            let (c, _) = crate::chars::decode(s, 0);
            // Postfix operators and constants: `Type*`, `n !`, `..`, `x⁻¹`, `xᶜ`, `Rˣ`, `⊤`, `⊥`,
            // `∅`, `∞`, `‖x‖`, `⌊x⌋`, `⊢`.
            matches!(
                c,
                0x25 | 0x27 | 0x2a | 0x21 | 0x2e | 0xb2 | 0xb3 | 0xb9 | 0x2020 | 0x2032 | 0x2016 | 0x2205 | 0x221e | 0x2217
                    | 0x22a2 | 0x22a4 | 0x22a5 | 0x230b | 0x2309 | 0x02b0..=0x02ff | 0x1d2c..=0x1dbf
                    | 0x2070..=0x209f
            )
        }
        _ => false,
    }
}

/// Like [`ends_term`], also accepting the end of a JSX element (`</div>`, `/>`).
fn ends_term_at(src: &[u8], toks: &[Token], k: usize) -> bool {
    let t = &toks[k];
    if ends_term(src, t) {
        return true;
    }
    if t.kind == Kind::Sym && t.text(src) == b">" && k >= 1 {
        let p = &toks[k - 1];
        if p.kind == Kind::Sym && p.text(src) == b"/" {
            return true;
        }
        if p.kind == Kind::Ident
            && k >= 2
            && toks[k - 2].kind == Kind::Sym
            && toks[k - 2].text(src) == b"/"
        {
            return true;
        }
    }
    false
}

/// Splits the token stream into commands.
fn segment(src: &[u8], toks: &[Token]) -> Vec<Seg> {
    let n = toks.len();
    let mut segs: Vec<Seg> = Vec::with_capacity(n / 40 + 4);
    let mut depth = 0i32;
    let mut base_col = 0u32;
    let mut prefix_cmd = false;
    let mut col0_block = false;
    let mut watch_block = false;
    let mut i = 0usize;

    while i < n {
        let t = toks[i];
        if watch_block {
            watch_block = false;
            if t.lead && t.col <= base_col && is_block_content(src, &t) {
                col0_block = true;
            }
        }
        // Commands begin a line; a token after others on its line starts one only after
        // `in`, which is handled below, or when it is a module doc.
        let start = if t.lead || t.kind == Kind::ModDoc {
            let at_base = t.lead && t.col <= base_col;
            let prev_ends = i == 0 || ends_term_at(src, toks, i - 1);
            start_at(src, toks, i, depth, at_base, prev_ends, col0_block)
        } else {
            None
        };
        if let Some(head) = start {
            segs.push(Seg { start: i, head });
            if t.lead {
                base_col = t.col;
            }
            depth = 0;
            col0_block = false;
            prefix_cmd = takes_in(src, &toks[head]);
            i = head + 1;
            continue;
        }
        match t.kind {
            Kind::Open => depth += 1,
            Kind::Close => {
                if depth > 0 {
                    depth -= 1
                }
            }
            Kind::Ident if depth == 0 => {
                let s = t.text(src);
                if prefix_cmd && s == b"in" && i + 1 < n {
                    let head = skip_chain(src, toks, i + 1);
                    if head < n {
                        segs.push(Seg { start: i + 1, head });
                        depth = 0;
                        col0_block = false;
                        prefix_cmd = takes_in(src, &toks[head]);
                        i = head + 1;
                        continue;
                    }
                }
                if matches!(s, b"by" | b"where" | b"do") {
                    watch_block = true;
                }
            }
            _ => {}
        }
        i += 1;
    }
    segs
}

/// True if a token at command indentation right after `by`, `where` or `do` belongs to that
/// block rather than to a new command.
fn is_block_content(src: &[u8], t: &Token) -> bool {
    match t.kind {
        Kind::Ident => matches!(classify(t.text(src)), Kw::None | Kw::Continue),
        Kind::Hash | Kind::Doc | Kind::ModDoc | Kind::Bar => false,
        Kind::Open => !t.attr,
        _ => true,
    }
}

/// Commands that can prefix another command with `in`.
fn takes_in(src: &[u8], t: &Token) -> bool {
    match t.kind {
        Kind::Ident => matches!(
            t.text(src),
            b"open"
                | b"set_option"
                | b"omit"
                | b"include"
                | b"variable"
                | b"attribute"
                | b"unseal"
                | b"seal"
                | b"suppress_compilation"
        ),
        Kind::Hash => t.text(src) == b"#guard_msgs",
        _ => false,
    }
}

/// Decides whether a command starts at token `i`; returns the index of its head keyword.
fn start_at(
    src: &[u8],
    toks: &[Token],
    i: usize,
    depth: i32,
    at_base: bool,
    prev_ends: bool,
    col0_block: bool,
) -> Option<usize> {
    let t = &toks[i];
    let n = toks.len();
    let free = depth == 0 || at_base;
    if t.kind == Kind::Ident && i > 0 {
        // `x.end`, `.opaque` and `| instance` use keywords as names.
        let p = &toks[i - 1];
        if (p.kind == Kind::Dot && p.end == t.start) || (p.kind == Kind::Bar && !t.lead) {
            return None;
        }
        // `| /-- doc -/ prefix (s : Seq)`: a constructor named by a keyword.
        if p.kind == Kind::Doc && i >= 2 && toks[i - 2].kind == Kind::Bar {
            return None;
        }
    }
    match t.kind {
        Kind::ModDoc => Some(i),
        Kind::Doc => {
            if !free || (!t.lead && i > 0) {
                return None;
            }
            // The doc comment argument of `#adaptation_note` and `library_note name`.
            if i > 0
                && toks[i - 1].kind == Kind::Hash
                && toks[i - 1].text(src) == b"#adaptation_note"
            {
                return None;
            }
            if i > 1 && is_ident(src, &toks[i - 2], b"library_note") {
                return None;
            }
            let head = skip_chain(src, toks, i);
            head_starts(src, toks, head, at_base && prev_ends && !col0_block).then_some(head)
        }
        Kind::Open if t.attr => {
            if !free {
                return None;
            }
            let head = skip_chain(src, toks, i);
            head_starts(src, toks, head, at_base && prev_ends && !col0_block).then_some(head)
        }
        Kind::Hash => {
            let after_where = i > 0 && is_ident(src, &toks[i - 1], b"where");
            (at_base && depth == 0 && (prev_ends || after_where) && !col0_block).then_some(i)
        }
        Kind::Ident => {
            let s = t.text(src);
            match classify(s) {
                Kw::Modifier(_) => {
                    if !free {
                        return None;
                    }
                    let head = skip_chain(src, toks, i);
                    head_starts(src, toks, head, at_base && prev_ends && !col0_block)
                        .then_some(head)
                }
                Kw::Decl(DeclKw::Deriving) => {
                    if !free {
                        return None;
                    }
                    let mut j = i + 1;
                    if j < n && is_ident(src, &toks[j], b"noncomputable") {
                        j += 1;
                    }
                    (j < n && is_ident(src, &toks[j], b"instance")).then_some(i)
                }
                Kw::Decl(d) => {
                    // Declarations begin a line; a keyword in the middle of one is text (JSX) or
                    // follows `in`, which `segment` handles.
                    if !free || !t.lead {
                        return None;
                    }
                    if i > 0 {
                        let p = &toks[i - 1];
                        if matches!(d, DeclKw::Inductive | DeclKw::Abbrev)
                            && is_ident(src, p, b"class")
                        {
                            return None;
                        }
                    }
                    Some(i)
                }
                Kw::Instance => {
                    if !free || !t.lead {
                        return None;
                    }
                    if i > 0 {
                        let p = &toks[i - 1];
                        if is_ident(src, p, b"deriving")
                            || is_ident(src, p, b"noncomputable")
                                && i > 1
                                && is_ident(src, &toks[i - 2], b"deriving")
                        {
                            return None;
                        }
                    }
                    Some(i)
                }
                Kw::Scope(_) | Kw::Command => (free && t.lead).then_some(i),
                Kw::SoftCommand => {
                    // An empty `where` structure instance can end a declaration.
                    let after_where = i > 0 && is_ident(src, &toks[i - 1], b"where");
                    (at_base && depth == 0 && (prev_ends || after_where) && !col0_block)
                        .then_some(i)
                }
                Kw::Continue => None,
                Kw::None => {
                    if !(at_base && depth == 0 && prev_ends && !col0_block) || is_tactic_word(s) {
                        return None;
                    }
                    if i + 1 < n && matches!(toks[i + 1].kind, Kind::Colon | Kind::Assign) {
                        return None;
                    }
                    Some(i)
                }
            }
        }
        _ => None,
    }
}

/// True if `head`, reached after a doc comment, attributes or modifiers, begins a command.
/// `may_be_custom` allows an unknown keyword (a user-defined command) when the chain starts a
/// line at command indentation after a complete term.
fn head_starts(src: &[u8], toks: &[Token], head: usize, may_be_custom: bool) -> bool {
    let n = toks.len();
    if head >= n {
        return false;
    }
    let t = &toks[head];
    match t.kind {
        Kind::Ident => match classify(t.text(src)) {
            Kw::Decl(_) | Kw::Instance | Kw::Scope(_) | Kw::Command => true,
            Kw::SoftCommand => may_be_custom,
            Kw::Continue | Kw::Modifier(_) => false,
            Kw::None => {
                may_be_custom
                    && !(head + 1 < n && matches!(toks[head + 1].kind, Kind::Colon | Kind::Assign))
            }
        },
        Kind::Hash => may_be_custom,
        _ => false,
    }
}

struct ScopeEntry {
    prefix_len: usize,
}

struct Reader<'a> {
    src: &'a [u8],
    toks: &'a [Token],
    parsed: Parsed,
    prefix: String,
    scopes: Vec<ScopeEntry>,
    /// `to_additive` and `to_dual` dictionaries with this file's name hints, made on first use.
    dicts: [Option<crate::translate::Dict>; 2],
    /// Translations registered so far in this file, for each attribute.
    registry: [std::collections::HashMap<String, String>; 2],
    /// The rest of the project, when known.
    ctx: Option<&'a Context>,
}

/// Removes generated declarations whose name the file declares itself, and repeated generated
/// names, renumbering `parent` indices.
fn drop_shadowed(decls: &mut Vec<Decl>) {
    if !decls.iter().any(|d| d.generated) {
        return;
    }
    let written: std::collections::HashSet<&str> = decls
        .iter()
        .filter(|d| !d.generated)
        .filter_map(|d| d.name.as_deref())
        .collect();
    let mut seen = std::collections::HashSet::new();
    let keep: Vec<bool> = decls
        .iter()
        .map(|d| match (&d.name, d.generated) {
            (Some(n), true) => !written.contains(n.as_str()) && seen.insert(n.clone()),
            _ => true,
        })
        .collect();
    if keep.iter().all(|&k| k) {
        return;
    }
    remove_decls(decls, &keep);
}

/// Removes the declarations not marked in `keep`, renumbering `parent` indices.
fn remove_decls(decls: &mut Vec<Decl>, keep: &[bool]) {
    let mut index = vec![u32::MAX; decls.len()];
    let mut next = 0u32;
    for (i, &k) in keep.iter().enumerate() {
        if k {
            index[i] = next;
            next += 1;
        }
    }
    let mut i = 0;
    decls.retain(|_| {
        let k = keep[i];
        i += 1;
        k
    });
    for d in decls.iter_mut() {
        d.parent = d
            .parent
            .and_then(|p| (index[p as usize] != u32::MAX).then(|| index[p as usize]));
    }
}

/// The name as Lean prints it: `«»` is dropped around components that are identifiers.
/// Returns `None` if nothing changes.
fn lean_name(name: &str) -> Option<String> {
    if !name.contains('«') {
        return None;
    }
    let parts: Vec<String> = components(name)
        .into_iter()
        .map(|c| {
            let inner = c.strip_prefix('«').and_then(|x| x.strip_suffix('»'));
            match inner {
                Some(x) if is_identifier(x) => x.to_string(),
                _ => c.to_string(),
            }
        })
        .collect();
    let out = parts.join(".");
    (out != name).then_some(out)
}

/// True if `s` is an identifier by Lean's `isIdFirst` and `isIdRest`.
fn is_identifier(s: &str) -> bool {
    let mut cs = s.chars();
    let Some(c) = cs.next() else { return false };
    let first = c.is_ascii_alphabetic() || c == '_' || crate::chars::is_letter_like(c as u32);
    first
        && cs.all(|c| {
            c.is_ascii_alphanumeric()
                || matches!(c, '_' | '\'' | '!' | '?')
                || crate::chars::is_letter_like(c as u32)
                || crate::chars::is_subscript_alnum(c as u32)
        })
}

/// Splits a dotted name into components, keeping `«»`-escaped components whole.
pub fn name_components(s: &str) -> Vec<&str> {
    components(s)
}

/// Splits a dotted name into components, keeping `«»`-escaped components whole.
fn components(s: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let b = s.as_bytes();
    let mut start = 0;
    let mut i = 0;
    let mut esc = false;
    while i < b.len() {
        if b[i] == 0xc2 && i + 1 < b.len() {
            if b[i + 1] == 0xab {
                esc = true;
            } else if b[i + 1] == 0xbb {
                esc = false;
            }
        } else if b[i] == b'.' && !esc {
            out.push(&s[start..i]);
            start = i + 1;
        }
        i += 1;
    }
    out.push(&s[start..]);
    out
}

/// Last component of a dotted name, with `«»` removed.
fn last_component(s: &str) -> String {
    let c = components(s);
    c.last().copied().unwrap_or("").replace(['«', '»'], "")
}

impl<'a> Reader<'a> {
    #[inline]
    fn text(&self, i: usize) -> &'a str {
        let t = &self.toks[i];
        // Token boundaries fall on UTF-8 boundaries of a `&str`; callers pass `&str` bytes.
        unsafe { std::str::from_utf8_unchecked(&self.src[t.start as usize..t.end as usize]) }
    }

    #[inline]
    fn ident_is(&self, i: usize, s: &[u8]) -> bool {
        i < self.toks.len() && is_ident(self.src, &self.toks[i], s)
    }

    fn qualify(&self, written: &str) -> String {
        if let Some(rest) = written.strip_prefix("_root_.") {
            return rest.to_string();
        }
        if self.prefix.is_empty() {
            written.to_string()
        } else {
            let mut s = String::with_capacity(self.prefix.len() + 1 + written.len());
            s.push_str(&self.prefix);
            s.push('.');
            s.push_str(written);
            s
        }
    }

    fn namespace_index(&mut self) -> u32 {
        if let Some(last) = self.parsed.namespaces.last()
            && *last == self.prefix
        {
            return (self.parsed.namespaces.len() - 1) as u32;
        }
        self.parsed.namespaces.push(self.prefix.clone());
        (self.parsed.namespaces.len() - 1) as u32
    }

    fn command(&mut self, s: usize, h: usize, e: usize) {
        let toks = self.toks;
        let e = e.max(h + 1).min(toks.len());
        let range = Span::new(toks[s].start, toks[e - 1].end);
        let head = toks[h];
        let ns = self.namespace_index();
        let mut kind = CommandKind::Other;
        match head.kind {
            Kind::ModDoc => kind = CommandKind::ModuleDoc,
            Kind::Hash => kind = CommandKind::Hash,
            Kind::Ident => match classify(head.text(self.src)) {
                Kw::Scope(ScopeKw::Namespace) => {
                    kind = CommandKind::Namespace;
                    if h + 1 < e && toks[h + 1].kind == Kind::Ident {
                        let name = self.text(h + 1);
                        for c in components(name) {
                            self.scopes.push(ScopeEntry {
                                prefix_len: self.prefix.len(),
                            });
                            if !self.prefix.is_empty() {
                                self.prefix.push('.');
                            }
                            self.prefix.push_str(c);
                        }
                    }
                }
                Kw::Scope(ScopeKw::Section) => {
                    kind = CommandKind::Section;
                    let count = if h + 1 < e && toks[h + 1].kind == Kind::Ident {
                        components(self.text(h + 1)).len()
                    } else {
                        1
                    };
                    for _ in 0..count {
                        self.scopes.push(ScopeEntry {
                            prefix_len: self.prefix.len(),
                        });
                    }
                }
                Kw::Scope(ScopeKw::Mutual) => {
                    kind = CommandKind::Mutual;
                    self.scopes.push(ScopeEntry {
                        prefix_len: self.prefix.len(),
                    });
                }
                Kw::Scope(ScopeKw::End) => {
                    kind = CommandKind::End;
                    let count = if h + 1 < e && toks[h + 1].kind == Kind::Ident {
                        components(self.text(h + 1)).len()
                    } else {
                        1
                    };
                    let mut restore = None;
                    for _ in 0..count {
                        if let Some(sc) = self.scopes.pop() {
                            restore = Some(sc.prefix_len);
                        }
                    }
                    if let Some(len) = restore {
                        self.prefix.truncate(len);
                    }
                }
                Kw::Decl(_) | Kw::Instance => {
                    kind = CommandKind::Declaration;
                    self.declaration(s, h, e);
                }
                _ => {
                    self.special_command(h, e);
                    kind = match head.text(self.src) {
                        b"open" => CommandKind::Open,
                        b"variable" => CommandKind::Variable,
                        b"universe" => CommandKind::Universe,
                        b"set_option" => CommandKind::SetOption,
                        b"attribute" => CommandKind::Attribute,
                        b"import" => CommandKind::Import,
                        b"module" => CommandKind::Module,
                        _ => CommandKind::Other,
                    };
                }
            },
            _ => {}
        }
        self.parsed.commands.push(Command {
            kind,
            range,
            head: Span::new(head.start, head.end),
            namespace: ns,
        });
    }

    /// `attribute [...] names` and the translation hints of `to_additive` and `to_dual`.
    fn special_command(&mut self, h: usize, e: usize) {
        let toks = self.toks;
        match toks[h].text(self.src) {
            b"attribute" => {
                if h + 1 >= e || toks[h + 1].kind != Kind::Open {
                    return;
                }
                let close = matching_close(toks, h + 1, e);
                let a0 = self.parsed.attrs.len() as u32;
                self.attributes(h + 2, close);
                let a1 = self.parsed.attrs.len() as u32;
                let mut j = close + 1;
                while j < e && toks[j].kind == Kind::Ident && toks[j].text(self.src) != b"in" {
                    let written = self.text(j);
                    let full = if written.contains('.') {
                        written.trim_start_matches("_root_.").to_string()
                    } else {
                        self.qualify(written)
                    };
                    let last = last_component(&full);
                    let kind = if last.starts_with(|c: char| c.is_ascii_uppercase()) {
                        DeclKind::Structure
                    } else {
                        DeclKind::Theorem
                    };
                    self.attribute_decls(None, Some(&full), kind, (a0, a1));
                    j += 1;
                }
            }
            b"to_additive_name_hint" | b"to_dual_name_hint" => {
                let k = (toks[h].text(self.src) == b"to_dual_name_hint") as usize;
                let mut j = h + 1;
                while j + 1 < e && toks[j].kind == Kind::Ident && toks[j + 1].kind == Kind::Ident {
                    let (a, b) = (self.text(j).to_string(), self.text(j + 1).to_string());
                    let d = self.dict_mut(k);
                    d.add_hint(&a, &b);
                    if k == 1 {
                        d.add_hint(&b, &a);
                    }
                    j += 2;
                    if j < e && toks[j].kind == Kind::Comma {
                        j += 1;
                    }
                }
            }
            b"mk_iff_of_inductive_prop" => {
                if h + 2 < e && toks[h + 1].kind == Kind::Ident && toks[h + 2].kind == Kind::Ident {
                    let written = self.text(h + 2);
                    let name = if written.contains('.') {
                        written.trim_start_matches("_root_.").to_string()
                    } else {
                        self.qualify(written)
                    };
                    let sel = Span::new(toks[h + 2].start, toks[h + 2].end);
                    let range = Span::new(toks[h].start, toks[e - 1].end);
                    self.push(Decl {
                        kind: DeclKind::Theorem,
                        name: Some(name),
                        generated: true,
                        range,
                        selection: sel,
                        keyword: Span::new(toks[h].start, toks[h].end),
                        signature: range,
                        type_: None,
                        body: None,
                        doc: None,
                        attrs: (0, 0),
                        modifiers: 0,
                        parent: None,
                        origin: Some(Span::new(toks[h].start, toks[h].end)),
                        approximate: false,
                    });
                }
            }
            b"insert_to_additive_translation" | b"insert_to_dual_translation" => {
                let k = (toks[h].text(self.src) == b"insert_to_dual_translation") as usize;
                if h + 2 < e && toks[h + 1].kind == Kind::Ident && toks[h + 2].kind == Kind::Ident {
                    let (a, b) = (self.text(h + 1).to_string(), self.text(h + 2).to_string());
                    self.register(k, a, b);
                }
            }
            _ => {}
        }
    }

    /// Reads the doc comment, attributes and modifiers in `[s, h)`.
    fn modifiers(&mut self, s: usize, h: usize) -> (Option<Span>, (u32, u32), u16) {
        let toks = self.toks;
        let mut doc = None;
        let mut mods = 0u16;
        let a0 = self.parsed.attrs.len() as u32;
        let mut i = s;
        while i < h {
            let t = toks[i];
            match t.kind {
                Kind::Doc => {
                    doc = Some(Span::new(t.start, t.end));
                    i += 1;
                }
                Kind::Open if t.attr => {
                    let close = matching_close(toks, i, h);
                    self.attributes(i + 1, close);
                    i = close + 1;
                }
                Kind::Ident => {
                    if let Kw::Modifier(m) = classify(t.text(self.src)) {
                        mods |= m.bit();
                    }
                    i += 1;
                }
                _ => i += 1,
            }
        }
        (doc, (a0, self.parsed.attrs.len() as u32), mods)
    }

    /// Records the attribute instances between `@[` and `]`.
    fn attributes(&mut self, a: usize, b: usize) {
        let toks = self.toks;
        let mut item = a;
        let mut depth = 0i32;
        let mut j = a;
        while j <= b {
            let at_end = j == b;
            if !at_end {
                match toks[j].kind {
                    Kind::Open => depth += 1,
                    Kind::Close => depth -= 1,
                    _ => {}
                }
            }
            if at_end || (depth == 0 && toks[j].kind == Kind::Comma) {
                if item < j {
                    let mut k = item;
                    while k < j
                        && (self.ident_is(k, b"scoped")
                            || self.ident_is(k, b"local")
                            || toks[k].kind == Kind::Sym)
                    {
                        k += 1;
                    }
                    let name = if k < j {
                        Span::new(toks[k].start, toks[k].end)
                    } else {
                        Span::new(toks[item].start, toks[item].end)
                    };
                    self.parsed.attrs.push(Attr {
                        span: Span::new(toks[item].start, toks[j - 1].end),
                        name,
                        toks: (item as u32, j as u32),
                    });
                }
                item = j + 1;
            }
            j += 1;
        }
    }

    fn push(&mut self, d: Decl) -> u32 {
        self.parsed.decls.push(d);
        (self.parsed.decls.len() - 1) as u32
    }

    /// The namespace named by a `scoped[NS]` modifier in `[s, h)`.
    fn scoped_namespace(&self, s: usize, h: usize) -> Option<String> {
        let toks = self.toks;
        let mut i = s;
        while i + 3 <= h && i + 3 < toks.len() {
            if self.ident_is(i, b"scoped")
                && toks[i + 1].kind == Kind::Open
                && self.src[toks[i + 1].start as usize] == b'['
                && toks[i + 2].kind == Kind::Ident
                && toks[i + 3].kind == Kind::Close
            {
                return Some(self.text(i + 2).to_string());
            }
            i += 1;
        }
        None
    }

    fn declaration(&mut self, s: usize, h: usize, e: usize) {
        // `scoped[NS] cmd` runs `cmd` in namespace `NS` (`with_weak_namespace`).
        let saved = self
            .scoped_namespace(s, h)
            .map(|ns| std::mem::replace(&mut self.prefix, ns));
        self.declaration_inner(s, h, e);
        if let Some(p) = saved {
            self.prefix = p;
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn simple(
        &mut self,
        kind: DeclKind,
        name_tok: usize,
        range: Span,
        keyword: Span,
        doc: Option<Span>,
        attrs: (u32, u32),
        mods: u16,
        e: usize,
    ) -> u32 {
        let toks = self.toks;
        let sel = Span::new(toks[name_tok].start, toks[name_tok].end);
        let name = self.qualify(self.text(name_tok));
        self.push(Decl {
            kind,
            name: Some(name),
            generated: false,
            range,
            selection: sel,
            keyword,
            signature: Span::new(keyword.start, toks[e - 1].end),
            type_: None,
            body: None,
            doc,
            attrs,
            modifiers: mods,
            parent: None,
            origin: None,
            approximate: false,
        })
    }

    fn declaration_inner(&mut self, s: usize, h: usize, e: usize) {
        let toks = self.toks;
        let src = self.src;
        let (doc, attrs, mods) = self.modifiers(s, h);
        let range = Span::new(toks[s].start, toks[e - 1].end);
        let kw = toks[h];
        let keyword = Span::new(kw.start, kw.end);
        let mut p = h + 1;
        let kind = match classify(kw.text(src)) {
            Kw::Instance => DeclKind::Instance,
            Kw::Decl(d) => match d {
                DeclKw::Theorem => DeclKind::Theorem,
                DeclKw::Lemma => DeclKind::Lemma,
                DeclKw::Def => DeclKind::Def,
                DeclKw::Abbrev => DeclKind::Abbrev,
                DeclKw::Opaque => DeclKind::Opaque,
                DeclKw::Axiom => DeclKind::Axiom,
                DeclKw::Example => DeclKind::Example,
                DeclKw::Structure => DeclKind::Structure,
                DeclKw::Class => {
                    if self.ident_is(p, b"inductive") {
                        p += 1;
                        DeclKind::ClassInductive
                    } else if self.ident_is(p, b"abbrev") {
                        p += 1;
                        DeclKind::ClassAbbrev
                    } else {
                        DeclKind::Class
                    }
                }
                DeclKw::Inductive => DeclKind::Inductive,
                DeclKw::Coinductive => DeclKind::Coinductive,
                DeclKw::IrreducibleDef => DeclKind::IrreducibleDef,
                DeclKw::Alias => return self.alias(range, keyword, doc, attrs, mods, p, e),
                DeclKw::ProofWanted => DeclKind::ProofWanted,
                DeclKw::Initialize => DeclKind::Initialize,
                DeclKw::RegisterOption => {
                    if p < e && toks[p].kind == Kind::Ident {
                        self.simple(
                            DeclKind::RegisterOption,
                            p,
                            range,
                            keyword,
                            doc,
                            attrs,
                            mods,
                            e,
                        );
                    }
                    return;
                }
                DeclKw::ConfigElab => {
                    // The command expands to several declarations; Lean records the range from
                    // the keyword to the configuration type.
                    if p < e && toks[p].kind == Kind::Ident {
                        let end = if p + 1 < e {
                            toks[p + 1].end
                        } else {
                            range.end
                        };
                        let range = Span::new(keyword.start, end);
                        self.simple(DeclKind::ConfigElab, p, range, keyword, doc, attrs, mods, e);
                    }
                    return;
                }
                DeclKw::Syntax | DeclKw::Notation | DeclKw::Macro | DeclKw::Elab => {
                    return self.syntax_decl(d, range, keyword, doc, attrs, mods, p, e);
                }
                DeclKw::Simproc => DeclKind::Simproc,
                DeclKw::UnifHint => {
                    if p < e
                        && toks[p].kind == Kind::Ident
                        && classify(toks[p].text(src)) == Kw::None
                    {
                        self.simple(DeclKind::UnifHint, p, range, keyword, doc, attrs, mods, e);
                    }
                    return;
                }
                DeclKw::DeclareSyntaxCat => {
                    // Categories live in `Lean.Parser.Category` whatever the namespace.
                    if p < e && toks[p].kind == Kind::Ident {
                        let saved =
                            std::mem::replace(&mut self.prefix, "Lean.Parser.Category".to_string());
                        self.simple(
                            DeclKind::SyntaxCategory,
                            p,
                            range,
                            keyword,
                            doc,
                            attrs,
                            mods,
                            e,
                        );
                        self.prefix = saved;
                    }
                    return;
                }
                DeclKw::Recall | DeclKw::MacroRules | DeclKw::ElabRules | DeclKw::Deriving => {
                    return;
                }
            },
            _ => return,
        };

        // Name.
        let mut name_tok = None;
        let mut selection = keyword;
        let mut decl_id = keyword;
        match kind {
            DeclKind::Example => {}
            DeclKind::Instance => {
                if p < e && toks[p].kind == Kind::Open && self.ident_is(p + 1, b"priority") {
                    p = matching_close(toks, p, e) + 1;
                }
                if p < e
                    && toks[p].kind == Kind::Ident
                    && !matches!(classify(toks[p].text(src)), Kw::Continue)
                {
                    name_tok = Some(p);
                    selection = Span::new(toks[p].start, toks[p].end);
                    p += 1;
                    if let Some(q) = self.universe_params(p, e) {
                        selection.end = toks[q].end;
                        p = q + 1;
                    }
                }
            }
            DeclKind::Initialize => {
                if p + 1 < e && toks[p].kind == Kind::Ident && toks[p + 1].kind == Kind::Colon {
                    name_tok = Some(p);
                    selection = Span::new(toks[p].start, toks[p].end);
                    p += 1;
                } else {
                    return;
                }
            }
            DeclKind::Simproc => {
                while p < e && toks[p].kind == Kind::Sym {
                    p += 1;
                }
                if p < e && toks[p].kind == Kind::Open && src[toks[p].start as usize] == b'[' {
                    p = matching_close(toks, p, e) + 1;
                }
                while p < e && toks[p].kind == Kind::Sym {
                    p += 1;
                }
                if p < e && toks[p].kind == Kind::Ident {
                    name_tok = Some(p);
                    selection = Span::new(toks[p].start, toks[p].end);
                    p += 1;
                } else {
                    return;
                }
            }
            _ => {
                if p < e && toks[p].kind == Kind::Ident {
                    name_tok = Some(p);
                    selection = Span::new(toks[p].start, toks[p].end);
                    decl_id = selection;
                    p += 1;
                    if let Some(q) = self.universe_params(p, e) {
                        decl_id.end = toks[q].end;
                        p = q + 1;
                    }
                } else {
                    return;
                }
            }
        }
        let name = name_tok.map(|t| self.qualify(self.text(t)));

        // `irreducible_def f (lemma := f_eq)`
        let mut def_lemma = None;
        if kind == DeclKind::IrreducibleDef
            && p + 4 < e
            && toks[p].kind == Kind::Open
            && self.ident_is(p + 1, b"lemma")
            && toks[p + 2].kind == Kind::Assign
            && toks[p + 3].kind == Kind::Ident
        {
            def_lemma = Some(p + 3);
            p = matching_close(toks, p, e) + 1;
        }

        // Signature and body.
        let structure_like = matches!(kind, DeclKind::Structure | DeclKind::Class);
        let inductive_like = matches!(
            kind,
            DeclKind::Inductive | DeclKind::Coinductive | DeclKind::ClassInductive
        );
        let mut depth = 0i32;
        let mut colon = None;
        let mut body = None;
        let mut extends = None;
        let mut stop = e;
        let mut pending_binds = 0u32;
        let mut in_match = false;
        let mut q = p;
        while q < e {
            let t = toks[q];
            match t.kind {
                Kind::Open => depth += 1,
                Kind::Close => {
                    if depth > 0 {
                        depth -= 1
                    }
                }
                Kind::Colon if depth == 0 && colon.is_none() => {
                    let named_parent = extends.is_some()
                        && q >= 2
                        && toks[q - 1].kind == Kind::Ident
                        && (toks[q - 2].kind == Kind::Comma || Some(q - 2) == extends);
                    if !named_parent {
                        colon = Some(q);
                    }
                }
                Kind::Assign if depth == 0 => {
                    if pending_binds > 0 {
                        pending_binds -= 1;
                    } else {
                        body = Some(q);
                        break;
                    }
                }
                Kind::Bar
                    if depth == 0
                        && !in_match
                        && (inductive_like || self.bar_is_alternative(q, e)) =>
                {
                    body = Some(
                        if inductive_like && q > p && toks[q - 1].kind == Kind::Doc {
                            q - 1
                        } else {
                            q
                        },
                    );
                    break;
                }
                Kind::Ident if depth == 0 => match t.text(src) {
                    b"where" => {
                        body = Some(q);
                        break;
                    }
                    b"extends" if structure_like => extends = Some(q),
                    b"deriving" if t.lead || structure_like || inductive_like => {
                        stop = q;
                        break;
                    }
                    b"let" | b"have" | b"letI" | b"haveI" | b"let_fun" => pending_binds += 1,
                    b"match" | b"fun" => in_match = true,
                    _ => {}
                },
                _ => {}
            }
            q += 1;
        }
        let sig_end_tok = body.unwrap_or(stop).max(h + 1) - 1;
        let signature = Span::new(kw.start, toks[sig_end_tok].end);
        let type_ = colon
            .filter(|&c| c < sig_end_tok)
            .map(|c| Span::new(toks[c + 1].start, toks[sig_end_tok].end));
        let body_span = body.map(|b| Span::new(toks[b].start, range.end));

        let idx = self.push(Decl {
            kind,
            name: name.clone(),
            generated: false,
            range,
            selection,
            keyword,
            signature,
            type_,
            body: body_span,
            doc,
            attrs,
            modifiers: mods,
            parent: None,
            origin: None,
            approximate: false,
        });

        let Some(full) = name else {
            self.attribute_decls(Some(idx), None, kind, attrs);
            return;
        };
        if kind == DeclKind::IrreducibleDef {
            let (sel, lemma) = match def_lemma {
                Some(t) => (
                    Span::new(toks[t].start, toks[t].end),
                    self.qualify(self.text(t)),
                ),
                None => (selection, format!("{full}_def")),
            };
            self.push(Decl {
                kind: DeclKind::Theorem,
                name: Some(lemma),
                generated: def_lemma.is_none(),
                range,
                selection: sel,
                keyword,
                signature,
                type_: None,
                body: None,
                doc: None,
                attrs: (attrs.1, attrs.1),
                modifiers: mods,
                parent: Some(idx),
                origin: None,
                approximate: false,
            });
        }
        self.attribute_decls(Some(idx), Some(&full), kind, attrs);
        if kind == DeclKind::ClassAbbrev {
            self.default_constructor(idx, &full, decl_id, keyword, attrs);
        }
        if structure_like {
            let class_range = Span::new(kw.start, range.end);
            if let Some(x) = extends {
                let limit = colon.filter(|&c| c > x).or(body).unwrap_or(stop);
                self.parents(idx, &full, class_range, x + 1, limit);
            }
            let mut ctor_written = false;
            if let Some(b) = body {
                ctor_written = self.fields(idx, &full, b + 1, stop.min(e), extends.is_some());
            }
            if !ctor_written {
                self.default_constructor(idx, &full, decl_id, keyword, attrs);
            }
        } else if inductive_like {
            if let Some(b) = body {
                let first = if matches!(toks[b].kind, Kind::Bar | Kind::Doc) {
                    b
                } else {
                    b + 1
                };
                self.constructors(idx, &full, first, stop.min(e));
            }
        } else if let Some(b) = body {
            // Most values contain neither `where` nor `let rec`; look at the bytes first.
            let text = &self.src[toks[b].end as usize..range.end as usize];
            let w = if finders().0.find(text).is_some() {
                self.find_where(b + 1, e)
            } else {
                None
            };
            let w = w.filter(|&w| !self.ident_is(w + 1, b"finally"));
            if finders().1.find(text).is_some() {
                self.let_recs(idx, &full, b + 1, w.unwrap_or(e));
            }
            if let Some(w) = w {
                self.where_decls(idx, &full, w, e);
            }
        }
    }

    /// `X.ext` and `X.ext_iff` generated by `@[ext]` on a structure, and `foo_iff` generated by
    /// `@[ext]` on a theorem `foo`, unless `(iff := false)`.
    fn ext_lemmas(&mut self, parent: Option<u32>, full: &str, kind: DeclKind, attrs: (u32, u32)) {
        let at = (attrs.0..attrs.1).find(|&i| {
            let a = self.parsed.attrs[i as usize];
            &self.src[a.name.start as usize..a.name.end as usize] == b"ext"
        });
        let Some(at) = at else { return };
        let attr = self.parsed.attrs[at as usize];
        let text = &self.src[attr.span.start as usize..attr.span.end as usize];
        let no_iff = text.windows(5).any(|w| w == b"false") && text.windows(3).any(|w| w == b"iff");
        let names: Vec<String> = match kind {
            DeclKind::Structure | DeclKind::Class if no_iff => vec![format!("{full}.ext")],
            DeclKind::Structure | DeclKind::Class => {
                vec![format!("{full}.ext"), format!("{full}.ext_iff")]
            }
            DeclKind::Theorem | DeclKind::Lemma if !no_iff => {
                vec![format!("{}_iff", full)]
            }
            _ => return,
        };
        for name in names {
            // Lean records the attribute instance as the range and its name as the selection.
            self.push(Decl {
                kind: DeclKind::Theorem,
                name: Some(name),
                generated: true,
                range: Span::new(attr.name.start, attr.span.end),
                selection: Span::new(attr.name.start, attr.span.end),
                keyword: attr.name,
                signature: attr.span,
                type_: None,
                body: None,
                doc: None,
                attrs: (attrs.1, attrs.1),
                modifiers: 0,
                parent,
                origin: Some(attr.name),
                approximate: false,
            });
        }
    }

    #[inline]
    fn span_text(&self, s: Span) -> &'a str {
        std::str::from_utf8(&self.src[s.start as usize..s.end as usize]).unwrap_or("")
    }

    /// Arguments of an attribute instance after its name: a hint (`existing`, `self`, `none`)
    /// and the identifiers, skipping options in parentheses, `-flag`/`+flag` and doc strings.
    fn attr_args(&self, a: &Attr) -> (Option<&'a str>, Vec<usize>) {
        let toks = self.toks;
        let (mut j, end) = (a.toks.0 as usize, a.toks.1 as usize);
        while j < end && toks[j].start != a.name.start {
            j += 1;
        }
        j += 1;
        let mut hint = None;
        let mut idents = Vec::new();
        while j < end {
            let t = toks[j];
            match t.kind {
                Kind::Open => j = matching_close(toks, j, end),
                Kind::Sym if matches!(t.text(self.src), b"-" | b"+") => j += 1,
                Kind::Ident => match t.text(self.src) {
                    b"existing" | b"self" | b"none" if idents.is_empty() && hint.is_none() => {
                        hint = Some(self.text(j));
                    }
                    _ => idents.push(j),
                },
                _ => {}
            }
            j += 1;
        }
        (hint, idents)
    }

    /// Pushes a declaration generated by the attribute `a`. Lean records the attribute instance
    /// from its name on (without `local` or `scoped`) as the range, and the written name as the
    /// selection, or the whole instance when no name is written.
    #[allow(clippy::too_many_arguments)]
    fn generated(
        &mut self,
        parent: Option<u32>,
        kind: DeclKind,
        a: &Attr,
        name: String,
        sel: Span,
        approximate: bool,
    ) {
        let range = Span::new(a.name.start, a.span.end);
        // Translations select the attribute name; other attributes the whole instance.
        let translation = matches!(self.span_text(a.name), "to_additive" | "to_dual");
        let sel = if sel == a.name && !translation {
            range
        } else {
            sel
        };
        // Newer Mathlib records a translation's range at the written name (or the attribute
        // name) instead of the whole attribute instance.
        let range = if translation && self.ctx.is_some_and(|c| c.translation_ranges_at_name) {
            sel
        } else {
            range
        };
        self.push(Decl {
            kind,
            name: Some(name),
            generated: true,
            range,
            selection: sel,
            keyword: a.name,
            signature: a.span,
            type_: None,
            body: None,
            doc: None,
            attrs: (0, 0),
            modifiers: 0,
            parent,
            origin: Some(a.name),
            approximate,
        });
    }

    /// The dictionary in effect: Mathlib's, or this file's copy once it adds name hints.
    fn dict(&self, k: usize) -> &crate::translate::Dict {
        self.dicts[k]
            .as_ref()
            .unwrap_or_else(|| crate::translate::Dict::shared(k))
    }

    /// This file's own copy of a dictionary, for name hints.
    fn dict_mut(&mut self, k: usize) -> &mut crate::translate::Dict {
        if self.dicts[k].is_none() {
            self.dicts[k] = Some(crate::translate::Dict::shared(k).clone());
        }
        self.dicts[k].as_mut().unwrap()
    }

    /// The translated namespace: the longest prefix this file has registered, otherwise each
    /// component through the dictionary.
    /// Registers the translation `src ↦ tgt` (both ways for `to_dual`).
    fn register(&mut self, k: usize, src: String, tgt: String) {
        if k == 1 {
            self.registry[1].insert(tgt.clone(), src.clone());
            self.parsed.translations.push((1, tgt.clone(), src.clone()));
        }
        self.registry[k].insert(src.clone(), tgt.clone());
        self.parsed.translations.push((k as u8, src, tgt));
    }

    fn translate_namespace(&mut self, k: usize, pre: &[&str]) -> Vec<String> {
        for len in (1..=pre.len()).rev() {
            let key = pre[..len].join(".");
            let found = self.registry[k]
                .get(&key)
                .or_else(|| self.ctx.and_then(|c| c.translations[k].get(&key)));
            if let Some(t) = found {
                let mut out: Vec<String> = components(t).into_iter().map(str::to_string).collect();
                out.extend(pre[len..].iter().map(|c| c.to_string()));
                return out;
            }
        }
        // Lean translates a namespace only through registered translations. Without the rest of
        // the project, guess each component instead.
        if self.ctx.is_some() {
            return pre.iter().map(|c| c.to_string()).collect();
        }
        let d = self.dict(k);
        pre.iter()
            .map(|c| crate::translate::guess_name(d, c))
            .collect()
    }

    /// Declarations that attributes of `full` generate: `to_additive`, `to_dual`, `to_fun`,
    /// `simps`, `mk_iff`, `higher_order`, `reassoc`, `elementwise` and `ext`.
    fn attribute_decls(
        &mut self,
        parent: Option<u32>,
        full: Option<&str>,
        kind: DeclKind,
        attrs: (u32, u32),
    ) {
        if let Some(f) = full {
            self.ext_lemmas(parent, f, kind, attrs);
        }
        let kind = if kind == DeclKind::Lemma {
            DeclKind::Theorem
        } else {
            kind
        };
        for i in attrs.0..attrs.1 {
            let a = self.parsed.attrs[i as usize];
            let (hint, idents) = self.attr_args(&a);
            let written = idents.first().map(|&j| {
                (
                    self.text(j),
                    Span::new(self.toks[j].start, self.toks[j].end),
                )
            });
            match self.span_text(a.name) {
                n @ ("to_additive" | "to_dual") => {
                    if hint.is_some() {
                        continue;
                    }
                    // An anonymous instance lives in the current namespace; only a written
                    // target can be named.
                    let anon;
                    let src = match full {
                        Some(f) => f,
                        None if written.is_some() => {
                            anon = format!("{}._", self.prefix);
                            anon.trim_start_matches('.')
                        }
                        None => continue,
                    };
                    let k = (n == "to_dual") as usize;
                    let parts = components(src);
                    let (last, pre) = parts
                        .split_last()
                        .map(|(l, p)| (*l, p))
                        .unwrap_or(("", &[]));
                    let ns = self.translate_namespace(k, pre);
                    let d = self.dict(k);
                    let target = crate::translate::target_name(last, &ns, written.map(|w| w.0), d);
                    self.register(k, src.to_string(), target.clone());
                    let sel = written.map_or(a.name, |w| w.1);
                    self.generated(parent, kind, &a, target.clone(), sel, written.is_none());
                    // `(attr := ext)` gives the translation its own `ext` lemmas.
                    if matches!(kind, DeclKind::Structure | DeclKind::Class) {
                        let (t0, t1) = (a.toks.0 as usize, a.toks.1 as usize);
                        for j in t0 + 1..t1 {
                            if self.ident_is(j, b"ext") && self.toks[j - 1].kind == Kind::Assign {
                                let ext = Span::new(self.toks[j].start, self.toks[j].end);
                                let inner = Attr {
                                    span: ext,
                                    name: ext,
                                    toks: (j as u32, j as u32 + 1),
                                };
                                for suffix in ["ext", "ext_iff"] {
                                    let name = format!("{target}.{suffix}");
                                    self.generated(
                                        parent,
                                        DeclKind::Theorem,
                                        &inner,
                                        name,
                                        ext,
                                        false,
                                    );
                                }
                            }
                        }
                    }
                }
                "to_fun" => {
                    let (Some(src), Some((t, sel))) = (full, written) else {
                        continue;
                    };
                    let parts = components(src);
                    let pre: Vec<String> = parts[..parts.len().saturating_sub(1)]
                        .iter()
                        .map(|c| c.to_string())
                        .collect();
                    let d = self.dict(0);
                    let target = crate::translate::target_name("", &pre, Some(t), d);
                    self.generated(parent, kind, &a, target, sel, false);
                }
                "simps" | "simps!" => {
                    for &j in &idents {
                        let arg = self.text(j);
                        let sel = Span::new(self.toks[j].start, self.toks[j].end);
                        let name = match full {
                            Some(f) => format!("{f}_{arg}"),
                            None => self.qualify(arg),
                        };
                        self.generated(parent, DeclKind::Theorem, &a, name, sel, false);
                    }
                }
                "mk_iff" => match written {
                    Some((t, sel)) => {
                        let name = self.qualify(t);
                        self.generated(parent, DeclKind::Theorem, &a, name, sel, false);
                    }
                    None => {
                        if let Some(f) = full {
                            // `Foo` gives `foo_iff`.
                            let parts = components(f);
                            let last = parts.last().copied().unwrap_or("");
                            let mut name = parts[..parts.len().saturating_sub(1)].join(".");
                            if !name.is_empty() {
                                name.push('.');
                            }
                            let mut cs = last.chars();
                            if let Some(c) = cs.next() {
                                name.extend(c.to_lowercase());
                                name.push_str(cs.as_str());
                            }
                            name.push_str("_iff");
                            self.generated(parent, DeclKind::Theorem, &a, name, a.name, true);
                        }
                    }
                },
                "higher_order" => {
                    let (Some(f), Some((t, sel))) = (full, written) else {
                        continue;
                    };
                    let parts = components(f);
                    let mut name = parts[..parts.len().saturating_sub(1)].join(".");
                    if !name.is_empty() {
                        name.push('.');
                    }
                    name.push_str(t);
                    self.generated(parent, DeclKind::Theorem, &a, name, sel, false);
                }
                "reassoc" => {
                    if let Some(f) = full {
                        let name = match written {
                            Some((t, _)) => self.qualify(t),
                            None => format!("{f}_assoc"),
                        };
                        let sel = written.map_or(a.name, |w| w.1);
                        self.generated(parent, DeclKind::Theorem, &a, name, sel, written.is_none());
                    }
                }
                "elementwise" => {
                    if let Some(f) = full {
                        self.generated(
                            parent,
                            DeclKind::Theorem,
                            &a,
                            format!("{f}_apply"),
                            a.name,
                            true,
                        );
                    }
                }
                _ => {}
            }
        }
    }

    /// The constructor `mk` that Lean adds to a structure without a written one. Its range is
    /// the structure's name with universe parameters.
    fn default_constructor(
        &mut self,
        parent: u32,
        full: &str,
        decl_id: Span,
        keyword: Span,
        attrs: (u32, u32),
    ) {
        self.push(Decl {
            kind: DeclKind::Constructor,
            name: Some(format!("{full}.mk")),
            generated: true,
            range: decl_id,
            selection: decl_id,
            keyword,
            signature: decl_id,
            type_: None,
            body: None,
            doc: None,
            attrs: (attrs.1, attrs.1),
            modifiers: 0,
            parent: Some(parent),
            origin: None,
            approximate: false,
        });
    }

    /// The first `where` at bracket depth zero in `[a, b)`.
    fn find_where(&self, a: usize, b: usize) -> Option<usize> {
        let toks = self.toks;
        let mut depth = 0i32;
        for j in a..b {
            match toks[j].kind {
                Kind::Open => depth += 1,
                Kind::Close => {
                    if depth > 0 {
                        depth -= 1
                    }
                }
                Kind::Ident if depth == 0 && toks[j].text(self.src) == b"where" => return Some(j),
                _ => {}
            }
        }
        None
    }

    /// Index of the `}` closing `.{u, v}` right after a declaration name, if present.
    fn universe_params(&self, p: usize, e: usize) -> Option<usize> {
        let toks = self.toks;
        if p + 1 < e
            && toks[p].kind == Kind::Dot
            && toks[p].start == toks[p - 1].end
            && toks[p + 1].kind == Kind::Open
            && self.src[toks[p + 1].start as usize] == b'{'
        {
            Some(matching_close(toks, p + 1, e))
        } else {
            None
        }
    }

    /// True if the `|` at `q` starts a match alternative rather than `|x|` notation.
    fn bar_is_alternative(&self, q: usize, e: usize) -> bool {
        let toks = self.toks;
        if toks[q].lead {
            return true;
        }
        let mut depth = 0i32;
        let mut j = q + 1;
        while j < e && !toks[j].lead {
            match toks[j].kind {
                Kind::Open => depth += 1,
                Kind::Close => depth -= 1,
                Kind::FatArrow if depth == 0 => return true,
                _ => {}
            }
            j += 1;
        }
        false
    }

    #[allow(clippy::too_many_arguments)]
    fn alias(
        &mut self,
        range: Span,
        keyword: Span,
        doc: Option<Span>,
        attrs: (u32, u32),
        mods: u16,
        p: usize,
        e: usize,
    ) {
        let toks = self.toks;
        let mut names = Vec::new();
        if p < e && toks[p].kind == Kind::Ident {
            let mut j = p;
            while j < e && toks[j].kind == Kind::Ident {
                names.push(j);
                j += 1;
            }
        } else if p < e && toks[p].kind == Kind::Open {
            let close = matching_close(toks, p, e);
            for j in p + 1..close {
                if toks[j].kind == Kind::Ident && toks[j].text(self.src) != b"_" {
                    names.push(j);
                }
            }
        }
        for j in names {
            let idx = self.simple(DeclKind::Alias, j, range, keyword, doc, attrs, mods, e);
            if attrs.0 < attrs.1 {
                let full = self.qualify(self.text(j));
                self.attribute_decls(Some(idx), Some(&full), DeclKind::Theorem, attrs);
            }
        }
    }

    /// `syntax`, `notation`, `macro`, `elab` with an explicit `(name := x)`, and the parser
    /// alias `syntax x := p`.
    #[allow(clippy::too_many_arguments)]
    fn syntax_decl(
        &mut self,
        d: DeclKw,
        range: Span,
        keyword: Span,
        doc: Option<Span>,
        attrs: (u32, u32),
        mods: u16,
        p: usize,
        e: usize,
    ) {
        let toks = self.toks;
        let kind = match d {
            DeclKw::Syntax => DeclKind::Syntax,
            DeclKw::Notation => DeclKind::Notation,
            DeclKw::Macro => DeclKind::Macro,
            _ => DeclKind::Elab,
        };
        if d == DeclKw::Syntax
            && p + 1 < e
            && toks[p].kind == Kind::Ident
            && toks[p + 1].kind == Kind::Assign
        {
            self.simple(kind, p, range, keyword, doc, attrs, mods, e);
            return;
        }
        let mut j = p;
        while j + 3 < e {
            let t = toks[j];
            if t.kind == Kind::Str {
                return;
            }
            if t.kind == Kind::Open
                && self.ident_is(j + 1, b"name")
                && toks[j + 2].kind == Kind::Assign
                && toks[j + 3].kind == Kind::Ident
            {
                // `macro` expands to `syntax` and `macro_rules`; Lean records the range from the
                // keyword on.
                let range = if d == DeclKw::Macro {
                    Span::new(keyword.start, range.end)
                } else {
                    range
                };
                self.simple(kind, j + 3, range, keyword, doc, attrs, mods, e);
                return;
            }
            j += 1;
        }
    }

    /// `toParent` projections from `extends` entries in `[a, b)`. Entries that are not a plain
    /// application of a name (`G ≃ H`) are skipped: their projection name depends on notation.
    fn parents(&mut self, parent: u32, full: &str, class_range: Span, a: usize, b: usize) {
        let toks = self.toks;
        let mut entry = a;
        let mut depth = 0i32;
        let mut plain = true;
        let mut j = a;
        while j <= b {
            let at_end = j == b || j >= toks.len();
            if !at_end {
                match toks[j].kind {
                    Kind::Open => depth += 1,
                    Kind::Close => depth -= 1,
                    Kind::Sym | Kind::FatArrow | Kind::Bar if depth == 0 && j > entry => {
                        plain = false
                    }
                    _ => {}
                }
            }
            if at_end || (depth == 0 && toks[j].kind == Kind::Comma) {
                if entry < j {
                    let sel = Span::new(toks[entry].start, toks[j - 1].end);
                    let named = entry + 1 < j
                        && toks[entry].kind == Kind::Ident
                        && toks[entry + 1].kind == Kind::Colon;
                    let proj = if named {
                        Some(self.text(entry).to_string())
                    } else {
                        let mut k = entry;
                        if k < j
                            && toks[k].kind == Kind::Sym
                            && self.src[toks[k].start as usize] == b'@'
                        {
                            k += 1;
                        }
                        (plain && k < j && toks[k].kind == Kind::Ident)
                            .then(|| format!("to{}", last_component(self.text(k))))
                    };
                    if let Some(proj) = proj {
                        self.push(Decl {
                            kind: DeclKind::ParentProjection,
                            name: Some(format!("{full}.{proj}")),
                            generated: true,
                            range: class_range,
                            selection: sel,
                            keyword: sel,
                            signature: sel,
                            type_: None,
                            body: None,
                            doc: None,
                            attrs: (0, 0),
                            modifiers: 0,
                            parent: Some(parent),
                            origin: None,
                            approximate: false,
                        });
                    }
                }
                entry = j + 1;
                plain = true;
                if at_end {
                    break;
                }
            }
            j += 1;
        }
    }

    /// Splits `[a, b)` into items that begin at lines indented like the first item. A doc
    /// comment, attributes and modifiers stay with the item they precede; match alternatives
    /// and termination clauses stay with the item before them.
    fn items(&self, a: usize, b: usize) -> Vec<(usize, usize)> {
        let toks = self.toks;
        let mut out = Vec::new();
        if a >= b {
            return out;
        }
        let col = toks[a].col;
        let mut start = a;
        let mut content = false;
        let mut depth = 0i32;
        let mut attr_depth = 0i32;
        let mut block_at_col = false;
        for j in a..b {
            let t = toks[j];
            if j > a && t.lead && t.col <= col && depth == 0 {
                let p = &toks[j - 1];
                if p.kind == Kind::Ident && matches!(p.text(self.src), b"do" | b"by") {
                    // `go := do` followed by items at the column of `go`: Lean reads every
                    // following line at that column as part of the block.
                    block_at_col = true;
                }
            }
            let continues = block_at_col
                || t.kind == Kind::Bar
                || (t.kind == Kind::Ident
                    && matches!(t.text(self.src), b"termination_by" | b"decreasing_by"));
            // A new item starts at the item column after a complete term, or at a doc comment
            // there, and begins with a name or a binder.
            let new_item =
                j > start && depth == 0 && t.lead && t.col == col && content && !continues && {
                    let head = skip_chain(self.src, toks, j);
                    let item_like = head < b
                        && match toks[head].kind {
                            Kind::Ident => {
                                matches!(
                                    classify(toks[head].text(self.src)),
                                    Kw::None | Kw::SoftCommand
                                )
                            }
                            Kind::Open => !toks[head].attr,
                            _ => false,
                        };
                    let after_term = t.kind == Kind::Doc || {
                        let mut k = j - 1;
                        while k > start && toks[k].kind == Kind::Doc {
                            k -= 1;
                        }
                        ends_term_at(self.src, toks, k)
                    };
                    item_like && after_term
                };
            if new_item {
                out.push((start, j));
                start = j;
                content = false;
            }
            let chain = attr_depth > 0
                || (t.kind == Kind::Open && t.attr)
                || t.kind == Kind::Doc
                || (t.kind == Kind::Ident && matches!(classify(t.text(self.src)), Kw::Modifier(_)));
            match t.kind {
                Kind::Open => {
                    depth += 1;
                    if t.attr || attr_depth > 0 {
                        attr_depth += 1;
                    }
                }
                Kind::Close => {
                    if depth > 0 {
                        depth -= 1
                    }
                    if attr_depth > 0 {
                        attr_depth -= 1;
                    }
                }
                _ => {}
            }
            if !chain {
                content = true;
            }
        }
        out.push((start, b));
        out
    }

    /// Fields and an explicit constructor of a structure body in `[a, b)`.
    /// Returns true if the constructor is written (`mk ::`).
    fn fields(&mut self, parent: u32, full: &str, a: usize, b: usize, extends: bool) -> bool {
        let toks = self.toks;
        let mut a = a;
        let mut ctor = false;
        // Explicit constructor `mods ident binders* ::`.
        let c = skip_chain(self.src, toks, a);
        if c < b && toks[c].kind == Kind::Ident {
            let mut j = c + 1;
            while j < b && toks[j].kind == Kind::Open {
                j = matching_close(toks, j, b) + 1;
            }
            if j < b && toks[j].kind == Kind::ColonColon {
                let sel = Span::new(toks[c].start, toks[c].end);
                let (doc, attrs, mods) = self.modifiers(a, c);
                self.push(Decl {
                    kind: DeclKind::Constructor,
                    name: Some(format!("{full}.{}", self.text(c))),
                    generated: false,
                    range: sel,
                    selection: sel,
                    keyword: sel,
                    signature: Span::new(toks[c].start, toks[j].end),
                    type_: None,
                    body: None,
                    doc,
                    attrs,
                    modifiers: mods,
                    parent: Some(parent),
                    origin: None,
                    approximate: false,
                });
                ctor = true;
                a = j + 1;
            }
        }
        for (s, e) in self.items(a, b) {
            let c = skip_chain(self.src, toks, s);
            if c >= e {
                continue;
            }
            let (doc, attrs, mods) = self.modifiers(s, c);
            let t = toks[c];
            let mut names = Vec::new();
            let mut type_ = None;
            match t.kind {
                Kind::Open => {
                    // `(x y : T)`, `{K : Type} [inst : DecidableEq K]`: every binder group.
                    let mut g = c;
                    while g < e && toks[g].kind == Kind::Open && !toks[g].attr {
                        let close = matching_close(toks, g, e);
                        let mut j = g + 1;
                        while j < close && toks[j].kind == Kind::Ident {
                            names.push(j);
                            j += 1;
                        }
                        if j < close && toks[j].kind == Kind::Colon && j + 1 < close {
                            type_ = Some(Span::new(toks[j + 1].start, toks[close - 1].end));
                        }
                        g = close + 1;
                    }
                }
                Kind::Ident => {
                    let mut depth = 0i32;
                    let mut colon = None;
                    let mut assign = None;
                    for j in c + 1..e {
                        match toks[j].kind {
                            Kind::Open => depth += 1,
                            Kind::Close => depth -= 1,
                            Kind::Colon if depth == 0 && colon.is_none() => colon = Some(j),
                            Kind::Assign if depth == 0 => {
                                assign = Some(j);
                                break;
                            }
                            _ => {}
                        }
                    }
                    // Without `extends`, `x := v` declares a field whose type is inferred; with
                    // it, `x := v` usually overrides the default of an inherited field, unless
                    // it carries its own doc comment.
                    if colon.is_some() || (assign.is_some() && (!extends || doc.is_some())) {
                        names.push(c);
                        let end = assign.unwrap_or(e);
                        if let Some(col) = colon
                            && col + 1 < end
                        {
                            type_ = Some(Span::new(toks[col + 1].start, toks[end - 1].end));
                        }
                    }
                }
                _ => {}
            }
            for j in names {
                let sel = Span::new(toks[j].start, toks[j].end);
                let field_name = format!("{full}.{}", self.text(j).replace(['«', '»'], ""));
                let field_idx = self.parsed.decls.len() as u32;
                self.push(Decl {
                    kind: DeclKind::Field,
                    name: Some(field_name.clone()),
                    generated: false,
                    range: sel,
                    selection: sel,
                    keyword: sel,
                    signature: Span::new(toks[s].start, toks[e - 1].end),
                    type_,
                    body: None,
                    doc,
                    attrs,
                    modifiers: mods,
                    parent: Some(parent),
                    origin: None,
                    approximate: false,
                });
                if attrs.0 < attrs.1 {
                    self.attribute_decls(
                        Some(field_idx),
                        Some(&field_name),
                        DeclKind::Theorem,
                        attrs,
                    );
                }
            }
        }
        ctor
    }

    /// Constructors of an inductive type in `[a, b)`.
    fn constructors(&mut self, parent: u32, full: &str, a: usize, b: usize) {
        let toks = self.toks;
        let mut starts = Vec::new();
        let mut depth = 0i32;
        let mut j = a;
        let mut stop = b;
        while j < b {
            let t = toks[j];
            match t.kind {
                Kind::Open => depth += 1,
                Kind::Close => {
                    if depth > 0 {
                        depth -= 1
                    }
                }
                Kind::Bar if depth == 0 => {
                    if j > a && toks[j - 1].kind == Kind::Doc {
                        starts.push((j - 1, j));
                    } else {
                        starts.push((j, j));
                    }
                }
                Kind::Ident
                    if depth == 0
                        && t.lead
                        && matches!(
                            t.text(self.src),
                            b"with" | b"deriving" | b"monotonicity_by"
                        ) =>
                {
                    stop = j;
                    break;
                }
                _ => {}
            }
            j += 1;
        }
        for (k, &(s, bar)) in starts.iter().enumerate() {
            let e = starts.get(k + 1).map_or(stop, |x| x.0);
            let mut c = skip_chain(self.src, toks, bar + 1);
            // A modifier keyword is the constructor's name unless a name follows it.
            while c > bar + 1
                && toks[c - 1].kind == Kind::Ident
                && matches!(classify(toks[c - 1].text(self.src)), Kw::Modifier(_))
                && !(c < e && toks[c].kind == Kind::Ident)
            {
                c -= 1;
            }
            if c >= e || toks[c].kind != Kind::Ident {
                continue;
            }
            let (_, attrs, mods) = self.modifiers(bar + 1, c);
            let doc = (toks[s].kind == Kind::Doc).then(|| Span::new(toks[s].start, toks[s].end));
            let sel = Span::new(toks[c].start, toks[c].end);
            let mut depth = 0i32;
            let mut type_ = None;
            for q in c + 1..e {
                match toks[q].kind {
                    Kind::Open => depth += 1,
                    Kind::Close => depth -= 1,
                    Kind::Colon if depth == 0 => {
                        if q + 1 < e {
                            type_ = Some(Span::new(toks[q + 1].start, toks[e - 1].end));
                        }
                        break;
                    }
                    _ => {}
                }
            }
            self.push(Decl {
                kind: DeclKind::Constructor,
                name: Some(format!("{full}.{}", self.text(c))),
                generated: false,
                range: Span::new(toks[s].start, toks[e - 1].end),
                selection: sel,
                keyword: Span::new(toks[bar].start, toks[bar].end),
                signature: Span::new(toks[bar].start, toks[e - 1].end),
                type_,
                body: None,
                doc,
                attrs,
                modifiers: mods,
                parent: Some(parent),
                origin: None,
                approximate: false,
            });
        }
    }

    /// `where` declarations after the `where` at `w`, up to `b`.
    fn where_decls(&mut self, parent: u32, full: &str, w: usize, b: usize) {
        let toks = self.toks;
        let mut stop = b;
        for j in w + 1..b {
            if toks[j].lead && toks[j].kind == Kind::Ident && toks[j].text(self.src) == b"deriving"
            {
                stop = j;
                break;
            }
        }
        for (s, e) in self.items(w + 1, stop) {
            let c = skip_chain(self.src, toks, s);
            if c >= e || toks[c].kind != Kind::Ident {
                continue;
            }
            if matches!(toks[c].text(self.src), b"termination_by" | b"decreasing_by") {
                continue;
            }
            let (doc, attrs, mods) = self.modifiers(s, c);
            let sel = Span::new(toks[c].start, toks[c].end);
            let mut depth = 0i32;
            let mut body = None;
            let mut colon = None;
            for q in c + 1..e {
                match toks[q].kind {
                    Kind::Open => depth += 1,
                    Kind::Close => depth -= 1,
                    Kind::Colon if depth == 0 && colon.is_none() => colon = Some(q),
                    Kind::Assign | Kind::Bar if depth == 0 => {
                        body = Some(q);
                        break;
                    }
                    _ => {}
                }
            }
            let sig_end = body.unwrap_or(e).max(c + 1) - 1;
            let name = format!("{full}.{}", self.text(c));
            // Lean's range for a `where` declaration ends before `termination_by`.
            let mut e = e;
            for q in body.unwrap_or(c) + 1..e {
                let t = toks[q];
                if t.lead
                    && t.kind == Kind::Ident
                    && match t.text(self.src) {
                        b"termination_by" | b"decreasing_by" => true,
                        b"finally" => t.col <= toks[c].col,
                        _ => false,
                    }
                {
                    e = q;
                    break;
                }
            }
            let idx = self.push(Decl {
                kind: DeclKind::WhereDecl,
                name: Some(name.clone()),
                generated: false,
                // Lean's range for a `where` declaration starts at its name.
                range: Span::new(toks[c].start, toks[e - 1].end),
                selection: sel,
                keyword: sel,
                signature: Span::new(toks[c].start, toks[sig_end].end),
                type_: colon
                    .filter(|&x| x < sig_end)
                    .map(|x| Span::new(toks[x + 1].start, toks[sig_end].end)),
                body: body.map(|x| Span::new(toks[x].start, toks[e - 1].end)),
                doc,
                attrs,
                modifiers: mods,
                parent: Some(parent),
                origin: None,
                approximate: false,
            });
            if let Some(bd) = body {
                self.let_recs(idx, &name, bd + 1, e);
            }
        }
    }

    /// `let rec` declarations in `[a, b)`. One `let rec` may declare several functions separated
    /// by a comma at the end of a line. A declaration ends before the next line indented no more
    /// than the line of its `let`.
    fn let_recs(&mut self, parent: u32, full: &str, a: usize, b: usize) {
        let toks = self.toks;
        let mut j = a;
        while j + 2 < b {
            if !(self.ident_is(j, b"let") && self.ident_is(j + 1, b"rec")) {
                j += 1;
                continue;
            }
            let mut k = j;
            while k > a && !toks[k].lead {
                k -= 1;
            }
            let line_col = toks[k].col;
            let keyword = Span::new(toks[j].start, toks[j + 1].end);
            let mut c = skip_chain(self.src, toks, j + 2);
            let first_col = if c < b { toks[c].col } else { 0 };
            j += 2;
            while c < b && toks[c].kind == Kind::Ident {
                let mut end = c + 1;
                let mut depth = 0i32;
                let mut next = None;
                while end < b
                    && !(toks[end].lead && toks[end].col <= line_col && toks[end].kind != Kind::Bar)
                {
                    match toks[end].kind {
                        Kind::Open => depth += 1,
                        Kind::Close => depth -= 1,
                        Kind::Comma if depth == 0 && end + 1 < b && toks[end + 1].lead => {
                            let n2 = skip_chain(self.src, toks, end + 1);
                            if n2 < b
                                && toks[n2].kind == Kind::Ident
                                && toks[n2].col.abs_diff(first_col) <= 1
                            {
                                next = Some(n2);
                                break;
                            }
                        }
                        _ => {}
                    }
                    end += 1;
                }
                let sel = Span::new(toks[c].start, toks[c].end);
                let mut depth = 0i32;
                let mut body = None;
                for q in c + 1..end {
                    match toks[q].kind {
                        Kind::Open => depth += 1,
                        Kind::Close => depth -= 1,
                        Kind::Assign | Kind::Bar if depth == 0 => {
                            body = Some(q);
                            break;
                        }
                        _ => {}
                    }
                }
                let sig_end = body.unwrap_or(end).max(c + 1) - 1;
                for q in body.unwrap_or(c) + 1..end {
                    let t = toks[q];
                    if t.lead
                        && t.kind == Kind::Ident
                        && match t.text(self.src) {
                            b"termination_by" | b"decreasing_by" => true,
                            b"finally" => t.col <= line_col,
                            _ => false,
                        }
                    {
                        end = q;
                        break;
                    }
                }
                let name = format!("{full}.{}", self.text(c));
                let idx = self.push(Decl {
                    kind: DeclKind::LetRec,
                    name: Some(name.clone()),
                    generated: false,
                    range: Span::new(toks[c].start, toks[end - 1].end),
                    selection: sel,
                    keyword,
                    signature: Span::new(toks[c].start, toks[sig_end].end),
                    type_: None,
                    body: body.map(|x| Span::new(toks[x].start, toks[end - 1].end)),
                    doc: None,
                    attrs: (0, 0),
                    modifiers: 0,
                    parent: Some(parent),
                    origin: None,
                    approximate: false,
                });
                // A `let rec` inside this one is named after it (`f.go.loop`).
                if let Some(bd) = body {
                    self.let_recs(idx, &name, bd + 1, end);
                }
                j = end.max(c + 1);
                match next {
                    Some(n2) => c = n2,
                    None => break,
                }
            }
        }
    }
}
