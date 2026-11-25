//! Turns the model's answers into one description per target.
//!
//! Each answer should hold one `### Full.Name` section per name its request listed, the
//! description on the lines below the header; a JSON object keyed by the names is accepted
//! too. A name that is missing or empty is reported so that it can be asked for again; a name
//! that no request listed is counted as invented and ignored. Routed names are filled in
//! afterwards: an alias copies its target's description, and a `reassoc` or `elementwise`
//! lemma copies its parent's with one sentence added. A route whose source has no description
//! is reported as missing too.

use crate::plan::{Batch, Route};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashMap, HashSet};

/// The raw text the model returned for one request.
#[derive(Clone, Debug, Deserialize)]
pub struct Response {
    pub batch: String,
    pub content: String,
}

/// A description and where it came from.
#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct Description {
    pub name: String,
    pub description: String,
    /// `model`, `fallback`, `alias`, `reassoc` or `elementwise`.
    pub route: String,
}

/// The result of assembling every request of a run.
#[derive(Debug, Default, Serialize)]
pub struct Assembly {
    pub descriptions: Vec<Description>,
    /// Names that still have no description.
    pub missing: Vec<String>,
    /// Keys the model returned that its request did not ask for.
    pub invented: usize,
    /// Requests whose answer was absent or not a JSON object.
    pub malformed: Vec<String>,
}

/// The JSON object in a model answer, with or without a Markdown code fence around it.
pub fn extract_object(content: &str) -> Option<serde_json::Map<String, serde_json::Value>> {
    let start = content.find('{')?;
    let end = content.rfind('}')?;
    if end < start {
        return None;
    }
    match serde_json::from_str(&content[start..=end]) {
        Ok(serde_json::Value::Object(object)) => Some(object),
        _ => None,
    }
}

/// Adds the fixed sentence of a derived lemma to its parent's description.
///
/// A description of the form `**Title.** Body` keeps that form, with the rule named in the
/// title.
pub fn derive(rule: &str, parent: &str) -> String {
    let (label, sentence) = match rule {
        "reassoc" => (
            "reassociated form",
            "The same equality also holds after composing both sides on the right with an \
             arbitrary further morphism, with the composites associated to the right.",
        ),
        _ => (
            "elementwise form",
            "The same equality also holds pointwise: both sides agree when applied to an \
             arbitrary element.",
        ),
    };
    let parent = parent.trim();
    if let Some(rest) = parent.strip_prefix("**")
        && let Some((title, body)) = rest.split_once("**")
    {
        let title = title.trim().trim_end_matches('.');
        return format!("**{title} ({label}).** {} {sentence}", body.trim());
    }
    format!("{parent} {sentence}")
}

/// The `### Name` sections of an answer, by name. Code fence lines are dropped.
fn sections(content: &str) -> HashMap<String, String> {
    let mut found = HashMap::new();
    let mut current: Option<(String, Vec<&str>)> = None;
    let mut close = |section: Option<(String, Vec<&str>)>| {
        if let Some((name, body)) = section {
            found.insert(name, body.join("\n").trim().to_string());
        }
    };
    for line in content.lines() {
        if let Some(header) = line.trim_start().strip_prefix("### ") {
            close(current.take());
            current = Some((header.trim().trim_matches('`').to_string(), Vec::new()));
        } else if let Some((_, body)) = current.as_mut()
            && !line.trim_start().starts_with("```")
        {
            body.push(line);
        }
    }
    close(current);
    found
}

/// The descriptions in a model answer, by name: its `### Name` sections, or else the string
/// values of its JSON object. None if the answer has neither.
pub fn extract_answers(content: &str) -> Option<HashMap<String, String>> {
    let found = sections(content);
    if !found.is_empty() {
        return Some(found);
    }
    let object = extract_object(content)?;
    Some(
        object
            .into_iter()
            .filter_map(|(name, value)| {
                let text = value.as_str()?.to_string();
                Some((name.trim_matches('`').to_string(), text))
            })
            .collect(),
    )
}

fn record_model_answers(
    batches: &[Batch],
    responses: &[Response],
    found: &mut BTreeMap<String, (String, String)>,
    assembly: &mut Assembly,
) {
    let by_batch: HashMap<&str, &str> = responses
        .iter()
        .map(|response| (response.batch.as_str(), response.content.as_str()))
        .collect();
    for batch in batches {
        let Some(answers) = by_batch
            .get(batch.id.as_str())
            .and_then(|content| extract_answers(content))
        else {
            assembly.malformed.push(batch.id.clone());
            continue;
        };
        for item in &batch.items {
            let text = answers.get(&item.name).map(|text| text.trim());
            if let Some(text) = text.filter(|text| !text.is_empty()) {
                found.insert(item.name.clone(), (text.to_string(), "model".to_string()));
            }
        }
        let asked: HashSet<&str> = batch.items.iter().map(|i| i.name.as_str()).collect();
        assembly.invented += answers
            .keys()
            .filter(|name| !asked.contains(name.as_str()))
            .count();
    }
}

/// Resolves routes until no more can be resolved, so that chains such as an alias of a
/// `reassoc` lemma are followed.
fn resolve_routes(
    routes: &[Route],
    found: &mut BTreeMap<String, (String, String)>,
    known: &HashMap<String, String>,
) {
    loop {
        let mut progressed = false;
        for route in routes {
            if found.contains_key(&route.name) {
                continue;
            }
            let source = found
                .get(&route.from)
                .map(|(text, _)| text.clone())
                .or_else(|| known.get(&route.from).cloned());
            if let Some(source) = source {
                let text = match route.rule.as_str() {
                    "alias" => source,
                    rule => derive(rule, &source),
                };
                found.insert(route.name.clone(), (text, route.rule.clone()));
                progressed = true;
            }
        }
        if !progressed {
            return;
        }
    }
}

/// Assembles the descriptions of a run.
///
/// # Arguments
/// * `batches` - the requests of the plan.
/// * `routes` - the routed names of the plan.
/// * `responses` - the model's answer to each request.
/// * `fallback` - descriptions written one declaration at a time, for unmatched targets and
///   for names a request missed; a model answer takes precedence.
/// * `known` - existing descriptions of names outside the targets, for aliases of them.
pub fn assemble(
    batches: &[Batch],
    routes: &[Route],
    responses: &[Response],
    fallback: &HashMap<String, String>,
    known: &HashMap<String, String>,
) -> Assembly {
    let mut assembly = Assembly::default();
    let mut found: BTreeMap<String, (String, String)> = BTreeMap::new();
    record_model_answers(batches, responses, &mut found, &mut assembly);
    for (name, text) in fallback {
        found
            .entry(name.clone())
            .or_insert_with(|| (text.clone(), "fallback".to_string()));
    }
    resolve_routes(routes, &mut found, known);
    let wanted = batches
        .iter()
        .flat_map(|batch| batch.items.iter().map(|item| &item.name))
        .chain(routes.iter().map(|route| &route.name));
    for name in wanted {
        if !found.contains_key(name) {
            assembly.missing.push(name.clone());
        }
    }
    assembly.missing.sort();
    assembly.missing.dedup();
    assembly.descriptions = found
        .into_iter()
        .map(|(name, (description, route))| Description {
            name,
            description,
            route,
        })
        .collect();
    assembly
}
