use lean_source::{Context, DeclKind, parse, parse_in};

#[test]
fn named_parent_selection_contains_only_the_projection_name() {
    let src = "structure S extends toLeft : Left Nat, toRight : Right Nat where\n";
    let parsed = parse(src.as_bytes());
    let parents: Vec<_> = parsed
        .decls
        .iter()
        .filter(|d| d.kind == DeclKind::ParentProjection)
        .collect();
    assert_eq!(parents.len(), 2);
    for (decl, name) in parents.iter().zip(["toLeft", "toRight"]) {
        assert_eq!(
            &src[decl.selection.start as usize..decl.selection.end as usize],
            name
        );
        assert_eq!(
            &src[decl.range.start as usize..decl.range.end as usize],
            src.trim_end()
        );
    }
}

#[test]
fn documented_inherited_defaults_do_not_declare_fields() {
    let src = "structure Base where\n  x : Nat\nstructure Child extends Base where\n  /-- inherited default -/\n  x := 0\n  /-- new field with inferred type -/\n  y := 1\n  z := 2\n";
    let parsed = parse(src.as_bytes());
    let fields: Vec<_> = parsed
        .decls
        .iter()
        .filter(|d| d.kind == DeclKind::Field)
        .collect();
    assert_eq!(
        fields
            .iter()
            .map(|d| d.name.as_deref().unwrap())
            .collect::<Vec<_>>(),
        ["Base.x", "Child.y", "Child.z"]
    );
    assert!(fields.iter().all(|d| !d.approximate));
}

#[test]
fn inherited_fields_are_resolved_across_files_and_namespaces() {
    let base = parse(b"namespace N\nstructure Base where\n  x : Nat\nstructure Middle extends Base where\n  y : Nat\nend N\n");
    let child = b"namespace N\nstructure Child extends Middle where\n  /-- inherited from Base -/\n  x := 0\n  y := 1\n  z := 2\nend N\n";
    let initial = parse(child);
    assert!(initial.needs_structure_context);
    let context = Context::build([&base, &initial], false);
    let parsed = parse_in(child, &context);
    let fields: Vec<_> = parsed
        .decls
        .iter()
        .filter(|d| d.kind == DeclKind::Field)
        .collect();
    assert_eq!(fields.len(), 1);
    assert_eq!(fields[0].name.as_deref(), Some("N.Child.z"));
    assert!(!fields[0].approximate);
}

#[test]
fn unavailable_parent_sources_leave_field_candidates_uncertain() {
    let parsed = parse(
        b"structure Child extends ExternalBase where\n  /-- could be inherited -/\n  x := 0\n",
    );
    let field = parsed
        .decls
        .iter()
        .find(|d| d.kind == DeclKind::Field)
        .unwrap();
    assert_eq!(field.name.as_deref(), Some("Child.x"));
    assert!(field.approximate);
}

#[test]
fn conflicting_parent_declarations_do_not_suppress_new_fields() {
    let left = parse(b"structure Base where\n  x : Nat\n");
    let right = parse(b"structure Base where\n  y : Nat\n");
    let src = b"structure Child extends Base where\n  x := 1\n";
    let context = Context::build([&left, &right], false);
    let parsed = parse_in(src, &context);
    let field = parsed
        .decls
        .iter()
        .find(|d| d.kind == DeclKind::Field)
        .unwrap();
    assert_eq!(field.name.as_deref(), Some("Child.x"));
    assert!(field.approximate);
}

#[test]
fn ext_ranges_exclude_synthetic_priorities_but_keep_configuration() {
    for (attribute, expected, count) in [
        ("ext high + 1", "ext", 2),
        ("ext 1000", "ext 1000", 2),
        ("ext (iff := false) high + 1", "ext (iff := false)", 1),
        ("ext (flat := false) high + 1", "ext (flat := false)", 2),
    ] {
        let src = format!("@[{attribute}] structure Probe where\n  x : Nat\n");
        let parsed = parse(src.as_bytes());
        let generated: Vec<_> = parsed.decls.iter().filter(|d| d.origin.is_some()).collect();
        assert_eq!(generated.len(), count);
        for d in generated {
            assert_eq!(&src[d.range.start as usize..d.range.end as usize], expected);
            assert_eq!(d.range, d.selection);
        }
    }
}

#[test]
fn ext_ranges_keep_simple_priorities_and_exclude_local_modifiers() {
    for (attribute, expected) in [
        ("ext high", "ext high"),
        ("ext high + 1", "ext"),
        ("local ext high", "ext high"),
        ("local ext", "ext"),
    ] {
        let source = format!("@[{attribute}] theorem equal : True := trivial\n");
        let parsed = parse(source.as_bytes());
        let generated = parsed.decls.iter().find(|d| d.generated).unwrap();
        assert_eq!(generated.name.as_deref(), Some("equal_iff"));
        assert_eq!(
            &source[generated.range.start as usize..generated.range.end as usize],
            expected
        );
        assert_eq!(generated.range, generated.selection);
    }
}

#[test]
fn existing_translations_register_namespaces_without_emitting_declarations() {
    let source = b"namespace N\nstructure IsSuccLimit where\n  x : Nat\n@[to_dual existing] structure IsPredLimit where\n  x : Nat\n@[to_dual] theorem IsSuccLimit.withTopCoe : True := trivial\nend N\n";
    let initial = parse(source);
    let context = Context::build([&initial], false);
    let parsed = parse_in(source, &context);
    let translated: Vec<_> = parsed.decls.iter().filter(|d| d.origin.is_some()).collect();
    assert_eq!(translated.len(), 1);
    assert_eq!(
        translated[0].name.as_deref(),
        Some("N.IsPredLimit.withBotCoe")
    );
}
