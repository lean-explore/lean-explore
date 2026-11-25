//! The dependency descriptions sent with a request.
//!
//! A request describes several declarations, so their dependencies are pooled: each is listed
//! once however many statements use it. Candidates are ranked by how many declarations of the
//! request use them, then by how rarely the rest of the corpus does, since a rare name is the
//! one a model is least likely to know. Names described in the request itself, names on the
//! common list and names without a description are left out. The list stops at a token budget.

use crate::estimate_tokens;
use crate::input::Knowledge;
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};

/// One dependency line of a request.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct ContextLine {
    pub name: String,
    pub description: String,
}

/// The selected lines and how many candidates did not fit.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Selection {
    pub lines: Vec<ContextLine>,
    pub dropped: usize,
}

/// Shortens a description the way the current prompt does: at most `limit` characters, the
/// last three replaced by an ellipsis when it is cut.
pub fn truncate(description: &str, limit: usize) -> String {
    if description.chars().count() <= limit {
        return description.to_string();
    }
    let kept: String = description.chars().take(limit.saturating_sub(3)).collect();
    kept + "..."
}

/// The line as the prompt shows it.
pub fn render(line: &ContextLine) -> String {
    format!("- {}: {}", line.name, line.description)
}

/// Chooses the dependency lines of a request.
///
/// # Arguments
/// * `items` - names the request asks the model to describe.
/// * `knowledge` - dependencies of every target and existing descriptions.
/// * `token_budget` - the most tokens the lines may take together.
/// * `description_chars` - length at which each description is cut.
pub fn select(
    items: &[String],
    knowledge: &Knowledge,
    token_budget: usize,
    description_chars: usize,
) -> Selection {
    let in_request: HashSet<&str> = items.iter().map(String::as_str).collect();
    let mut uses: HashMap<&str, usize> = HashMap::new();
    for item in items {
        let Some(target) = knowledge.targets.get(item) else {
            continue;
        };
        for dependency in &target.dependencies {
            let known = knowledge
                .context
                .get(dependency)
                .is_some_and(|entry| entry.description.is_some());
            if known
                && !in_request.contains(dependency.as_str())
                && !knowledge.common.contains(dependency)
            {
                *uses.entry(dependency.as_str()).or_default() += 1;
            }
        }
    }
    let mut ranked: Vec<(&str, usize, u64)> = uses
        .into_iter()
        .map(|(name, count)| (name, count, knowledge.context[name].frequency))
        .collect();
    ranked.sort_by(|a, b| b.1.cmp(&a.1).then(a.2.cmp(&b.2)).then(a.0.cmp(b.0)));
    let mut selection = Selection::default();
    let mut spent = 0;
    for (name, _, _) in ranked {
        let description = knowledge.context[name].description.as_deref().unwrap_or("");
        let line = ContextLine {
            name: name.to_string(),
            description: truncate(description, description_chars),
        };
        let cost = estimate_tokens(&render(&line));
        if spent + cost > token_budget {
            selection.dropped += 1;
            continue;
        }
        spent += cost;
        selection.lines.push(line);
    }
    selection
}
