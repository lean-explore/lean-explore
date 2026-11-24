use lean_source::{Context, parse, parse_in};

fn generated(parsed: &lean_source::Parsed) -> Vec<(String, bool)> {
    parsed
        .decls
        .iter()
        .filter(|d| d.origin.is_some())
        .map(|d| (d.name.clone().unwrap(), d.approximate))
        .collect()
}

#[test]
fn simps_prefix_configuration_uses_the_annotated_structure() {
    let source = b"namespace N\nstructure Box where\n  val : Nat\ninitialize_simps_projections Box (val -> coe, as_prefix coe)\n@[simps coe] def copy : Box := { val := 1 }\nend N\n";
    let parsed = parse(source);
    assert!(parsed.needs_simps_context);
    assert_eq!(generated(&parsed), [("N.coe_copy".into(), false)]);
}

#[test]
fn simps_configuration_resolves_across_files() {
    let config = parse(b"namespace N\nstructure Box where\n  val : Nat\n  other : Nat\ninitialize_simps_projections Box (val -> coe, as_prefix coe)\nend N\n");
    let source =
        b"namespace N\n@[simps coe other] def copy : Box := { val := 1, other := 2 }\nend N\n";
    let initial = parse(source);
    assert_eq!(
        generated(&initial),
        [("N.copy_coe".into(), true), ("N.copy_other".into(), true)]
    );
    let context = Context::build([&config, &initial], false);
    assert_eq!(
        generated(&parse_in(source, &context)),
        [("N.coe_copy".into(), false), ("N.copy_other".into(), false)]
    );
}

#[test]
fn simps_unknown_inferred_and_function_types_remain_approximate() {
    let source = b"structure Box where\n  val : Nat\ninitialize_simps_projections Box (val -> coe, as_prefix coe)\n@[simps coe] def inferred := Box.mk 1\n@[simps coe] def function : Box -> Box := fun x => x\n@[simps coe] def missing : Unknown := sorry\n@[simps] def default : Box := Box.mk 1\n";
    assert_eq!(
        generated(&parse(source)),
        [
            ("inferred_coe".into(), true),
            ("function_coe".into(), true),
            ("missing_coe".into(), true)
        ]
    );
}

#[test]
fn conflicting_simps_configs_remain_approximate() {
    let prefix = parse(b"structure Box where\n  val : Nat\ninitialize_simps_projections Box (val -> coe, as_prefix coe)\n");
    let suffix =
        parse(b"structure Box where\n  val : Nat\ninitialize_simps_projections Box (val -> coe)\n");
    let source = b"@[simps coe] def copy : Box := Box.mk 1\n";
    let context = Context::build([&prefix, &suffix], false);
    assert_eq!(
        generated(&parse_in(source, &context)),
        [("copy_coe".into(), true)]
    );
}
