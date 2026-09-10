//! FLP Note Merger — flatten every arranged Pattern Clip into one pattern.
//!
//! Rust port of the reference Python/NumPy implementation. The pipeline is:
//!
//! 1. [`scan`] — one pass over the FLdt event headers of the memory-mapped file.
//! 2. [`clips`] — index the Pattern Clips and lay out arrangement sections.
//! 3. [`expand`] — flatten clips into absolute note records (shape-cached, parallel).
//! 4. [`rewrite`] — plan the notes-only copy as borrowed spans, then stream it.
//!
//! The source project is opened read-only and never modified.

pub mod clips;
pub mod error;
pub mod expand;
pub mod flp;
pub mod rewrite;
pub mod scan;
pub mod sort;

use std::fs::File;
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};
use std::time::Instant;

use error::{MergeError, Result};

pub const APP_NAME: &str = "FLP Note Merger";
pub const APP_VERSION: &str = "2.0.0";

#[derive(Clone, Copy, Debug)]
pub struct MergeOptions {
    pub include_muted_clips: bool,
    /// Turbo writes transformed records directly. FL Studio accepts note
    /// records in insertion order, so a global sort is optional.
    pub turbo_mode: bool,
    pub parallel: bool,
    /// Flush the output to stable storage before the atomic rename.
    pub fsync: bool,
}

impl Default for MergeOptions {
    fn default() -> Self {
        MergeOptions {
            include_muted_clips: true,
            turbo_mode: true,
            parallel: true,
            fsync: true,
        }
    }
}

#[derive(Clone, Copy, Debug, Default)]
pub struct StageTimings {
    pub map_us: u128,
    pub scan_us: u128,
    pub clips_us: u128,
    pub expand_us: u128,
    pub sort_us: u128,
    pub rewrite_us: u128,
    pub write_us: u128,
    pub total_us: u128,
}

impl StageTimings {
    /// Everything except the final `write()`/`fsync()` syscalls.
    pub fn merge_us(&self) -> u128 {
        self.map_us + self.scan_us + self.clips_us + self.expand_us + self.sort_us + self.rewrite_us
    }
}

#[derive(Clone, Debug, Default)]
pub struct MergeStats {
    pub source_notes: u64,
    pub source_pattern_clips: u64,
    pub included_pattern_clips: u64,
    pub merged_notes: u64,
    pub arrangements: usize,
    pub patterns_with_notes: usize,
    pub ppq: u16,
    pub source_size: u64,
    pub output_size: u64,
    pub target_pattern_id: u16,
    pub version_text: String,
    pub timings: StageTimings,
}

pub type StatusCallback<'a> = &'a dyn Fn(&str);

/// Everything the rewrite needs, once the source has been analysed.
struct Prepared {
    scan: scan::ScanResult,
    index: clips::ClipIndex,
    merged: expand::MergedNotes,
}

/// Scan, index clips and expand notes. Shared by the file and in-memory paths.
fn prepare(
    data: &[u8],
    options: &MergeOptions,
    timings: &mut StageTimings,
    status: Option<StatusCallback<'_>>,
) -> Result<Prepared> {
    let report = |message: &str| {
        if let Some(status) = status {
            status(message);
        }
    };

    report("Scanning FLP structure…");
    let step = Instant::now();
    let mut scan = scan::scan_flp(data)?;
    timings.scan_us = step.elapsed().as_micros();
    report(&format!(
        "Found {} stored notes in {} patterns at {} PPQ.",
        scan.source_note_count, scan.patterns_with_notes, scan.ppq
    ));

    report("Indexing Pattern Clips (audio and automation are ignored)…");
    let step = Instant::now();
    let index = clips::extract_pattern_clips(data, &mut scan, options.include_muted_clips)?;
    timings.clips_us = step.elapsed().as_micros();

    report("Expanding Pattern Clips into absolute note positions…");
    let step = Instant::now();
    let mut merged = expand::expand_clips(data, &scan, &index.clips, options.parallel)?;
    timings.expand_us = step.elapsed().as_micros();

    if merged.count == 0 {
        return Err(MergeError::format(
            "Pattern Clips were found, but no notes fall inside their visible ranges.",
        ));
    }

    if !options.turbo_mode {
        report("Compatibility mode: globally sorting all merged note records.");
        let step = Instant::now();
        sort::sort_by_position(&mut merged);
        timings.sort_us = step.elapsed().as_micros();
    }

    Ok(Prepared {
        scan,
        index,
        merged,
    })
}

