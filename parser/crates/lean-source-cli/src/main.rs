//! `lean-source`: parse Lean files or whole trees and print one JSON object per declaration.

#[global_allocator]
static ALLOC: mimalloc::MiMalloc = mimalloc::MiMalloc;

use lean_source::json::{JsonOptions, write_commands, write_decls};
use rayon::prelude::*;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::Instant;

const USAGE: &str = "\
usage: lean-source [options] PATH...

Parses .lean files (directories are searched recursively) and writes one JSON object per
declaration to standard output.

options:
  --root DIR     compute module names relative to DIR (default: each directory argument)
  --jobs N       number of worker threads (default: all cores)
  --text         include signature and doc comment text
  --commands     write one object per command (kind, range, namespace) instead
  --no-project   read each file on its own: faster, but names that to_additive and to_dual
                 generate are not resolved against the other files
  --translation-ranges-at-name
                 record to_additive/to_dual ranges at the written name (newer Mathlib)
  --mmap         map files into memory instead of reading them
  --stats        parse only; print file, byte, declaration counts and timings to stderr
  -o FILE        write to FILE instead of standard output
";

struct Opts {
    paths: Vec<PathBuf>,
    root: Option<PathBuf>,
    jobs: Option<usize>,
    text: bool,
    commands: bool,
    name_ranges: bool,
    no_project: bool,
    mmap: bool,
    stats: bool,
    out: Option<PathBuf>,
}

fn args() -> Result<Opts, String> {
    let mut o = Opts {
        paths: vec![],
        root: None,
        jobs: None,
        text: false,
        commands: false,
        name_ranges: false,
        no_project: false,
        mmap: false,
        stats: false,
        out: None,
    };
    let mut it = std::env::args().skip(1);
    while let Some(a) = it.next() {
        match a.as_str() {
            "--root" => o.root = Some(it.next().ok_or("--root needs a value")?.into()),
            "--jobs" => {
                o.jobs = Some(
                    it.next()
                        .ok_or("--jobs needs a value")?
                        .parse()
                        .map_err(|_| "bad --jobs")?,
                )
            }
            "--text" => o.text = true,
            "--commands" => o.commands = true,
            "--translation-ranges-at-name" => o.name_ranges = true,
            "--no-project" => o.no_project = true,
            "--mmap" => o.mmap = true,
            "--stats" => o.stats = true,
            "-o" => o.out = Some(it.next().ok_or("-o needs a value")?.into()),
            "-h" | "--help" => return Err(String::new()),
            _ if a.starts_with('-') => return Err(format!("unknown option {a}")),
            _ => o.paths.push(a.into()),
        }
    }
    if o.paths.is_empty() {
        return Err(String::new());
    }
    Ok(o)
}

/// A file to parse and the module name it gets.
struct Job {
    path: PathBuf,
    file: String,
    module: String,
}

fn module_name(rel: &Path) -> String {
    let s = rel.with_extension("");
    s.components()
        .map(|c| c.as_os_str().to_string_lossy().into_owned())
        .collect::<Vec<_>>()
        .join(".")
}

fn collect(o: &Opts) -> Vec<Job> {
    let mut jobs = Vec::new();
    for p in &o.paths {
        if p.is_dir() {
            let root = o.root.clone().unwrap_or_else(|| p.clone());
            for e in walkdir::WalkDir::new(p)
                .sort_by_file_name()
                .into_iter()
                .filter_map(Result::ok)
            {
                let path = e.path();
                if e.file_type().is_file() && path.extension().is_some_and(|x| x == "lean") {
                    let rel = path.strip_prefix(&root).unwrap_or(path);
                    jobs.push(Job {
                        path: path.to_path_buf(),
                        file: rel.to_string_lossy().into_owned(),
                        module: module_name(rel),
                    });
                }
            }
        } else {
            let rel = match &o.root {
                Some(r) => p.strip_prefix(r).unwrap_or(p).to_path_buf(),
                None => PathBuf::from(p.file_name().unwrap_or_default()),
            };
            jobs.push(Job {
                path: p.clone(),
                file: rel.to_string_lossy().into_owned(),
                module: module_name(&rel),
            });
        }
    }
    jobs
}

