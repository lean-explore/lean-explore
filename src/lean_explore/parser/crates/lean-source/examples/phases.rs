//! Times the lexer and the scanner separately over a directory of Lean files.
//! usage: cargo run --release --example phases DIR [LIMIT]

use std::path::Path;
use std::time::Instant;

fn collect(dir: &Path, out: &mut Vec<Vec<u8>>) {
    let entries: Vec<_> = std::fs::read_dir(dir)
        .unwrap()
        .filter_map(Result::ok)
        .collect();
    for e in entries {
        let p = e.path();
        if p.is_dir() {
            collect(&p, out);
        } else if p.extension().is_some_and(|x| x == "lean") {
            out.push(std::fs::read(&p).unwrap());
        }
    }
}

fn main() {
    let mut args = std::env::args().skip(1);
    let dir = args.next().expect("DIR");
    let limit: usize = args.next().map_or(usize::MAX, |s| s.parse().unwrap());
    let mut files = Vec::new();
    collect(Path::new(&dir), &mut files);
    files.truncate(limit);
    let bytes: usize = files.iter().map(Vec::len).sum();
    let mut toks_all = Vec::with_capacity(files.len());
    let t = Instant::now();
    for f in &files {
        let mut toks = Vec::with_capacity(f.len() / 5);
        lean_source::lexer::lex(f, &mut toks);
        toks_all.push(toks);
    }
    let lex = t.elapsed();
    let t = Instant::now();
    let mut decls = 0;
    for (f, toks) in files.iter().zip(&toks_all) {
        decls += lean_source::scan::parse_tokens(f, toks).decls.len();
    }
    let scan = t.elapsed();
    let ntoks: usize = toks_all.iter().map(Vec::len).sum();
    println!(
        "files {} bytes {} tokens {} decls {} lex {:.1} ms scan {:.1} ms",
        files.len(),
        bytes,
        ntoks,
        decls,
        lex.as_secs_f64() * 1e3,
        scan.as_secs_f64() * 1e3
    );
}
