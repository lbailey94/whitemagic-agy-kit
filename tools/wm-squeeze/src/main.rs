//! wm-squeeze: Zero-dependency deterministic CLI output compressor & safe proxy for AGY CLI.
//!
//! Modes:
//!   wm-squeeze --safe [--cwd DIR] [--max-bytes N] [--] <cmd> [args...]
//!       Executes ONLY strictly allowlisted read-only / local build-test commands directly via
//!       execvp (zero shell interpolation), preserving exit code while stripping ANSI/progress
//!       spam, collapsing passing noise, deduplicating repeated lines, and capping inline output
//!       below Antigravity's 10 KB disk-spill threshold.
//!   wm-squeeze --pipe [--kind auto|cargo|pytest|git|rg|ls|generic] [--max-bytes N]
//!       Compresses stdin to stdout.
//!   wm-squeeze --stats
//!       Shows cumulative bytes & estimated tokens saved.

use std::collections::HashMap;
use std::env;
use std::fs::{self, OpenOptions};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};

const DEFAULT_MAX_BYTES: usize = 8_200;
const MAX_LINE_CHARS: usize = 320;
const SPILL_DIR: &str = "/tmp/wm-squeeze";

#[derive(Clone, Copy, PartialEq, Eq)]
enum Kind {
    Cargo,
    Pytest,
    Git,
    Rg,
    Ls,
    Generic,
}

fn stats_path() -> Option<PathBuf> {
    if env::var("WM_SQUEEZE_NO_STATS").ok().as_deref() == Some("1") {
        return None;
    }
    let home = env::var("HOME").ok()?;
    Some(
        PathBuf::from(home)
            .join(".local/share/whitemagic/wm_squeeze_stats.jsonl"),
    )
}

fn log_stats(cmd_label: &str, raw_bytes: usize, out_bytes: usize, spilled: bool) {
    if raw_bytes == 0 {
        return;
    }
    if let Some(p) = stats_path() {
        if let Some(parent) = p.parent() {
            let _ = fs::create_dir_all(parent);
        }
        let ts = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        let safe_label: String = cmd_label
            .chars()
            .filter(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | ':' | '/' | ' '))
            .take(40)
            .collect();
        let line = format!(
            "{{\"ts\":{},\"cmd\":\"{}\",\"raw\":{},\"out\":{},\"saved\":{},\"spilled\":{}}}\n",
            ts,
            safe_label,
            raw_bytes,
            out_bytes,
            raw_bytes.saturating_sub(out_bytes),
            spilled
        );
        if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(p) {
            let _ = f.write_all(line.as_bytes());
        }
    }
}

fn print_stats() {
    let Some(p) = stats_path() else {
        println!("wm-squeeze stats disabled (WM_SQUEEZE_NO_STATS=1).");
        return;
    };
    let Ok(content) = fs::read_to_string(&p) else {
        println!("No wm-squeeze telemetry recorded yet ({}).", p.display());
        return;
    };
    let (mut count, mut raw_sum, mut out_sum, mut spills) = (0usize, 0usize, 0usize, 0usize);
    for line in content.lines() {
        if line.trim().is_empty() {
            continue;
        }
        let get_num = |key: &str| -> usize {
            line.find(key)
                .and_then(|i| {
                    let rest = &line[i + key.len()..];
                    let num: String = rest.chars().take_while(|c| c.is_ascii_digit()).collect();
                    num.parse().ok()
                })
                .unwrap_or(0)
        };
        count += 1;
        raw_sum += get_num("\"raw\":");
        out_sum += get_num("\"out\":");
        if line.contains("\"spilled\":true") {
            spills += 1;
        }
    }
    let saved = raw_sum.saturating_sub(out_sum);
    let pct = if raw_sum > 0 {
        (saved as f64) * 100.0 / (raw_sum as f64)
    } else {
        0.0
    };
    println!(
        "wm-squeeze: {} cmds | raw {:.1} KB -> out {:.1} KB | saved {:.1} KB ({:.1}%, ~{} tok) | overflow files: {}",
        count,
        raw_sum as f64 / 1024.0,
        out_sum as f64 / 1024.0,
        saved as f64 / 1024.0,
        pct,
        saved / 4,
        spills
    );
}

