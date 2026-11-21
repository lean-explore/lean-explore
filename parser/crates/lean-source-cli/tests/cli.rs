use std::fs;
use std::path::PathBuf;
use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};

struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let suffix = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path =
            std::env::temp_dir().join(format!("lean-source-cli-{}-{suffix}", std::process::id()));
        fs::create_dir(&path).unwrap();
        Self(path)
    }
    fn command(&self) -> Command {
        let mut command = Command::new(env!("CARGO_BIN_EXE_lean-source"));
        command.arg("--root").arg(&self.0).arg(&self.0);
        command
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}

#[test]
fn preserves_order_across_output_batches() {
    let fixture = Fixture::new();
    for i in 0..1030 {
        fs::write(
            fixture.0.join(format!("File{i:04}.lean")),
            format!("def value{i} : Nat := {i}\n"),
        )
        .unwrap();
    }
    let output = fixture.command().arg("--jobs").arg("4").output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(output.stderr.is_empty());
    let text = String::from_utf8(output.stdout).unwrap();
    let lines: Vec<_> = text.lines().collect();
    assert_eq!(lines.len(), 1030);
    for (i, line) in lines.iter().enumerate() {
        assert!(
            line.contains(&format!("\"file\":\"File{i:04}.lean\"")),
            "{line}"
        );
        assert!(line.contains(&format!("\"name\":\"value{i}\"")), "{line}");
    }
    let commands = fixture.command().arg("--commands").output().unwrap();
    assert!(commands.status.success());
    assert_eq!(
        String::from_utf8(commands.stdout).unwrap().lines().count(),
        1030
    );
}

#[test]
fn read_failure_exits_nonzero_without_creating_output() {
    let fixture = Fixture::new();
    let missing = fixture.0.join("missing.lean");
    let destination = fixture.0.join("output.jsonl");
    let output = Command::new(env!("CARGO_BIN_EXE_lean-source"))
        .arg(&missing)
        .arg("-o")
        .arg(&destination)
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("missing.lean"));
    assert!(!destination.exists());
}

#[cfg(unix)]
#[test]
fn directory_enumeration_failure_exits_nonzero() {
    use std::os::unix::fs::PermissionsExt;
    let fixture = Fixture::new();
    let hidden = fixture.0.join("unreadable");
    fs::create_dir(&hidden).unwrap();
    fs::write(hidden.join("Hidden.lean"), "def hidden := 1\n").unwrap();
    fs::set_permissions(&hidden, fs::Permissions::from_mode(0o0)).unwrap();
    // Root and filesystems without Unix permission enforcement can still enumerate it.
    if fs::read_dir(&hidden).is_ok() {
        fs::set_permissions(&hidden, fs::Permissions::from_mode(0o700)).unwrap();
        return;
    }
    let output = fixture.command().arg("--stats").output().unwrap();
    fs::set_permissions(&hidden, fs::Permissions::from_mode(0o700)).unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("unreadable"));
}

#[test]
fn imported_context_resolves_parent_fields_without_emitting_imports() {
    let fixture = Fixture::new();
    let context = fixture.0.join("Base.lean");
    let target = fixture.0.join("Child.lean");
    fs::write(&context, "structure Base where\n  x : Nat\n").unwrap();
    fs::write(
        &target,
        "structure Child extends Base where\n  x := 1\n  y : Nat\n",
    )
    .unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_lean-source"))
        .arg("--context")
        .arg(&context)
        .arg(&target)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let text = String::from_utf8(output.stdout).unwrap();
    assert!(text.contains("\"name\":\"Child.y\""));
    assert!(!text.contains("\"name\":\"Child.x\""));
    assert!(!text.contains("\"file\":\"Base.lean\""));
    let stats = Command::new(env!("CARGO_BIN_EXE_lean-source"))
        .arg("--context")
        .arg(&context)
        .arg(&target)
        .arg("--stats")
        .output()
        .unwrap();
    assert!(stats.status.success());
    let text = String::from_utf8(stats.stderr).unwrap();
    assert!(text.contains("context files 1 bytes "));
    assert!(text.contains("\nfiles 1 bytes "));
}

#[test]
fn context_and_no_project_are_incompatible() {
    let fixture = Fixture::new();
    let output = fixture
        .command()
        .arg("--context")
        .arg(&fixture.0)
        .arg("--no-project")
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    assert!(
        String::from_utf8_lossy(&output.stderr)
            .contains("--context cannot be combined with --no-project")
    );
}

#[test]
fn imported_simps_configuration_controls_generated_names() {
    let fixture = Fixture::new();
    let context = fixture.0.join("Configuration.lean");
    let target = fixture.0.join("Target.lean");
    fs::write(
        &context,
        "structure Carrier where\n  carrier : Nat\ninitialize_simps_projections Carrier (carrier → coe, as_prefix coe)\n",
    )
    .unwrap();
    fs::write(&target, "@[simps coe] def foo : Carrier := value\n").unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_lean-source"))
        .arg("--context")
        .arg(&context)
        .arg(&target)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let text = String::from_utf8(output.stdout).unwrap();
    assert!(text.contains("\"name\":\"coe_foo\""), "{text}");
    assert!(!text.contains("\"name\":\"foo_coe\""), "{text}");
    assert!(!text.contains("\"file\":\"Configuration.lean\""));
}

#[test]
fn mmap_rejects_output_aliases_without_truncating_sources() {
    let fixture = Fixture::new();
    let source = fixture.0.join("Source.lean");
    let original = b"def untouched : Nat := 1\n";
    fs::write(&source, original).unwrap();
    let hard_link = fixture.0.join("hard-link.jsonl");
    fs::hard_link(&source, &hard_link).unwrap();
    let aliases = vec![source.clone(), hard_link];
    #[cfg(unix)]
    let aliases = {
        let mut aliases = aliases;
        let symlink = fixture.0.join("symbolic-link.jsonl");
        std::os::unix::fs::symlink(&source, &symlink).unwrap();
        aliases.push(symlink);
        aliases
    };
    for alias in aliases {
        let result = Command::new(env!("CARGO_BIN_EXE_lean-source"))
            .arg(&source)
            .arg("--mmap")
            .arg("-o")
            .arg(&alias)
            .output()
            .unwrap();
        assert!(!result.status.success());
        assert!(String::from_utf8_lossy(&result.stderr).contains("output aliases input"));
        assert_eq!(fs::read(&source).unwrap(), original);
    }
    let destination = fixture.0.join("valid-output.jsonl");
    fs::write(&destination, b"old output").unwrap();
    let valid = Command::new(env!("CARGO_BIN_EXE_lean-source"))
        .arg(&source)
        .arg("--mmap")
        .arg("-o")
        .arg(&destination)
        .output()
        .unwrap();
    assert!(valid.status.success());
    assert!(
        fs::read_to_string(&destination)
            .unwrap()
            .contains("untouched")
    );
}