enum Data {
    Vec(Vec<u8>),
    Map(memmap2::Mmap),
}

impl std::ops::Deref for Data {
    type Target = [u8];
    fn deref(&self) -> &[u8] {
        match self {
            Data::Vec(v) => v,
            Data::Map(m) => m,
        }
    }
}

fn read(path: &Path, mmap: bool) -> std::io::Result<Data> {
    if mmap {
        let f = std::fs::File::open(path)?;
        if f.metadata()?.len() == 0 {
            return Ok(Data::Vec(Vec::new()));
        }
        // SAFETY: the file is only read; concurrent modification is the caller's responsibility.
        Ok(Data::Map(unsafe { memmap2::Mmap::map(&f)? }))
    } else {
        std::fs::read(path).map(Data::Vec)
    }
}

/// True if a file uses `to_additive` or `to_dual`, whose names depend on the project.
fn needs_context(data: &[u8]) -> bool {
    memchr::memmem::find(data, b"to_additive").is_some()
        || memchr::memmem::find(data, b"to_dual").is_some()
}

fn main() {
    let o = match args() {
        Ok(o) => o,
        Err(e) => {
            if !e.is_empty() {
                eprintln!("lean-source: {e}");
            }
            eprint!("{USAGE}");
            std::process::exit(2);
        }
    };
    if let Some(j) = o.jobs {
        rayon::ThreadPoolBuilder::new()
            .num_threads(j)
            .build_global()
            .ok();
    }
    let t0 = Instant::now();
    let jobs = collect(&o);
    let t_walk = t0.elapsed();
    let opts = JsonOptions { text: o.text };
    let t1 = Instant::now();
    // With several files, a first pass collects what the project declares and translates.
    // First pass: every file on its own.
    let first: Vec<Option<(Data, lean_source::Parsed)>> = jobs
        .par_iter()
        .map(|j| match read(&j.path, o.mmap) {
            Ok(d) => {
                let p = lean_source::parse(&d);
                Some((d, p))
            }
            Err(e) => {
                eprintln!("lean-source: {}: {e}", j.path.display());
                None
            }
        })
        .collect();
    // With several files, the names that to_additive and to_dual generate depend on the rest of
    // the project; files that use them are read again with that context.
    let ctx = (jobs.len() > 1 && !o.no_project)
        .then(|| lean_source::Context::build(first.iter().flatten().map(|x| &x.1), o.name_ranges));
    let results: Vec<(usize, usize, String)> = jobs
        .par_iter()
        .zip(first.into_par_iter())
        .map(|(j, first)| {
            let Some((data, parsed)) = first else {
                return (0, 0, String::new());
            };
            let parsed = match &ctx {
                Some(ctx) if needs_context(&data) => lean_source::parse_in(&data, ctx),
                _ => parsed,
            };
            let mut out = String::new();
            if o.commands {
                write_commands(&mut out, &j.file, &j.module, &data, &parsed);
            } else if !o.stats {
                write_decls(&mut out, &j.file, &j.module, &data, &parsed, opts);
            }
            (data.len(), parsed.decls.len(), out)
        })
        .collect();
    let t_parse = t1.elapsed();
    let bytes: usize = results.iter().map(|r| r.0).sum();
    let decls: usize = results.iter().map(|r| r.1).sum();
    if o.stats {
        eprintln!(
            "files {} bytes {} declarations {} threads {} walk {:.1} ms parse {:.1} ms ({:.0} MB/s)",
            jobs.len(),
            bytes,
            decls,
            rayon::current_num_threads(),
            t_walk.as_secs_f64() * 1e3,
            t_parse.as_secs_f64() * 1e3,
            bytes as f64 / t_parse.as_secs_f64() / 1e6
        );
        return;
    }
    let stdout = std::io::stdout();
    let mut w: Box<dyn Write> = match &o.out {
        Some(p) => Box::new(std::io::BufWriter::new(
            std::fs::File::create(p).expect("cannot create output"),
        )),
        None => Box::new(std::io::BufWriter::new(stdout.lock())),
    };
    for (_, _, s) in &results {
        w.write_all(s.as_bytes()).expect("write failed");
    }
    w.flush().expect("write failed");
}
