//! `lean-informal`: plan per-file informalization requests and assemble the model's answers.

use lean_informal::assemble::{Response, assemble};
use lean_informal::input::{ContextEntry, Knowledge, Target, read_jsonl, read_names};
use lean_informal::plan::{
    Batch, Budget, FilePlan, Reference, Route, Unmatched, plan_file, references,
};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashMap};
use std::io::Write;
use std::path::{Path, PathBuf};

const USAGE: &str = "\
usage:
  lean-informal plan --sources DIR --targets FILE --context FILE --common FILE --out DIR
                     [--batch-tokens N] [--batch-items N] [--context-tokens N]
                     [--description-chars N]
  lean-informal assemble --plan DIR --responses FILE --context FILE --out DIR
                         [--fallback FILE]
  lean-informal statements --sources DIR --targets FILE --out FILE

plan      reads the Lean files under --sources and writes batches.jsonl (one request per
          line), routes.jsonl (names described without the model), unmatched.jsonl (names
          left to the per-declaration prompt) and plan.json (counts) to --out.
assemble  checks the answers in --responses ({\"batch\", \"content\"} per line) against the
          plan and writes descriptions.jsonl, missing.jsonl and assembly.json to --out.
          --fallback holds {\"name\", \"description\"} lines written one declaration at a time.
statements writes, for every target found in the sources, the statement and the scope it is
          read in: the reference a description is judged against.
";

fn main() {
    if let Err(message) = run() {
        eprintln!("lean-informal: {message}\n\n{USAGE}");
        std::process::exit(2);
    }
}

fn run() -> Result<(), String> {
    let mut arguments = std::env::args().skip(1);
    let command = arguments.next().ok_or("missing command")?;
    let options = parse_options(arguments)?;
    match command.as_str() {
        "plan" => plan(&options),
        "assemble" => assemble_answers(&options),
        "statements" => statements(&options),
        "--help" | "-h" => {
            print!("{USAGE}");
            Ok(())
        }
        other => Err(format!("unknown command {other}")),
    }
}

/// Reads `--key value` pairs.
fn parse_options(
    arguments: impl Iterator<Item = String>,
) -> Result<HashMap<String, String>, String> {
    let mut options = HashMap::new();
    let mut arguments = arguments.peekable();
    while let Some(key) = arguments.next() {
        let key = key
            .strip_prefix("--")
            .ok_or_else(|| format!("unexpected argument {key}"))?
            .to_string();
        let value = arguments
            .next()
            .ok_or_else(|| format!("--{key} needs a value"))?;
        options.insert(key, value);
    }
    Ok(options)
}

fn path_option(options: &HashMap<String, String>, key: &str) -> Result<PathBuf, String> {
    options
        .get(key)
        .map(PathBuf::from)
        .ok_or_else(|| format!("--{key} is required"))
}

fn number_option(
    options: &HashMap<String, String>,
    key: &str,
    default: usize,
) -> Result<usize, String> {
    options.get(key).map_or(Ok(default), |value| {
        value
            .parse()
            .map_err(|_| format!("--{key} must be a number"))
    })
}

fn write_jsonl<T: Serialize>(path: &Path, values: &[T]) -> Result<(), String> {
    let mut file = std::io::BufWriter::new(
        std::fs::File::create(path).map_err(|e| format!("{}: {e}", path.display()))?,
    );
    for value in values {
        let line = serde_json::to_string(value).map_err(|e| e.to_string())?;
        writeln!(file, "{line}").map_err(|e| format!("{}: {e}", path.display()))?;
    }
    file.flush().map_err(|e| format!("{}: {e}", path.display()))
}

fn write_json<T: Serialize>(path: &Path, value: &T) -> Result<(), String> {
    let text = serde_json::to_string_pretty(value).map_err(|e| e.to_string())?;
    std::fs::write(path, text + "\n").map_err(|e| format!("{}: {e}", path.display()))
}

fn knowledge(options: &HashMap<String, String>, targets: Vec<Target>) -> Result<Knowledge, String> {
    let context: Vec<ContextEntry> = read_jsonl(&path_option(options, "context")?)?;
    Ok(Knowledge {
        context: context.into_iter().map(|e| (e.name.clone(), e)).collect(),
        common: read_names(&path_option(options, "common")?)?,
        targets: targets.into_iter().map(|t| (t.name.clone(), t)).collect(),
    })
}

fn budget(options: &HashMap<String, String>) -> Result<Budget, String> {
    let default = Budget::default();
    Ok(Budget {
        batch_tokens: number_option(options, "batch-tokens", default.batch_tokens)?,
        batch_items: number_option(options, "batch-items", default.batch_items)?,
        context_tokens: number_option(options, "context-tokens", default.context_tokens)?,
        description_chars: number_option(options, "description-chars", default.description_chars)?,
    })
}

/// Counts written to plan.json.
#[derive(Serialize)]
struct PlanSummary {
    files: usize,
    targets: usize,
    batches: usize,
    model_items: usize,
    routes: BTreeMap<String, usize>,
    unmatched: BTreeMap<&'static str, usize>,
    estimated_request_tokens: usize,
    dropped_context: usize,
    batch_tokens: usize,
    batch_items: usize,
    context_tokens: usize,
    description_chars: usize,
}

fn plan_one(
    sources: &Path,
    path: &str,
    targets: &[&Target],
    knowledge: &Knowledge,
    budget: &Budget,
) -> FilePlan {
    match std::fs::read(sources.join(path)) {
        Ok(src) => plan_file(path, &targets[0].module, &src, targets, knowledge, budget),
        Err(_) => FilePlan {
            unmatched: targets
                .iter()
                .map(|target| Unmatched {
                    name: target.name.clone(),
                    path: Some(path.to_string()),
                    reason: "source file missing",
                })
                .collect(),
            ..FilePlan::default()
        },
    }
}

fn plan(options: &HashMap<String, String>) -> Result<(), String> {
    let sources = path_option(options, "sources")?;
    let out = path_option(options, "out")?;
    let budget = budget(options)?;
    let targets: Vec<Target> = read_jsonl(&path_option(options, "targets")?)?;
    let target_count = targets.len();
    let knowledge = knowledge(options, targets)?;
    let mut by_path: BTreeMap<&str, Vec<&Target>> = BTreeMap::new();
    let mut whole = FilePlan::default();
    for target in knowledge.targets.values() {
        match &target.path {
            Some(path) => by_path.entry(path.as_str()).or_default().push(target),
            None => whole.unmatched.push(Unmatched {
                name: target.name.clone(),
                path: None,
                reason: "no source location",
            }),
        }
    }
    for (path, targets) in &mut by_path {
        targets.sort_by_key(|target| (target.start_line, target.name.as_str()));
        let file = plan_one(&sources, path, targets, &knowledge, &budget);
        whole.batches.extend(file.batches);
        whole.routes.extend(file.routes);
        whole.unmatched.extend(file.unmatched);
    }
    whole.unmatched.sort_by(|a, b| a.name.cmp(&b.name));
    std::fs::create_dir_all(&out).map_err(|e| format!("{}: {e}", out.display()))?;
    write_jsonl(&out.join("batches.jsonl"), &whole.batches)?;
    write_jsonl(&out.join("routes.jsonl"), &whole.routes)?;
    write_jsonl(&out.join("unmatched.jsonl"), &whole.unmatched)?;
    write_json(
        &out.join("plan.json"),
        &summarize(&whole, by_path.len(), target_count, &budget),
    )
}

fn summarize(whole: &FilePlan, files: usize, targets: usize, budget: &Budget) -> PlanSummary {
    let mut routes = BTreeMap::new();
    for route in &whole.routes {
        *routes.entry(route.rule.clone()).or_default() += 1;
    }
    let mut unmatched = BTreeMap::new();
    for entry in &whole.unmatched {
        *unmatched.entry(entry.reason).or_default() += 1;
    }
    PlanSummary {
        files,
        targets,
        batches: whole.batches.len(),
        model_items: whole.batches.iter().map(|b| b.items.len()).sum(),
        routes,
        unmatched,
        estimated_request_tokens: whole.batches.iter().map(|b| b.estimated_tokens).sum(),
        dropped_context: whole.batches.iter().map(|b| b.dropped_context).sum(),
        batch_tokens: budget.batch_tokens,
        batch_items: budget.batch_items,
        context_tokens: budget.context_tokens,
        description_chars: budget.description_chars,
    }
}

#[derive(Deserialize)]
struct FallbackLine {
    name: String,
    description: String,
}

/// Counts written to assembly.json.
#[derive(Serialize)]
struct AssemblySummary {
    descriptions: usize,
    by_route: BTreeMap<String, usize>,
    missing: usize,
    invented: usize,
    malformed: Vec<String>,
}

fn assemble_answers(options: &HashMap<String, String>) -> Result<(), String> {
    let plan_directory = path_option(options, "plan")?;
    let out = path_option(options, "out")?;
    let batches: Vec<Batch> = read_jsonl(&plan_directory.join("batches.jsonl"))?;
    let routes: Vec<Route> = read_jsonl(&plan_directory.join("routes.jsonl"))?;
    let responses: Vec<Response> = read_jsonl(&path_option(options, "responses")?)?;
    let fallback: HashMap<String, String> = match options.get("fallback") {
        Some(path) => read_jsonl::<FallbackLine>(Path::new(path))?
            .into_iter()
            .map(|line| (line.name, line.description))
            .collect(),
        None => HashMap::new(),
    };
    let context: Vec<ContextEntry> = read_jsonl(&path_option(options, "context")?)?;
    let known: HashMap<String, String> = context
        .into_iter()
        .filter_map(|entry| entry.description.map(|text| (entry.name, text)))
        .collect();
    let assembly = assemble(&batches, &routes, &responses, &fallback, &known);
    let mut by_route = BTreeMap::new();
    for description in &assembly.descriptions {
        *by_route.entry(description.route.clone()).or_default() += 1;
    }
    std::fs::create_dir_all(&out).map_err(|e| format!("{}: {e}", out.display()))?;
    write_jsonl(&out.join("descriptions.jsonl"), &assembly.descriptions)?;
    let missing: Vec<serde_json::Value> = assembly
        .missing
        .iter()
        .map(|name| serde_json::json!({ "name": name }))
        .collect();
    write_jsonl(&out.join("missing.jsonl"), &missing)?;
    write_json(
        &out.join("assembly.json"),
        &AssemblySummary {
            descriptions: assembly.descriptions.len(),
            by_route,
            missing: assembly.missing.len(),
            invented: assembly.invented,
            malformed: assembly.malformed,
        },
    )
}

fn statements(options: &HashMap<String, String>) -> Result<(), String> {
    let sources = path_option(options, "sources")?;
    let targets: Vec<Target> = read_jsonl(&path_option(options, "targets")?)?;
    let mut by_path: BTreeMap<&str, Vec<&Target>> = BTreeMap::new();
    for target in &targets {
        if let Some(path) = &target.path {
            by_path.entry(path.as_str()).or_default().push(target);
        }
    }
    let mut found: Vec<Reference> = Vec::new();
    for (path, targets) in &by_path {
        if let Ok(src) = std::fs::read(sources.join(path)) {
            found.extend(references(path, &targets[0].module, &src, targets));
        }
    }
    found.sort_by(|a, b| a.name.cmp(&b.name));
    write_jsonl(&path_option(options, "out")?, &found)
}