/// Merge a project already held in memory and return the new project bytes.
///
/// Useful for embedding, and for tests that never touch the filesystem.
pub fn merge_in_memory(data: &[u8], options: &MergeOptions) -> Result<Vec<u8>> {
    let mut timings = StageTimings::default();
    let prepared = prepare(data, options, &mut timings, None)?;
    let plan = rewrite::rewrite_project(data, &prepared.scan, &prepared.merged)?;
    let mut out = Vec::with_capacity(plan.total_size);
    plan.write_to(&mut out)?;
    Ok(out)
}

/// Merge `source_path` into a new notes-only project at `output_path`.
pub fn merge_flp(
    source_path: &Path,
    output_path: &Path,
    options: &MergeOptions,
    status: Option<StatusCallback<'_>>,
) -> Result<MergeStats> {
    let report = |message: &str| {
        if let Some(status) = status {
            status(message);
        }
    };

    let source_path = absolutize(source_path);
    let output_path = absolutize(output_path);
    if source_path == output_path {
        return Err(MergeError::usage(
            "Choose a different output path; the source is never overwritten.",
        ));
    }
    if !source_path
        .extension()
        .is_some_and(|ext| ext.eq_ignore_ascii_case("flp"))
    {
        return Err(MergeError::usage("The input must be an .flp file."));
    }
    if !source_path.is_file() {
        return Err(MergeError::usage(format!(
            "Input project not found: {}",
            source_path.display()
        )));
    }
    if let Some(parent) = output_path.parent() {
        std::fs::create_dir_all(parent)?;
    }

    let started = Instant::now();
    let mut timings = StageTimings::default();

    // The source is mapped read-only; nothing here ever writes to it.
    let step = Instant::now();
    let file = File::open(&source_path)?;
    let source_size = file.metadata()?.len();
    let mapping = unsafe { memmap2::Mmap::map(&file)? };
    let data: &[u8] = &mapping;
    timings.map_us = step.elapsed().as_micros();

    let Prepared {
        scan,
        index,
        merged,
    } = prepare(data, options, &mut timings, status)?;

    report("Writing optimized FLP copy…");
    let step = Instant::now();
    let plan = rewrite::rewrite_project(data, &scan, &merged)?;
    timings.rewrite_us = step.elapsed().as_micros();

    let step = Instant::now();
    let output_size = plan.total_size as u64;
    write_atomically(&output_path, &plan, options.fsync)?;
    timings.write_us = step.elapsed().as_micros();
    timings.total_us = started.elapsed().as_micros();

    report(&format!(
        "Done: {} notes in Pattern {}; output {:.1} MiB.",
        merged.count,
        scan.target_pattern_id,
        output_size as f64 / (1024.0 * 1024.0)
    ));

    Ok(MergeStats {
        source_notes: scan.source_note_count,
        source_pattern_clips: index.total_pattern_clips,
        included_pattern_clips: index.included_pattern_clips,
        merged_notes: merged.count as u64,
        arrangements: scan.arrangements.len(),
        patterns_with_notes: scan.patterns_with_notes,
        ppq: scan.ppq,
        source_size,
        output_size,
        target_pattern_id: scan.target_pattern_id,
        version_text: scan.version_text.clone(),
        timings,
    })
}

