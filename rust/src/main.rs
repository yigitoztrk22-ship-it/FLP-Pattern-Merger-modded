//! Command-line front end.

use std::path::PathBuf;
use std::process::ExitCode;

use flp_note_merger::error::MergeError;
use flp_note_merger::{merge_flp, MergeOptions, MergeStats, APP_NAME, APP_VERSION};

const USAGE: &str = "\
Merge all arranged FL Studio Pattern Clip notes into one optimized, notes-only
FLP copy. The source project is never modified.

USAGE:
    flp-note-merger <input.flp> <output.flp> [OPTIONS]

OPTIONS:
    --skip-muted        Skip muted Pattern Clips so the audible arrangement is preserved.
    --sorted            Disable Turbo mode and globally sort note records.
    --run-records <N>   Accepted for command-line compatibility; unused (no external sort).
    --threads <N>       Worker threads (default: all cores, 1 disables parallelism).
    --no-fsync          Skip the flush-to-disk before the atomic rename.
    --repeat <N>        Run the merge N times and report the best/median timing.
    -q, --quiet         Only print the final summary line.
    -v, --verbose       Print a per-stage timing breakdown.
        --json          Print the summary as one JSON object.
    -h, --help          Show this help.
    -V, --version       Show the version.
";

struct Args {
    input: PathBuf,
    output: PathBuf,
    options: MergeOptions,
    threads: usize,
    repeat: usize,
    quiet: bool,
    verbose: bool,
    json: bool,
}

enum Parsed {
    Run(Box<Args>),
    Exit(ExitCode),
}

fn parse_args() -> Result<Parsed, String> {
    let mut positional: Vec<PathBuf> = Vec::new();
    let mut options = MergeOptions::default();
    let mut threads = 0usize;
    let mut repeat = 1usize;
    let (mut quiet, mut verbose, mut json) = (false, false, false);

    let mut argv = std::env::args().skip(1);
    while let Some(arg) = argv.next() {
        let mut value = |name: &str| -> Result<String, String> {
            argv.next()
                .ok_or_else(|| format!("{name} requires a value"))
        };
        match arg.as_str() {
            "-h" | "--help" => {
                print!("{USAGE}");
                return Ok(Parsed::Exit(ExitCode::SUCCESS));
            }
            "-V" | "--version" => {
                println!("flp-note-merger {APP_VERSION}");
                return Ok(Parsed::Exit(ExitCode::SUCCESS));
            }
            "--skip-muted" => options.include_muted_clips = false,
            "--sorted" => options.turbo_mode = false,
            "--no-fsync" => options.fsync = false,
            "-q" | "--quiet" => quiet = true,
            "-v" | "--verbose" => verbose = true,
            "--json" => json = true,
            "--run-records" => {
                let _ = value("--run-records")?;
            }
            "--threads" => {
                threads = value("--threads")?
                    .parse()
                    .map_err(|_| "--threads expects a whole number".to_string())?;
            }
            "--repeat" => {
                repeat = value("--repeat")?
                    .parse()
                    .map_err(|_| "--repeat expects a whole number".to_string())?;
                repeat = repeat.max(1);
            }
            other if other.starts_with('-') && other.len() > 1 => {
                return Err(format!("unrecognized option: {other}"));
            }
            other => positional.push(PathBuf::from(other)),
        }
    }

    if positional.len() != 2 {
        return Err("both input and output are required in command-line mode".to_string());
    }
    if threads == 1 {
        options.parallel = false;
    }

    Ok(Parsed::Run(Box::new(Args {
        input: positional[0].clone(),
        output: positional[1].clone(),
        options,
        threads,
        repeat,
        quiet,
        verbose,
        json,
    })))
}

fn format_size(size: u64) -> String {
    const GIB: f64 = (1024 * 1024 * 1024) as f64;
    const MIB: f64 = (1024 * 1024) as f64;
    let value = size as f64;
    if value >= GIB {
        format!("{:.2} GiB", value / GIB)
    } else if value >= MIB {
        format!("{:.1} MiB", value / MIB)
    } else if value >= 1024.0 {
        format!("{:.1} KiB", value / 1024.0)
    } else {
        format!("{size} B")
    }
}

fn thousands(value: u64) -> String {
    let digits = value.to_string();
    let mut out = String::with_capacity(digits.len() + digits.len() / 3);
    for (index, ch) in digits.chars().enumerate() {
        if index > 0 && (digits.len() - index) % 3 == 0 {
            out.push(',');
        }
        out.push(ch);
    }
    out
}

