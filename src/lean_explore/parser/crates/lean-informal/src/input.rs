//! Inputs of a plan: the declarations to describe and what is known about their dependencies.

use serde::Deserialize;
use serde::de::DeserializeOwned;
use std::collections::{HashMap, HashSet};
use std::io::{BufRead, BufReader};
use std::path::Path;

/// A declaration to describe, as exported from the search database.
#[derive(Clone, Debug, Deserialize)]
pub struct Target {
    /// Full name of the declaration.
    pub name: String,
    pub module: String,
    /// Path of the source file in its repository, or None when the database has no location.
    pub path: Option<String>,
    /// First line of the declaration range, doc comment and attributes included (1-based).
    pub start_line: Option<u32>,
    #[serde(default)]
    pub dependencies: Vec<String>,
}

/// A dependency with its existing description.
#[derive(Clone, Debug, Deserialize)]
pub struct ContextEntry {
    pub name: String,
    pub description: Option<String>,
    /// Number of declarations in the database that depend on this name.
    #[serde(default)]
    pub frequency: u64,
}

/// Everything a plan may look up across files.
#[derive(Debug, Default)]
pub struct Knowledge {
    /// Existing descriptions and dependency frequencies by name.
    pub context: HashMap<String, ContextEntry>,
    /// Names that prompts leave out because every model knows them (`Eq`, `HAdd.hAdd`, ...).
    pub common: HashSet<String>,
    /// Every target, by name.
    pub targets: HashMap<String, Target>,
}

impl Knowledge {
    /// True if `name` is a target or a dependency with an existing description.
    pub fn is_described(&self, name: &str) -> bool {
        self.targets.contains_key(name)
            || self
                .context
                .get(name)
                .is_some_and(|entry| entry.description.is_some())
    }
}

/// Reads one JSON value per non-empty line.
pub fn read_jsonl<T: DeserializeOwned>(path: &Path) -> Result<Vec<T>, String> {
    let file = std::fs::File::open(path).map_err(|e| format!("{}: {e}", path.display()))?;
    let mut values = Vec::new();
    for (number, line) in BufReader::new(file).lines().enumerate() {
        let line = line.map_err(|e| format!("{}: {e}", path.display()))?;
        if line.trim().is_empty() {
            continue;
        }
        let value = serde_json::from_str(&line)
            .map_err(|e| format!("{}:{}: {e}", path.display(), number + 1))?;
        values.push(value);
    }
    Ok(values)
}

/// Reads a list of names, one per line, skipping blank lines and `#` comments.
pub fn read_names(path: &Path) -> Result<HashSet<String>, String> {
    let text = std::fs::read_to_string(path).map_err(|e| format!("{}: {e}", path.display()))?;
    Ok(text
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty() && !line.starts_with('#'))
        .map(String::from)
        .collect())
}