/// Export arranged notes directly as a format-0 Standard MIDI file.
///
/// This shares the memory-mapped scan and parallel expansion pipeline with the
/// FLP merger, avoiding the Python per-note decoding and sorting overhead.
pub fn export_midi(
    source_path: &Path,
    output_path: &Path,
    options: &MergeOptions,
    status: Option<StatusCallback<'_>>,
) -> Result<u64> {
    let report = |message: &str| {
        if let Some(status) = status {
            status(message);
        }
    };
    let source_path = absolutize(source_path);
    let output_path = absolutize(output_path);
    if source_path == output_path {
        return Err(MergeError::usage("Choose a different output path; the source is never overwritten."));
    }
    if !source_path.is_file() {
        return Err(MergeError::usage(format!("Input project not found: {}", source_path.display())));
    }
    if let Some(parent) = output_path.parent() {
        std::fs::create_dir_all(parent)?;
    }

    let file = File::open(&source_path)?;
    let mapping = unsafe { memmap2::Mmap::map(&file)? };
    let mut timings = StageTimings::default();
    let prepared = prepare(&mapping, options, &mut timings, Some(&report))?;
    report("Sorting MIDI events…");

    let mut events: Vec<(u64, u8, u8, u8)> = Vec::with_capacity(prepared.merged.count * 2);
    for record in prepared.merged.bytes.chunks_exact(NOTE_SIZE) {
        let position = read_u32(record, NOTE_POSITION) as u64;
        let length = read_u32(record, NOTE_LENGTH).max(1) as u64;
        let pitch = read_u16(record, NOTE_KEY).min(127) as u8;
        let velocity = record[NOTE_VELOCITY].clamp(1, 127);
        events.push((position, 1, pitch, velocity));
        events.push((position + length, 0, pitch, 0));
    }
    events.sort_unstable_by_key(|event| (event.0, event.1));

    let partial = output_path.with_extension("mid.partial");
    let result = (|| -> Result<()> {
        let file = File::create(&partial)?;
        let mut output = BufWriter::with_capacity(1 << 20, file);
        output.write_all(b"MThd")?;
        output.write_all(&6u32.to_be_bytes())?;
        output.write_all(&0u16.to_be_bytes())?;
        output.write_all(&1u16.to_be_bytes())?;
        output.write_all(&prepared.scan.ppq.to_be_bytes())?;

        let mut track = Vec::with_capacity(events.len() * 4 + 11);
        track.extend_from_slice(b"\0\xFF\x51\x03\x07\xA1\x20");
        let mut previous = 0u64;
        for (tick, kind, pitch, velocity) in events {
            push_midi_varlen(&mut track, tick - previous);
            track.extend_from_slice(&[if kind == 1 { 0x90 } else { 0x80 }, pitch, velocity]);
            previous = tick;
        }
        track.extend_from_slice(b"\0\xFF\x2F\0");
        output.write_all(b"MTrk")?;
        output.write_all(&(track.len() as u32).to_be_bytes())?;
        output.write_all(&track)?;
        output.flush()?;
        drop(output);
        std::fs::rename(&partial, &output_path)?;
        Ok(())
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(&partial);
    }
    result?;
    report(&format!("Done: {} notes exported to {}.", prepared.merged.count, output_path.display()));
    Ok(prepared.merged.count as u64)
}

fn push_midi_varlen(output: &mut Vec<u8>, mut value: u64) {
    let mut buffer = [0u8; 10];
    let mut index = buffer.len() - 1;
    buffer[index] = (value & 0x7F) as u8;
    while {
        value >>= 7;
        value != 0
    } {
        index -= 1;
        buffer[index] = ((value & 0x7F) as u8) | 0x80;
    }
    output.extend_from_slice(&buffer[index..]);
}

/// Write to `name.flp.partial`, then rename only after a complete write.
///
/// The plan is streamed through a large `BufWriter`: small generated events are
/// coalesced, while multi-megabyte spans of the source and the merged payload
/// are passed straight to `write`, so nothing is copied twice.
fn write_atomically(output_path: &Path, plan: &rewrite::OutputPlan<'_>, fsync: bool) -> Result<()> {
    const WRITE_BUFFER: usize = 1 << 20;

    let mut partial = output_path.as_os_str().to_owned();
    partial.push(".partial");
    let partial = PathBuf::from(partial);

    let result = (|| -> Result<()> {
        let file = File::create(&partial)?;
        // Pre-size the file so the filesystem can allocate one extent.
        file.set_len(plan.total_size as u64)?;
        let mut writer = std::io::BufWriter::with_capacity(WRITE_BUFFER, file);
        plan.write_to(&mut writer)?;
        let file = writer
            .into_inner()
            .map_err(|err| MergeError::Io(err.into_error()))?;
        if fsync {
            file.sync_all()?;
        }
        drop(file);
        std::fs::rename(&partial, output_path)?;
        Ok(())
    })();

    if result.is_err() {
        let _ = std::fs::remove_file(&partial);
    }
    result
}

/// Absolute path with the parent directory resolved, so `a/../b.flp` and
/// `b.flp` compare equal when checking that input and output differ.
fn absolutize(path: &Path) -> PathBuf {
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()
            .map(|dir| dir.join(path))
            .unwrap_or_else(|_| path.to_path_buf())
    };
    match (absolute.parent(), absolute.file_name()) {
        (Some(parent), Some(name)) => match parent.canonicalize() {
            Ok(resolved) => resolved.join(name),
            Err(_) => absolute,
        },
        _ => absolute,
    }
}