/// Strip ANSI CSI / OSC / single-byte escape sequences and collapse carriage-return progress frames.
fn normalize_terminal(raw: &str) -> String {
    let bytes = raw.as_bytes();
    let mut cleaned = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == 0x1b {
            i += 1;
            if i >= bytes.len() {
                break;
            }
            match bytes[i] {
                b'[' => {
                    // CSI: ESC [ ... final byte 0x40..=0x7E
                    i += 1;
                    while i < bytes.len() && !(0x40..=0x7e).contains(&bytes[i]) {
                        i += 1;
                    }
                    if i < bytes.len() {
                        i += 1;
                    }
                }
                b']' => {
                    // OSC: ESC ] ... BEL or ST (ESC \)
                    i += 1;
                    while i < bytes.len() {
                        if bytes[i] == 0x07 {
                            i += 1;
                            break;
                        }
                        if bytes[i] == 0x1b && i + 1 < bytes.len() && bytes[i + 1] == b'\\' {
                            i += 2;
                            break;
                        }
                        i += 1;
                    }
                }
                _ => {
                    i += 1;
                }
            }
        } else {
            cleaned.push(bytes[i]);
            i += 1;
        }
    }
    let text = String::from_utf8_lossy(&cleaned).replace("\r\n", "\n");
    let mut out = String::with_capacity(text.len());
    for (idx, line) in text.split('\n').enumerate() {
        if idx > 0 {
            out.push('\n');
        }
        if line.contains('\r') {
            if let Some(last) = line.rsplit('\r').find(|s| !s.is_empty()) {
                out.push_str(last.trim_end());
            }
        } else {
            out.push_str(line.trim_end());
        }
    }
    out.trim_end().to_string()
}

fn clamp_line(line: &str, max_chars: usize) -> String {
    if line.chars().count() <= max_chars {
        return line.to_string();
    }
    let mut s: String = line.chars().take(max_chars.saturating_sub(18)).collect();
    let total = line.chars().count();
    s.push_str(&format!(" ...[+{}ch]", total.saturating_sub(max_chars.saturating_sub(18))));
    s
}

fn dedup_consecutive(lines: Vec<String>) -> Vec<String> {
    let mut out: Vec<String> = Vec::with_capacity(lines.len());
    let mut prev: Option<String> = None;
    let mut run = 0usize;
    let mut blank_run = 0usize;

    let flush = |prev: &mut Option<String>, run: &mut usize, out: &mut Vec<String>| {
        if let Some(p) = prev.take() {
            if *run > 2 {
                out.push(format!("{}  [x{}]", p, *run));
            } else {
                for _ in 0..*run {
                    out.push(p.clone());
                }
            }
            *run = 0;
        }
    };

    for line in lines {
        if line.trim().is_empty() {
            flush(&mut prev, &mut run, &mut out);
            blank_run += 1;
            if blank_run <= 1 {
                out.push(String::new());
            }
            continue;
        }
        blank_run = 0;
        if prev.as_deref() == Some(line.as_str()) {
            run += 1;
        } else {
            flush(&mut prev, &mut run, &mut out);
            prev = Some(line);
            run = 1;
        }
    }
    flush(&mut prev, &mut run, &mut out);
    out
}

fn compress_cargo(text: &str) -> String {
    let mut kept = Vec::new();
    let mut compiled = 0usize;
    let mut passed = 0usize;
    let mut downloaded = 0usize;

    for line in text.lines() {
        let t = line.trim();
        if t.starts_with("Compiling ") || t.starts_with("Checking ") || t.starts_with("Fresh ") {
            compiled += 1;
            continue;
        }
        if t.starts_with("Downloading ") || t.starts_with("Downloaded ") || t.starts_with("Updating ") {
            downloaded += 1;
            continue;
        }
        if t.starts_with("test ") && t.ends_with("... ok") {
            passed += 1;
            continue;
        }
        kept.push(line.to_string());
    }
    let mut header = Vec::new();
    if compiled > 0 || downloaded > 0 || passed > 0 {
        header.push(format!(
            "[cargo-summary: compiled/checked={}, downloaded={}, passed_tests={}]",
            compiled, downloaded, passed
        ));
    }
    header.extend(dedup_consecutive(kept));
    header.join("\n")
}

