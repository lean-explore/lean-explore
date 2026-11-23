//! Keyword classes used by the command scanner.

/// What an identifier-like token means at the start of a command.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kw {
    /// Not a keyword the scanner knows.
    None,
    /// Declaration keyword (`theorem`, `def`, `structure`, ...).
    Decl(DeclKw),
    /// A declaration modifier (`private`, `noncomputable`, ...).
    Modifier(Modifier),
    /// `namespace`, `section`, `end`, `mutual`.
    Scope(ScopeKw),
    /// A command keyword that cannot occur inside a term, so it starts a command anywhere at
    /// bracket depth zero.
    Command,
    /// A command keyword that also occurs inside terms and tactic blocks (`open`, `set_option`),
    /// so it starts a command only at the start of a line at command indentation.
    SoftCommand,
    /// A keyword that continues the current command (`where`, `deriving`, `termination_by`).
    Continue,
    /// `instance`
    Instance,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DeclKw {
    Theorem,
    Lemma,
    Def,
    Abbrev,
    Opaque,
    Axiom,
    Example,
    Structure,
    Class,
    Inductive,
    Coinductive,
    IrreducibleDef,
    Alias,
    Recall,
    ProofWanted,
    Initialize,
    RegisterOption,
    Syntax,
    Notation,
    Macro,
    Elab,
    MacroRules,
    ElabRules,
    DeclareSyntaxCat,
    Deriving,
    Simproc,
    ConfigElab,
    UnifHint,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ScopeKw {
    Namespace,
    Section,
    End,
    Mutual,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Modifier {
    Private,
    Public,
    Protected,
    Meta,
    Noncomputable,
    Unsafe,
    Partial,
    Nonrec,
    Scoped,
    Local,
}

impl Modifier {
    pub fn bit(self) -> u16 {
        1 << (self as u16)
    }
    pub const ALL: [Modifier; 10] = [
        Modifier::Private,
        Modifier::Public,
        Modifier::Protected,
        Modifier::Meta,
        Modifier::Noncomputable,
        Modifier::Unsafe,
        Modifier::Partial,
        Modifier::Nonrec,
        Modifier::Scoped,
        Modifier::Local,
    ];
    pub fn name(self) -> &'static str {
        match self {
            Modifier::Private => "private",
            Modifier::Public => "public",
            Modifier::Protected => "protected",
            Modifier::Meta => "meta",
            Modifier::Noncomputable => "noncomputable",
            Modifier::Unsafe => "unsafe",
            Modifier::Partial => "partial",
            Modifier::Nonrec => "nonrec",
            Modifier::Scoped => "scoped",
            Modifier::Local => "local",
        }
    }
}

/// Classifies an identifier token.
#[inline]
pub fn classify(t: &[u8]) -> Kw {
    use DeclKw::*;
    // Every keyword starts with a lowercase letter or `_` and is at most 33 bytes long.
    match t.first() {
        Some(b'a'..=b'z' | b'_') if t.len() <= 33 => {}
        _ => return Kw::None,
    }
    match t {
        b"theorem" => Kw::Decl(Theorem),
        b"lemma" => Kw::Decl(Lemma),
        b"def" => Kw::Decl(Def),
        b"abbrev" => Kw::Decl(Abbrev),
        b"opaque" => Kw::Decl(Opaque),
        b"axiom" => Kw::Decl(Axiom),
        b"example" => Kw::Decl(Example),
        b"structure" => Kw::Decl(Structure),
        b"class" => Kw::Decl(Class),
        b"inductive" => Kw::Decl(Inductive),
        b"coinductive" => Kw::Decl(Coinductive),
        b"irreducible_def" => Kw::Decl(IrreducibleDef),
        b"alias" => Kw::Decl(Alias),
        b"recall" => Kw::Decl(Recall),
        b"proof_wanted" => Kw::Decl(ProofWanted),
        b"initialize" | b"builtin_initialize" => Kw::Decl(Initialize),
        b"register_option" | b"register_builtin_option" | b"register_linter_set" => {
            Kw::Decl(RegisterOption)
        }
        b"syntax" => Kw::Decl(Syntax),
        b"notation" | b"notation3" | b"infix" | b"infixl" | b"infixr" | b"prefix" | b"postfix" => {
            Kw::Decl(Notation)
        }
        b"macro" => Kw::Decl(Macro),
        b"elab" => Kw::Decl(Elab),
        b"macro_rules" => Kw::Decl(MacroRules),
        b"elab_rules" => Kw::Decl(ElabRules),
        b"declare_syntax_cat" => Kw::Decl(DeclareSyntaxCat),
        b"deriving" => Kw::Decl(Deriving),
        b"simproc"
        | b"simproc_decl"
        | b"dsimproc"
        | b"dsimproc_decl"
        | b"builtin_simproc"
        | b"builtin_dsimproc"
        | b"builtin_simproc_decl"
        | b"builtin_dsimproc_decl" => Kw::Decl(Simproc),
        b"declare_config_elab" | b"declare_command_config_elab" | b"declare_core_config_elab" => {
            Kw::Decl(ConfigElab)
        }
        b"unif_hint" => Kw::Decl(UnifHint),
        b"instance" => Kw::Instance,
        b"private" => Kw::Modifier(Modifier::Private),
        b"public" => Kw::Modifier(Modifier::Public),
        b"protected" => Kw::Modifier(Modifier::Protected),
        b"meta" => Kw::Modifier(Modifier::Meta),
        b"noncomputable" => Kw::Modifier(Modifier::Noncomputable),
        b"unsafe" => Kw::Modifier(Modifier::Unsafe),
        b"partial" => Kw::Modifier(Modifier::Partial),
        b"nonrec" => Kw::Modifier(Modifier::Nonrec),
        b"scoped" => Kw::Modifier(Modifier::Scoped),
        b"local" => Kw::Modifier(Modifier::Local),
        b"namespace" => Kw::Scope(ScopeKw::Namespace),
        b"section" => Kw::Scope(ScopeKw::Section),
        b"end" => Kw::Scope(ScopeKw::End),
        b"mutual" => Kw::Scope(ScopeKw::Mutual),
        b"import"
        | b"export"
        | b"variable"
        | b"universe"
        | b"attribute"
        | b"include"
        | b"omit"
        | b"unseal"
        | b"seal"
        | b"library_note"
        | b"add_decl_doc"
        | b"assert_not_exists"
        | b"assert_not_imported"
        | b"run_cmd"
        | b"run_elab"
        | b"run_meta"
        | b"compile_inductive"
        | b"compile_def"
        | b"suppress_compilation"
        | b"unsuppress_compilation"
        | b"initialize_simps_projections"
        | b"register_simp_attr"
        | b"register_label_attr"
        | b"declare_aesop_rule_sets"
        | b"declare_simp_like_tactic"
        | b"mk_iff_of_inductive_prop"
        | b"with_weak_namespace"
        | b"open_locale"
        | b"irreducible_extension"
        | b"to_additive_ignore_args"
        | b"grind_pattern"
        | b"deprecated_module"
        | b"add_aesop_rules"
        | b"elab_stx_quot"
        | b"deriving_handler"
        | b"register_try?_tactic"
        | b"bound_lemma"
        | b"lemma_wanted"
        | b"init_quot"
        | b"unlock_limits"
        | b"docs_to_verso"
        | b"register_hint"
        | b"register_error_explanation"
        | b"set_premise_selector"
        | b"insert_to_additive_translation"
        | b"attribute_wanted"
        | b"copy_doc_string"
        | b"universes" => Kw::Command,
        b"open" | b"set_option" | b"module" => Kw::SoftCommand,
        b"where"
        | b"with"
        | b"extends"
        | b"termination_by"
        | b"decreasing_by"
        | b"in"
        | b"then"
        | b"else"
        | b"at"
        | b"from"
        | b"using"
        | b"fun"
        | b"by"
        | b"do"
        | b"if"
        | b"match"
        | b"have"
        | b"show"
        | b"let"
        | b"calc"
        | b"catch"
        | b"try"
        | b"return"
        | b"unless"
        | b"mut"
        | b"monotonicity_by"
        | b"for"
        | b"_"
        | b"suffices"
        | b"obtain"
        | b"given"
        | b"requires"
        | b"ensures"
        | b"forall"
        | b"exists"
        | b"finally"
        | b"partial_fixpoint"
        | b"inductive_fixpoint"
        | b"coinductive_fixpoint" => Kw::Continue,
        _ => Kw::None,
    }
}

/// Common tactic names. A line starting with one is never taken for a user-defined command,
/// even when a tactic block runs back to column zero.
#[inline]
pub fn is_tactic_word(t: &[u8]) -> bool {
    matches!(
        t,
        b"exact"
            | b"exacts"
            | b"refine"
            | b"refine'"
            | b"intro"
            | b"intros"
            | b"rintro"
            | b"rw"
            | b"rwa"
            | b"erw"
            | b"simp"
            | b"simpa"
            | b"simp_all"
            | b"dsimp"
            | b"simp_rw"
            | b"apply"
            | b"constructor"
            | b"cases"
            | b"rcases"
            | b"induction"
            | b"use"
            | b"norm_num"
            | b"linarith"
            | b"nlinarith"
            | b"ring"
            | b"ring_nf"
            | b"omega"
            | b"aesop"
            | b"decide"
            | b"rfl"
            | b"trivial"
            | b"assumption"
            | b"contradiction"
            | b"exfalso"
            | b"ext"
            | b"funext"
            | b"congr"
            | b"convert"
            | b"unfold"
            | b"positivity"
            | b"gcongr"
            | b"field_simp"
            | b"push_neg"
            | b"by_contra"
            | b"by_cases"
            | b"specialize"
            | b"classical"
            | b"infer_instance"
            | b"tauto"
            | b"grind"
            | b"subst"
            | b"split"
            | b"split_ifs"
            | b"left"
            | b"right"
            | b"symm"
            | b"change"
            | b"show"
            | b"repeat"
            | b"first"
            | b"try"
            | b"all_goals"
            | b"any_goals"
            | b"case"
            | b"next"
            | b"filter_upwards"
            | b"refine_lift"
            | b"norm_cast"
            | b"push_cast"
            | b"exact_mod_cast"
            | b"apply_fun"
            | b"nth_rw"
            | b"nth_rewrite"
            | b"rewrite"
            | b"generalize"
            | b"set"
            | b"clear"
            | b"revert"
            | b"fin_cases"
            | b"interval_cases"
            | b"mono"
            | b"bound"
            | b"fun_prop"
    )
}

/// Keywords after which a term is still expected.
#[inline]
pub fn expects_more(t: &[u8]) -> bool {
    matches!(
        t,
        b"by"
            | b"fun"
            | b"with"
            | b"at"
            | b"from"
            | b"then"
            | b"else"
            | b"do"
            | b"in"
            | b"if"
            | b"match"
            | b"have"
            | b"show"
            | b"let"
            | b"calc"
            | b"where"
            | b"deriving"
            | b"extends"
            | b"termination_by"
            | b"decreasing_by"
            | b"using"
            | b"generalizing"
            | b"suffices"
            | b"obtain"
            | b"for"
            | b"forall"
            | b"exists"
            | b"and"
            | b"or"
            | b"of"
            | b"to"
    )
}
