//! In-memory project parsing benchmark, separate from file discovery and reading.
//! Usage: cargo run --release -p lean-source-cli --example project_bench -- DIR [RUNS] [JOBS] [CONTEXT ...]

#[global_allocator]
static ALLOC: mimalloc::MiMalloc = mimalloc::MiMalloc;

use rayon::prelude::*;
use std::path::PathBuf;
use std::time::Instant;

fn memory_kib() -> (String, String) {
    let status = std::fs::read_to_string("/proc/self/status").unwrap_or_default();
    let value = |key: &str| {
        status
            .lines()
            .find_map(|line| {
                line.strip_prefix(key)
                    .and_then(|s| s.split_whitespace().next())
                    .and_then(|s| s.parse::<u64>().ok())
                    .map(|n| n.to_string())
            })
            .unwrap_or_else(|| "null".to_owned())
    };
    (value("VmRSS:"), value("VmHWM:"))
}

fn read_sources(roots: &[PathBuf]) -> Vec<Vec<u8>> {
    let paths: Vec<_> = roots
        .iter()
        .flat_map(|root| {
            walkdir::WalkDir::new(root)
                .sort_by_file_name()
                .into_iter()
                .map(|entry| entry.expect("cannot enumerate source directory"))
                .filter(|entry| {
                    entry.file_type().is_file()
                        && entry.path().extension().is_some_and(|x| x == "lean")
                })
                .map(|entry| entry.into_path())
        })
        .collect();
    paths
        .par_iter()
        .map(|path| std::fs::read(path).expect("cannot read source file"))
        .collect()
}

fn main() {
    let mut args = std::env::args().skip(1);
    let root = PathBuf::from(args.next().expect("DIR"));
    let runs: usize = args.next().map_or(8, |s| s.parse().expect("RUNS"));
    let jobs: usize = args.next().map_or(24, |s| s.parse().expect("JOBS"));
    let context_roots: Vec<PathBuf> = args.map(PathBuf::from).collect();
    assert!(runs > 0 && jobs > 0);
    rayon::ThreadPoolBuilder::new()
        .num_threads(jobs)
        .build_global()
        .unwrap();
    let start = Instant::now();
    let files = read_sources(&[root]);
    let context_files = read_sources(&context_roots);
    assert!(!files.is_empty(), "no Lean source files");
    let context_count = context_files.len();
    let context_bytes: usize = context_files.iter().map(Vec::len).sum();
    let bytes: usize = files.iter().map(Vec::len).sum();
    let load_ms = start.elapsed().as_secs_f64() * 1000.0;
    let (rss, peak) = memory_kib();
    println!(
        "{{\"phase\":\"load\",\"files\":{},\"bytes\":{bytes},\"threads\":{jobs},\"context_files\":{context_count},\"context_bytes\":{context_bytes},\"load_ms\":{load_ms:.3},\"rss_kib\":{rss},\"peak_rss_kib\":{peak}}}",
        files.len()
    );
    for run in 0..runs {
        let start = Instant::now();
        let context_parsed: Vec<_> = context_files
            .par_iter()
            .map(|source| lean_source::parse(source))
            .collect();
        let parsed: Vec<_> = files
            .par_iter()
            .map(|source| lean_source::parse(source))
            .collect();
        let context =
            lean_source::Context::build(context_parsed.iter().chain(parsed.iter()), false);
        let declarations: usize = files
            .par_iter()
            .zip(parsed.par_iter())
            .map(|(source, first)| {
                if first.needs_structure_context
                    || first.needs_simps_context
                    || memchr::memmem::find(source, b"to_additive").is_some()
                    || memchr::memmem::find(source, b"to_dual").is_some()
                {
                    lean_source::parse_in(source, &context).decls.len()
                } else {
                    first.decls.len()
                }
            })
            .sum();
        let parse_ms = start.elapsed().as_secs_f64() * 1000.0;
        let (rss, peak) = memory_kib();
        std::hint::black_box(&parsed);
        let release = Instant::now();
        drop(context);
        drop(context_parsed);
        drop(parsed);
        let release_ms = release.elapsed().as_secs_f64() * 1000.0;
        println!(
            "{{\"phase\":\"parse\",\"run\":{run},\"warmup\":{},\"files\":{},\"bytes\":{bytes},\"threads\":{jobs},\"context_files\":{context_count},\"context_bytes\":{context_bytes},\"declarations\":{declarations},\"parse_ms\":{parse_ms:.3},\"release_ms\":{release_ms:.3},\"rss_kib\":{rss},\"peak_rss_kib\":{peak}}}",
            run == 0,
            files.len()
        );
    }
}