fn compress_pytest(text: &str) -> String {
    let mut kept = Vec::new();
    let mut passed = 0usize;
    for line in text.lines() {
        let t = line.trim();
        if t.ends_with(" PASSED") || t.contains(" PASSED [") {
            passed += 1;
            continue;
        }
        kept.push(line.to_string());
    }
    let mut out = Vec::new();
    if passed > 0 {
        out.push(format!("[pytest-summary: {} passed lines collapsed]", passed));
    }
    out.extend(dedup_consecutive(kept));
    out.join("\n")
}

fn compress_git(text: &str, max_bytes: usize) -> String {
    if text.len() <= max_bytes {
        return dedup_consecutive(text.lines().map(|l| clamp_line(l, MAX_LINE_CHARS)).collect())
            .join("\n");
    }
    // Check if unified diff: summarize lockfiles/huge files while preserving real code hunks
    if text.contains("diff --git ") {
        let mut out = Vec::new();
        let mut skip_noisy = false;
        let mut skipped_lines = 0usize;
        let mut cur_file = String::new();
        for line in text.lines() {
            if line.starts_with("diff --git ") {
                if skip_noisy && skipped_lines > 0 {
                    out.push(format!("  [skipped {} noisy diff lines in {}]", skipped_lines, cur_file));
                }
                cur_file = line.split_whitespace().last().unwrap_or("").to_string();
                let low = cur_file.to_lowercase();
                skip_noisy = low.ends_with("package-lock.json")
                    || low.ends_with("cargo.lock")
                    || low.ends_with("yarn.lock")
                    || low.ends_with("pnpm-lock.yaml")
                    || low.ends_with(".min.js")
                    || low.ends_with(".min.css")
                    || low.ends_with(".svg");
                skipped_lines = 0;
                out.push(line.to_string());
                continue;
            }
            if skip_noisy {
                skipped_lines += 1;
                continue;
            }
            out.push(clamp_line(line, MAX_LINE_CHARS));
        }
        if skip_noisy && skipped_lines > 0 {
            out.push(format!("  [skipped {} noisy diff lines in {}]", skipped_lines, cur_file));
        }
        return dedup_consecutive(out).join("\n");
    }
    dedup_consecutive(text.lines().map(|l| clamp_line(l, MAX_LINE_CHARS)).collect()).join("\n")
}

fn compress_rg(text: &str, max_bytes: usize) -> String {
    let raw_lines: Vec<&str> = text.lines().collect();
    if text.len() <= max_bytes {
        return raw_lines
            .into_iter()
            .map(|l| clamp_line(l, MAX_LINE_CHARS))
            .collect::<Vec<_>>()
            .join("\n");
    }
    // Cap per-file matches when output exceeds budget
    let mut per_file: HashMap<String, usize> = HashMap::new();
    let mut hidden_per_file: HashMap<String, usize> = HashMap::new();
    let mut out = Vec::new();
    for line in raw_lines {
        if let Some(colon) = line.find(':') {
            let f = &line[..colon];
            if !f.is_empty() && !f.contains(' ') {
                let cnt = per_file.entry(f.to_string()).or_insert(0);
                *cnt += 1;
                if *cnt > 12 {
                    *hidden_per_file.entry(f.to_string()).or_insert(0) += 1;
                    continue;
                }
            }
        }
        out.push(clamp_line(line, MAX_LINE_CHARS));
    }
    if !hidden_per_file.is_empty() {
        let mut pairs: Vec<(String, usize)> = hidden_per_file.into_iter().collect();
        pairs.sort_by(|a, b| b.1.cmp(&a.1));
        let summary: Vec<String> = pairs
            .into_iter()
            .take(8)
            .map(|(f, n)| format!("{}(+{})", f, n))
            .collect();
        out.push(format!("[rg-collapsed excess per-file hits: {}]", summary.join(", ")));
    }
    out.join("\n")
}

