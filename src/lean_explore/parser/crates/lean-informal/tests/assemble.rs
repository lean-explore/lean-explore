use lean_informal::assemble::{Response, assemble, derive, extract_answers, extract_object};
use lean_informal::plan::{Batch, Item, Route};
use std::collections::HashMap;

fn batch(id: &str, names: &[&str]) -> Batch {
    Batch {
        id: id.to_string(),
        path: "Mathlib/Test.lean".to_string(),
        module: "Mathlib.Test".to_string(),
        excerpt: String::new(),
        items: names
            .iter()
            .map(|name| Item {
                name: name.to_string(),
                kind: "theorem".to_string(),
                note: None,
            })
            .collect(),
        context: Vec::new(),
        dropped_context: 0,
        estimated_tokens: 0,
    }
}

fn response(batch: &str, content: &str) -> Response {
    Response {
        batch: batch.to_string(),
        content: content.to_string(),
    }
}

fn route(name: &str, rule: &str, from: &str) -> Route {
    Route {
        name: name.to_string(),
        rule: rule.to_string(),
        from: from.to_string(),
    }
}

#[test]
fn objects_are_found_inside_code_fences() {
    let object = extract_object("Here:\n```json\n{\"A.b\": \"**B.** text\"}\n```").unwrap();
    assert_eq!(object["A.b"], "**B.** text");
    assert!(extract_object("no object").is_none());
    assert!(extract_object("[1, 2]").is_none());
}

#[test]
fn sections_are_read_by_name() {
    let answer =
        "```markdown\n### A.b\n**B.** First line.\nSecond line.\n\n### `A.c`\n**C.** Text.\n```";
    let answers = extract_answers(answer).unwrap();
    assert_eq!(answers["A.b"], "**B.** First line.\nSecond line.");
    assert_eq!(answers["A.c"], "**C.** Text.");
    let json = extract_answers("{\"`A.d`\": \"**D.** x\", \"A.e\": 3}").unwrap();
    assert_eq!(json.len(), 1);
    assert_eq!(json["A.d"], "**D.** x");
    assert!(extract_answers("nothing here").is_none());
}

#[test]
fn derived_descriptions_keep_the_title_form() {
    let text = derive("reassoc", "**Naturality of f.** The square commutes.");
    assert!(text.starts_with("**Naturality of f (reassociated form).** The square commutes. "));
    assert!(text.contains("composing both sides on the right"));
    let plain = derive("elementwise", "Plain text.");
    assert!(plain.starts_with("Plain text. The same equality also holds pointwise"));
}

#[test]
fn answers_routes_and_fallbacks_are_combined() {
    let batches = vec![batch("f#0", &["A.x", "A.y", "A.z"]), batch("f#1", &["A.w"])];
    let routes = vec![
        route("A.x_assoc", "reassoc", "A.x"),
        route("A.old", "alias", "A.x_assoc"),
        route("A.ext", "alias", "Outside.thing"),
        route("A.lost", "alias", "Nowhere"),
    ];
    let responses = vec![response(
        "f#0",
        "### A.x\n**X.** Holds.\n\n### `A.y`\n**Y.** Also.\n\n### A.z\n\n### A.invented\n?",
    )];
    let fallback = HashMap::from([
        ("A.z".to_string(), "**Z.** From fallback.".to_string()),
        ("A.x".to_string(), "ignored".to_string()),
    ]);
    let known = HashMap::from([("Outside.thing".to_string(), "**T.** Known.".to_string())]);
    let assembly = assemble(&batches, &routes, &responses, &fallback, &known);
    let by_name: HashMap<&str, (&str, &str)> = assembly
        .descriptions
        .iter()
        .map(|d| (d.name.as_str(), (d.description.as_str(), d.route.as_str())))
        .collect();
    assert_eq!(by_name["A.x"], ("**X.** Holds.", "model"));
    assert_eq!(by_name["A.y"], ("**Y.** Also.", "model"));
    assert_eq!(by_name["A.z"], ("**Z.** From fallback.", "fallback"));
    assert_eq!(by_name["A.x_assoc"].1, "reassoc");
    assert_eq!(by_name["A.old"].0, by_name["A.x_assoc"].0);
    assert_eq!(by_name["A.ext"], ("**T.** Known.", "alias"));
    assert_eq!(assembly.missing, ["A.lost", "A.w"]);
    assert_eq!(assembly.invented, 1);
    assert_eq!(assembly.malformed, ["f#1"]);
}
