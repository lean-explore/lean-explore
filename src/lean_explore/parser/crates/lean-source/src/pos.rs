//! Byte offsets to Lean positions (1-based lines, 0-based columns in code points).

use memchr::memchr_iter;

/// Line start offsets of one file.
pub struct LineIndex {
    starts: Vec<u32>,
}

/// A position: byte offset, line (1-based) and column in code points (0-based).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Pos {
    pub byte: u32,
    pub line: u32,
    pub col: u32,
}

impl LineIndex {
    pub fn new(src: &[u8]) -> Self {
        let mut starts = Vec::with_capacity(src.len() / 40 + 1);
        starts.push(0);
        starts.extend(memchr_iter(b'\n', src).map(|p| p as u32 + 1));
        LineIndex { starts }
    }

    /// Number of lines.
    pub fn lines(&self) -> usize {
        self.starts.len()
    }

    /// Byte offset where line `line` (1-based) starts.
    pub fn line_start(&self, line: u32) -> u32 {
        self.starts[(line - 1) as usize]
    }

    pub fn pos(&self, src: &[u8], byte: u32) -> Pos {
        let line = self.starts.partition_point(|&s| s <= byte);
        let start = self.starts[line - 1] as usize;
        let col = src[start..byte as usize]
            .iter()
            .filter(|&&b| (b & 0xc0) != 0x80)
            .count() as u32;
        Pos {
            byte,
            line: line as u32,
            col,
        }
    }
}