fn slice_utf8_prefix(s: &str, max_bytes: usize) -> &str {
    if s.len() <= max_bytes {
        return s;
    }
    let mut end = max_bytes;
    while end > 0 && !s.is_char_boundary(end) {
        end -= 1;
    }
    if let Some(nl) = s[..end].rfind('\n') {
        if nl > end / 2 {
            return &s[..nl];
        }
    }
    &s[..end]
}

fn slice_utf8_suffix(s: &str, max_bytes: usize) -> &str {
    if s.len() <= max_bytes {
        return s;
    }
    let mut start = s.len().saturating_sub(max_bytes);
    while start < s.len() && !s.is_char_boundary(start) {
        start += 1;
    }
    if let Some(nl) = s[start..].find('\n') {
        if nl < max_bytes / 2 {
            return &s[start + nl + 1..];
        }
    }
    &s[start..]
}

fn spill_raw(raw: &str) -> Option<String> {
    fs::create_dir_all(SPILL_DIR).ok()?;
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    let path = format!("{}/{}_{}.log", SPILL_DIR, ts, std::process::id());
    fs::write(&path, raw.as_bytes()).ok()?;
    Some(path)
}

fn squeeze(raw: &str, kind: Kind, max_bytes: usize) -> (String, bool) {
    let norm = normalize_terminal(raw);
    let body = match kind {
        Kind::Cargo => compress_cargo(&norm),
        Kind::Pytest => compress_pytest(&norm),
        Kind::Git => compress_git(&norm, max_bytes),
        Kind::Rg => compress_rg(&norm, max_bytes),
        Kind::Ls | Kind::Generic => dedup_consecutive(
            norm.lines().map(|l| clamp_line(l, MAX_LINE_CHARS)).collect(),
        )
        .join("\n"),
    };

    if body.len() <= max_bytes {
        if raw.len() > body.len() + 2048 {
            let pct = (raw.len() - body.len()) as f64 * 100.0 / raw.len() as f64;
            let note = format!(
                "\n[wm-squeeze: {:.1}KB -> {:.1}KB (-{:.0}%)]",
                raw.len() as f64 / 1024.0,
                body.len() as f64 / 1024.0,
                pct
            );
            if body.len() + note.len() <= max_bytes {
                return (format!("{}{}", body, note), false);
            }
        }
        return (body, false);
    }

    let spill_path = spill_raw(raw).unwrap_or_else(|| "unavailable".to_string());
    let omitted_lines = body.lines().count();
    let footer = format!(
        "\n[wm-squeeze: {:.1}KB -> ~{:.1}KB (-{:.0}%) | {} lines | full: {}]",
        raw.len() as f64 / 1024.0,
        max_bytes as f64 / 1024.0,
        (raw.len().saturating_sub(max_bytes)) as f64 * 100.0 / raw.len().max(1) as f64,
        omitted_lines,
        spill_path
    );
    let mid_sep = "\n... [middle omitted; errors/summary preserved below] ...\n";
    let avail = max_bytes.saturating_sub(footer.len() + mid_sep.len() + 16);
    let head_budget = (avail * 55) / 100;
    let tail_budget = avail.saturating_sub(head_budget);
    let head = slice_utf8_prefix(&body, head_budget);
    let tail = slice_utf8_suffix(&body, tail_budget);
    (format!("{}{}{}{}", head, mid_sep, tail, footer), true)
}

