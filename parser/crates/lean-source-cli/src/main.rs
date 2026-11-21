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
  --context PATH import source declarations for project context without emitting them (repeatable)
  --jobs N       number of worker threads (default: all cores)
  --text         include signature and doc comment text
  --commands     write one object per command (kind, range, namespace) instead
  --no-project   read each file on its own: generated names and inherited fields are not
                 resolved against the other files
  --translation-ranges-at-name
                 record to_additive/to_dual ranges at the written name (newer Mathlib)
  --mmap         map files into memory instead of reading them
  --stats        parse only; print file, byte, declaration counts and timings to stderr
  -o FILE        write to FILE instead of standard output
";

const OUTPUT_BUFFER_BYTES: usize = 1024 * 1024;

struct Opts {
    paths: Vec<PathBuf>,
    context: Vec<PathBuf>,
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
        context: vec![],
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
            "--context" => o
                .context
                .push(it.next().ok_or("--context needs a value")?.into()),
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
    if o.no_project && !o.context.is_empty() {
        return Err("--context cannot be combined with --no-project".into());
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

fn collect(paths: &[PathBuf], root: Option<&Path>) -> std::io::Result<Vec<Job>> {
    let mut jobs = Vec::new();
    for p in paths {
        if p.is_dir() {
            let root = root.unwrap_or(p);
            for e in walkdir::WalkDir::new(p).sort_by_file_name().into_iter() {
                let e = e.map_err(std::io::Error::other)?;
                let path = e.path();
                if e.file_type().is_file() && path.extension().is_some_and(|x| x == "lean") {
                    let rel = path.strip_prefix(root).unwrap_or(path);
                    jobs.push(Job {
                        path: path.to_path_buf(),
                        file: rel.to_string_lossy().into_owned(),
                        module: module_name(rel),
                    });
                }
            }
        } else {
            let rel = match root {
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
    Ok(jobs)
}

/// Mapped input must remain intact until every output batch has been serialized.
fn reject_output_alias(out: &Path, jobs: &[Job], context_jobs: &[Job]) -> std::io::Result<()> {
    let output = match same_file::Handle::from_path(out) {
        Ok(handle) => handle,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error),
    };
    for job in jobs.iter().chain(context_jobs) {
        if same_file::Handle::from_path(&job.path)? == output {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                format!("--mmap output aliases input: {}", job.path.display()),
            ));
        }
    }
    Ok(())
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
    let jobs = collect(&o.paths, o.root.as_deref()).unwrap_or_else(|e| {
        eprintln!("lean-source: {e}");
        std::process::exit(1);
    });
    let context_jobs = collect(&o.context, None).unwrap_or_else(|e| {
        eprintln!("lean-source: {e}");
        std::process::exit(1);
    });
    if o.mmap
        && !o.stats
        && let Some(out) = &o.out
    {
        reject_output_alias(out, &jobs, &context_jobs).unwrap_or_else(|e| {
            eprintln!("lean-source: {e}");
            std::process::exit(1);
        });
    }
    let t_walk = t0.elapsed();
    let opts = JsonOptions { text: o.text };
    let t1 = Instant::now();
    let context_first: Vec<Option<(usize, lean_source::Parsed)>> = context_jobs
        .par_iter()
        .map(|j| match read(&j.path, o.mmap) {
            Ok(data) => Some((data.len(), lean_source::parse(&data))),
            Err(e) => {
                eprintln!("lean-source: {}: {e}", j.path.display());
                None
            }
        })
        .collect();
    if context_first.iter().any(Option::is_none) {
        std::process::exit(1);
    }
    let context_bytes: usize = context_first.iter().flatten().map(|x| x.0).sum();
    // With several files, a first pass collects what the project declares and translates.
    // First pass: every file on its own.
    let mut first: Vec<Option<(Data, lean_source::Parsed)>> = jobs
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
    if first.iter().any(Option::is_none) {
        std::process::exit(1);
    }
    // With several files, the names that to_additive and to_dual generate depend on the rest of
    // the project; files that use them are read again with that context.
    let needs_project = jobs.len() > 1
        || !context_jobs.is_empty()
        || first
            .iter()
            .flatten()
            .any(|(_, parsed)| parsed.needs_structure_context || parsed.needs_simps_context);
    let ctx = (needs_project && !o.no_project).then(|| {
        lean_source::Context::build(
            context_first
                .iter()
                .flatten()
                .map(|x| &x.1)
                .chain(first.iter().flatten().map(|x| &x.1)),
            o.name_ranges,
        )
    });
    drop(context_first);
    let stdout = std::io::stdout();
    let mut writer: Option<Box<dyn Write>> = if o.stats {
        None
    } else {
        Some(match &o.out {
            Some(p) => Box::new(std::io::BufWriter::with_capacity(
                OUTPUT_BUFFER_BYTES,
                std::fs::File::create(p).expect("cannot create output"),
            )),
            None => Box::new(std::io::BufWriter::with_capacity(
                OUTPUT_BUFFER_BYTES,
                stdout.lock(),
            )),
        })
    };
    let mut bytes = 0;
    let mut decls = 0;
    // Preserve file order while limiting serialized output to one batch. Large projects no
    // longer need a second, corpus-sized allocation for JSON before the first write.
    for (batch_jobs, batch_first) in jobs.chunks(1024).zip(first.chunks_mut(1024)) {
        let results: Vec<(usize, usize, String)> = batch_jobs
            .par_iter()
            .zip(batch_first.par_iter_mut())
            .map(|(j, first)| {
                let (data, parsed) = first.take().expect("all source files were read");
                let parsed = match &ctx {
                    Some(ctx)
                        if parsed.needs_structure_context
                            || parsed.needs_simps_context
                            || needs_context(&data) =>
                    {
                        lean_source::parse_in(&data, ctx)
                    }
                    _ => parsed,
                };
                let mut out = String::new();
                if !o.stats {
                    if o.commands {
                        write_commands(&mut out, &j.file, &j.module, &data, &parsed);
                    } else {
                        write_decls(&mut out, &j.file, &j.module, &data, &parsed, opts);
                    }
                }
                (data.len(), parsed.decls.len(), out)
            })
            .collect();
        for (file_bytes, file_decls, output) in results {
            bytes += file_bytes;
            decls += file_decls;
            if let Some(writer) = &mut writer {
                writer.write_all(output.as_bytes()).expect("write failed");
            }
        }
    }
    if let Some(writer) = &mut writer {
        writer.flush().expect("write failed");
    }
    let t_parse = t1.elapsed();
    if o.stats {
        if !context_jobs.is_empty() {
            eprintln!(
                "context files {} bytes {}",
                context_jobs.len(),
                context_bytes
            );
        }
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
    }
}
