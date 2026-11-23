//! Benchmarks over a directory of Lean files.
//!
//! Set LEAN_SOURCE_BENCH_DIR to a source tree (for example Mathlib); without it the
//! benchmarks use a small built-in sample.

use criterion::{BenchmarkId, Criterion, Throughput, criterion_group, criterion_main};
use std::path::Path;

const SAMPLE: &str = r#"/-
Copyright (c) 2024. Released under Apache 2.0 license.
-/
module

public import Mathlib.Algebra.Group.Defs

/-! # Sample -/

namespace Sample

variable {α : Type*} [Monoid α]

/-- A doc comment. -/
@[simp, to_additive]
theorem mul_one' (a : α) : a * 1 = a := by
  simp

structure Point (α : Type*) where
  /-- First coordinate. -/
  x : α
  y : α
  deriving Repr

inductive Color
  | red
  | green (n : ℕ)

def f : ℕ → ℕ
  | 0 => 1
  | n + 1 => f n * 2
where
  helper (n : ℕ) : ℕ := n

instance : Inhabited (Point ℕ) := ⟨⟨0, 0⟩⟩

end Sample
"#;

fn corpus() -> Vec<Vec<u8>> {
    match std::env::var("LEAN_SOURCE_BENCH_DIR") {
        Ok(dir) => {
            let mut files = Vec::new();
            collect(Path::new(&dir), &mut files);
            files
        }
        Err(_) => vec![SAMPLE.repeat(200).into_bytes()],
    }
}

fn collect(dir: &Path, out: &mut Vec<Vec<u8>>) {
    let mut entries: Vec<_> = std::fs::read_dir(dir)
        .unwrap()
        .filter_map(Result::ok)
        .collect();
    entries.sort_by_key(|e| e.path());
    for e in entries {
        let p = e.path();
        if p.is_dir() {
            collect(&p, out);
        } else if p.extension().is_some_and(|x| x == "lean") {
            out.push(std::fs::read(&p).unwrap());
        }
    }
}

fn bench(c: &mut Criterion) {
    let files = corpus();
    let bytes: usize = files.iter().map(Vec::len).sum();
    let mut g = c.benchmark_group("single-thread");
    g.throughput(Throughput::Bytes(bytes as u64));
    g.sample_size(10);
    g.bench_function(BenchmarkId::new("lex", bytes), |b| {
        let mut toks = Vec::new();
        b.iter(|| {
            for f in &files {
                toks.clear();
                lean_source::lexer::lex(f, &mut toks);
            }
        })
    });
    g.bench_function(BenchmarkId::new("parse", bytes), |b| {
        b.iter(|| {
            for f in &files {
                std::hint::black_box(lean_source::parse(f));
            }
        })
    });
    g.finish();
}

criterion_group!(benches, bench);
criterion_main!(benches);