fn print_timings(stats: &MergeStats) {
    let t = &stats.timings;
    let ms = |micros: u128| format!("{:.3} ms", micros as f64 / 1000.0);
    eprintln!("  map     {}", ms(t.map_us));
    eprintln!("  scan    {}", ms(t.scan_us));
    eprintln!("  clips   {}", ms(t.clips_us));
    eprintln!("  expand  {}", ms(t.expand_us));
    if t.sort_us > 0 {
        eprintln!("  sort    {}", ms(t.sort_us));
    }
    eprintln!("  rewrite {}", ms(t.rewrite_us));
    eprintln!("  write   {}", ms(t.write_us));
    eprintln!("  ------------------");
    eprintln!(
        "  merge   {}  (everything before the output write)",
        ms(t.merge_us())
    );
    eprintln!("  total   {}", ms(t.total_us));
}

fn print_json(stats: &MergeStats) {
    let t = &stats.timings;
    println!(
        concat!(
            "{{\"merged_notes\":{},\"source_notes\":{},\"source_pattern_clips\":{},",
            "\"included_pattern_clips\":{},\"arrangements\":{},\"patterns_with_notes\":{},",
            "\"ppq\":{},\"target_pattern_id\":{},\"source_size\":{},\"output_size\":{},",
            "\"timings_us\":{{\"map\":{},\"scan\":{},\"clips\":{},\"expand\":{},\"sort\":{},",
            "\"rewrite\":{},\"write\":{},\"merge\":{},\"total\":{}}}}}"
        ),
        stats.merged_notes,
        stats.source_notes,
        stats.source_pattern_clips,
        stats.included_pattern_clips,
        stats.arrangements,
        stats.patterns_with_notes,
        stats.ppq,
        stats.target_pattern_id,
        stats.source_size,
        stats.output_size,
        t.map_us,
        t.scan_us,
        t.clips_us,
        t.expand_us,
        t.sort_us,
        t.rewrite_us,
        t.write_us,
        t.merge_us(),
        t.total_us,
    );
}

fn main() -> ExitCode {
    let args = match parse_args() {
        Ok(Parsed::Exit(code)) => return code,
        Ok(Parsed::Run(args)) => *args,
        Err(message) => {
            eprintln!("{APP_NAME}: {message}\n");
            eprint!("{USAGE}");
            return ExitCode::from(2);
        }
    };

    if args.threads > 1 {
        let _ = rayon::ThreadPoolBuilder::new()
            .num_threads(args.threads)
            .build_global();
    }

    let chatty = !args.quiet && !args.json;
    let status = |message: &str| println!("{message}");

    let mut best: Option<MergeStats> = None;
    let mut merge_times: Vec<u128> = Vec::with_capacity(args.repeat);
    for round in 0..args.repeat {
        let listener = if chatty && round == 0 {
            Some(&status as flp_note_merger::StatusCallback<'_>)
        } else {
            None
        };
        let stats = match merge_flp(&args.input, &args.output, &args.options, listener) {
            Ok(stats) => stats,
            Err(MergeError::Cancelled) => {
                eprintln!("Cancelled.");
                return ExitCode::from(130);
            }
            Err(err) => {
                eprintln!("ERROR: {err}");
                return ExitCode::FAILURE;
            }
        };
        merge_times.push(stats.timings.merge_us());
        let replace = best
            .as_ref()
            .is_none_or(|current| stats.timings.total_us < current.timings.total_us);
        if replace {
            best = Some(stats);
        }
    }

    let stats = best.expect("at least one merge round runs");
    if args.json {
        print_json(&stats);
        return ExitCode::SUCCESS;
    }

    if args.verbose {
        eprintln!("Stage timings (best of {} run(s)):", args.repeat);
        print_timings(&stats);
        if args.repeat > 1 {
            merge_times.sort_unstable();
            let median = merge_times[merge_times.len() / 2];
            eprintln!(
                "  median merge over {} runs: {:.3} ms",
                args.repeat,
                median as f64 / 1000.0
            );
        }
    }

    println!(
        "Success: {} notes, {}, merge {:.3} ms, total {:.3} ms",
        thousands(stats.merged_notes),
        format_size(stats.output_size),
        stats.timings.merge_us() as f64 / 1000.0,
        stats.timings.total_us as f64 / 1000.0,
    );
    ExitCode::SUCCESS
}