/// Strictly validate that a command + args is a read-only / local build-test invocation
/// safe for auto-allowance under `command(wm-squeeze --safe)`.
fn validate_safe(cmd: &str, args: &[String]) -> Result<Kind, String> {
    let base = Path::new(cmd)
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or(cmd);

    match base {
        "git" => {
            // Reject git flags that can execute arbitrary programs or alter config
            let mut i = 0;
            while i < args.len() {
                let a = args[i].as_str();
                if a == "-c"
                    || a.starts_with("-c")
                    || a.starts_with("--exec-path")
                    || a.starts_with("--config-env")
                    || a.starts_with("--upload-pack")
                    || a == "--paginate"
                {
                    return Err(format!("disallowed git global flag: {}", a));
                }
                if a == "-C" {
                    i += 2;
                    continue;
                }
                if a.starts_with('-') {
                    i += 1;
                    continue;
                }
                let sub = a;
                let next = args.get(i + 1).map(|s| s.as_str()).unwrap_or("");
                let ok = matches!(
                    sub,
                    "status"
                        | "diff"
                        | "log"
                        | "show"
                        | "ls-files"
                        | "rev-parse"
                        | "blame"
                        | "shortlog"
                        | "describe"
                ) || (sub == "branch" && !args[i + 1..].iter().any(|x| matches!(x.as_str(), "-d" | "-D" | "-m" | "-M" | "--delete")))
                    || (sub == "tag" && !args[i + 1..].iter().any(|x| matches!(x.as_str(), "-d" | "--delete")))
                    || (sub == "stash" && next == "list")
                    || (sub == "worktree" && next == "list")
                    || (sub == "remote" && (next == "-v" || next.is_empty()));
                if ok {
                    return Ok(Kind::Git);
                }
                return Err(format!("disallowed git subcommand: {}", sub));
            }
            Err("missing git subcommand".to_string())
        }
        "cargo" => {
            if args.iter().any(|a| a == "--config" || a.starts_with("--config=")) {
                return Err("disallowed cargo --config".to_string());
            }
            let sub = args
                .iter()
                .find(|a| !a.starts_with('-') && !a.starts_with('+'))
                .map(|s| s.as_str())
                .unwrap_or("");
            if matches!(sub, "check" | "test" | "clippy" | "build" | "tree" | "metadata") {
                Ok(Kind::Cargo)
            } else {
                Err(format!("disallowed cargo subcommand: {}", sub))
            }
        }
        "pytest" => Ok(Kind::Pytest),
        "npm" | "pnpm" | "yarn" => {
            let s0 = args.first().map(|s| s.as_str()).unwrap_or("");
            let s1 = args.get(1).map(|s| s.as_str()).unwrap_or("");
            if matches!(s0, "test" | "ls" | "outdated")
                || (s0 == "run" && matches!(s1, "typecheck" | "lint" | "build" | "test" | "check"))
            {
                Ok(Kind::Generic)
            } else {
                Err(format!("disallowed {} subcommand: {} {}", base, s0, s1))
            }
        }
        "rg" => {
            if args.iter().any(|a| a == "--pre" || a.starts_with("--pre=")) {
                return Err("disallowed rg --pre".to_string());
            }
            Ok(Kind::Rg)
        }
        "grep" => Ok(Kind::Rg),
        "find" => {
            for a in args {
                if matches!(
                    a.as_str(),
                    "-exec" | "-execdir" | "-ok" | "-okdir" | "-delete" | "-fls" | "-fprint" | "-fprint0" | "-fprintf"
                ) {
                    return Err(format!("disallowed find action: {}", a));
                }
            }
            Ok(Kind::Ls)
        }
        "ls" | "tree" | "wc" | "du" | "df" | "stat" | "file" | "cat" | "head" | "tail" | "nl" => {
            Ok(Kind::Ls)
        }
        "ruff" | "mypy" | "eslint" | "tsc" => Ok(Kind::Generic),
        "wm3" => {
            let sub = args
                .iter()
                .find(|a| !a.starts_with('-'))
                .map(|s| s.as_str())
                .unwrap_or("");
            if matches!(sub, "recall" | "stats" | "session" | "list")
                || args.iter().any(|a| a == "--version")
            {
                Ok(Kind::Generic)
            } else {
                Err(format!("disallowed wm3 subcommand: {}", sub))
            }
        }
        "gh" => {
            let s0 = args.first().map(|s| s.as_str()).unwrap_or("");
            let s1 = args.get(1).map(|s| s.as_str()).unwrap_or("");
            if matches!(s0, "pr" | "issue" | "repo" | "run" | "workflow" | "release")
                && matches!(s1, "list" | "view" | "status" | "diff" | "checks")
            {
                Ok(Kind::Generic)
            } else {
                Err(format!("disallowed gh command: {} {}", s0, s1))
            }
        }
        _ => Err(format!("command not in wm-squeeze --safe allowlist: {}", base)),
    }
}

fn parse_kind(s: &str) -> Kind {
    match s {
        "cargo" => Kind::Cargo,
        "pytest" => Kind::Pytest,
        "git" => Kind::Git,
        "rg" | "grep" => Kind::Rg,
        "ls" | "find" => Kind::Ls,
        _ => Kind::Generic,
    }
}

fn main() {
    let args: Vec<String> = env::args().skip(1).collect();
    if args.is_empty() || args[0] == "-h" || args[0] == "--help" {
        eprintln!(
            "Usage:\n  wm-squeeze --safe [--cwd DIR] [--max-bytes N] [--] <cmd> [args...]\n  wm-squeeze --pipe [--kind auto|cargo|pytest|git|rg|ls|generic] [--max-bytes N]\n  wm-squeeze --stats"
        );
        std::process::exit(0);
    }

    if args[0] == "--stats" {
        print_stats();
        return;
    }

    let mut max_bytes = DEFAULT_MAX_BYTES;

    if args[0] == "--pipe" {
        let mut kind = Kind::Generic;
        let mut i = 1;
        while i < args.len() {
            match args[i].as_str() {
                "--kind" if i + 1 < args.len() => {
                    kind = parse_kind(&args[i + 1]);
                    i += 2;
                }
                "--max-bytes" if i + 1 < args.len() => {
                    max_bytes = args[i + 1].parse().unwrap_or(DEFAULT_MAX_BYTES).clamp(512, 9200);
                    i += 2;
                }
                _ => i += 1,
            }
        }
        let mut raw = String::new();
        let _ = io::stdin().read_to_string(&mut raw);
        let (out, spilled) = squeeze(&raw, kind, max_bytes);
        log_stats("pipe", raw.len(), out.len(), spilled);
        println!("{}", out);
        return;
    }

    if args[0] != "--safe" {
        eprintln!("wm-squeeze: expected --safe, --pipe, or --stats");
        std::process::exit(2);
    }

    let mut cwd: Option<String> = None;
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--cwd" if i + 1 < args.len() => {
                cwd = Some(args[i + 1].clone());
                i += 2;
            }
            "--max-bytes" if i + 1 < args.len() => {
                max_bytes = args[i + 1].parse().unwrap_or(DEFAULT_MAX_BYTES).clamp(512, 9200);
                i += 2;
            }
            "--" => {
                i += 1;
                break;
            }
            _ => break,
        }
    }

    if i >= args.len() {
        eprintln!("wm-squeeze --safe: missing command");
        std::process::exit(2);
    }

    let cmd = &args[i];
    let cmd_args = &args[i + 1..];
    let kind = match validate_safe(cmd, cmd_args) {
        Ok(k) => k,
        Err(msg) => {
            eprintln!("wm-squeeze --safe refused: {}", msg);
            std::process::exit(2);
        }
    };

    let mut command = Command::new(cmd);
    command.args(cmd_args).stdout(Stdio::piped()).stderr(Stdio::piped());
    if let Some(dir) = cwd {
        command.current_dir(dir);
    }
    // Keep git/cargo clean & deterministic without pagers
    command.env("GIT_PAGER", "cat").env("PAGER", "cat").env("NO_COLOR", "1");

    let output = match command.output() {
        Ok(o) => o,
        Err(e) => {
            eprintln!("wm-squeeze failed to spawn '{}': {}", cmd, e);
            std::process::exit(127);
        }
    };

    let code = output.status.code().unwrap_or(1);
    let mut combined = String::from_utf8_lossy(&output.stdout).into_owned();
    let stderr_s = String::from_utf8_lossy(&output.stderr);
    if !stderr_s.trim().is_empty() {
        if !combined.is_empty() && !combined.ends_with('\n') {
            combined.push('\n');
        }
        combined.push_str(&stderr_s);
    }

    let label = if let Some(sub) = cmd_args.first() {
        format!("{} {}", Path::new(cmd).file_name().and_then(|s| s.to_str()).unwrap_or(cmd), sub)
    } else {
        cmd.clone()
    };

    let (squeezed, spilled) = squeeze(&combined, kind, max_bytes);
    log_stats(&label, combined.len(), squeezed.len(), spilled);
    if !squeezed.is_empty() {
        println!("{}", squeezed);
    }
    std::process::exit(code);
}
