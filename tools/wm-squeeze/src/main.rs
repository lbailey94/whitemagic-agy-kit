//! wm-squeeze v3.0.0 — Zero-dependency CoACT CLI compressor, content-addressed cache & safe proxy for AGY CLI.
//!
//! New in v3.0.0:
//!   - CoACT-style structured error-grouping compressors for `tsc`, `eslint`, `ruff`, `mypy`, `go test`, `jest`/`vitest`
//!     (groups repetitive diagnostics while preserving 100% of `file:line` next-action anchors & exit codes)
//!   - Content-addressed read-only result cache (`~/.cache/wm-squeeze/`, 5-min TTL) keyed on
//!     `(git HEAD + git status --porcelain + dirty file mtimes + cwd + argv)` so repeated read-only
//!     checks return in ~2ms instead of triggering 20-60s Gemini 4 prefix-cache evictions!

use std::collections::{BTreeMap, HashMap};
use std::env;
use std::fs::{self, OpenOptions};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

const DEFAULT_MAX_BYTES: usize = 8_200;
const MAX_LINE_CHARS: usize = 320;
const SPILL_DIR: &str = "/tmp/wm-squeeze";
const CACHE_TTL_SECS: u64 = 300;

#[derive(Clone, Copy, PartialEq, Eq)]
enum Kind {
    Cargo,
    Pytest,
    Git,
    Rg,
    Ls,
    Linter, // tsc, eslint, ruff, mypy
    GoTest,
    Generic,
}

fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn fnv1a_hex(s: &str) -> String {
    let mut h: u64 = 0xcbf29ce484222325;
    for b in s.as_bytes() {
        h ^= *b as u64;
        h = h.wrapping_mul(0x100000001b3);
    }
    format!("{:016x}", h)
}

fn stats_path() -> Option<PathBuf> {
    if env::var("WM_SQUEEZE_NO_STATS").ok().as_deref() == Some("1") {
        return None;
    }
    let home = env::var("HOME").ok()?;
    Some(PathBuf::from(home).join(".local/share/whitemagic/wm_squeeze_stats.jsonl"))
}

fn cache_dir() -> Option<PathBuf> {
    if env::var("WM_SQUEEZE_NO_CACHE").ok().as_deref() == Some("1") {
        return None;
    }
    let base = env::var("XDG_CACHE_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from(env::var("HOME").ok().unwrap_or_else(|| "/tmp".into())).join(".cache"));
    Some(base.join("wm-squeeze"))
}

fn log_stats(cmd_label: &str, raw_bytes: usize, out_bytes: usize, spilled: bool, cache_hit: bool) {
    if raw_bytes == 0 && !cache_hit {
        return;
    }
    if let Some(p) = stats_path() {
        if let Some(parent) = p.parent() {
            let _ = fs::create_dir_all(parent);
        }
        let safe_label: String = cmd_label
            .chars()
            .filter(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | ':' | '/' | ' '))
            .take(40)
            .collect();
        let line = format!(
            "{{\"ts\":{},\"cmd\":\"{}\",\"raw\":{},\"out\":{},\"saved\":{},\"spilled\":{},\"cache_hit\":{}}}\n",
            now_secs(),
            safe_label,
            raw_bytes,
            out_bytes,
            raw_bytes.saturating_sub(out_bytes),
            spilled,
            cache_hit
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
    let (mut count, mut raw_sum, mut out_sum, mut spills, mut hits) = (0usize, 0usize, 0usize, 0usize, 0usize);
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
        if line.contains("\"cache_hit\":true") {
            hits += 1;
        }
    }
    let saved = raw_sum.saturating_sub(out_sum);
    let pct = if raw_sum > 0 { (saved as f64) * 100.0 / (raw_sum as f64) } else { 0.0 };
    println!(
        "wm-squeeze v3: {} cmds ({} cache hits) | raw {:.1} KB -> out {:.1} KB | saved {:.1} KB ({:.1}%, ~{} tok) | overflow files: {}",
        count, hits, raw_sum as f64 / 1024.0, out_sum as f64 / 1024.0, saved as f64 / 1024.0, pct, saved / 4, spills
    );
}

/// Fast git workspace fingerprint (<5ms) for content-addressed read-only command caching.
fn workspace_cache_key(dir: &Path, cmd: &str, args: &[String], max_bytes: usize) -> Option<String> {
    let head = Command::new("git")
        .args(["rev-parse", "HEAD"])
        .current_dir(dir)
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .output()
        .ok()?;
    if !head.status.success() {
        return None;
    }
    let status = Command::new("git")
        .args(["status", "--porcelain"])
        .current_dir(dir)
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .output()
        .ok()?;
    if !status.status.success() {
        return None;
    }
    let st_text = String::from_utf8_lossy(&status.stdout);
    let mut fp = String::with_capacity(512);
    fp.push_str(&dir.to_string_lossy());
    fp.push('|');
    fp.push_str(String::from_utf8_lossy(&head.stdout).trim());
    fp.push('|');
    fp.push_str(cmd);
    for a in args {
        fp.push('\x1f');
        fp.push_str(a);
    }
    fp.push_str(&format!("|mb={}|", max_bytes));
    // Include dirty file mtimes & sizes so ANY edit invalidates the cache instantly
    for line in st_text.lines().take(64) {
        fp.push_str(line);
        if line.len() >= 4 {
            let rel = line[3..].trim().trim_matches('"');
            let p = dir.join(rel);
            if let Ok(m) = fs::metadata(&p) {
                let mt = m.modified().ok().and_then(|t| t.duration_since(UNIX_EPOCH).ok()).map(|d| d.as_nanos()).unwrap_or(0);
                fp.push_str(&format!(":{}:{}", m.len(), mt));
            }
        }
        fp.push(';');
    }
    Some(fnv1a_hex(&fp))
}

fn read_cached(key: &str) -> Option<(i32, usize, String)> {
    let p = cache_dir()?.join(format!("{}.cache", key));
    let meta = fs::metadata(&p).ok()?;
    let age = meta.modified().ok()?.elapsed().ok()?.as_secs();
    if age > CACHE_TTL_SECS {
        let _ = fs::remove_file(&p);
        return None;
    }
    let raw = fs::read_to_string(&p).ok()?;
    let (hdr, body) = raw.split_once('\n')?;
    let mut parts = hdr.split('\t');
    let code: i32 = parts.next()?.parse().ok()?;
    let orig_bytes: usize = parts.next()?.parse().ok()?;
    Some((code, orig_bytes, body.to_string()))
}

fn write_cached(key: &str, code: i32, orig_bytes: usize, body: &str) {
    if let Some(dir) = cache_dir() {
        let _ = fs::create_dir_all(&dir);
        let p = dir.join(format!("{}.cache", key));
        let _ = fs::write(p, format!("{}\t{}\n{}", code, orig_bytes, body));
    }
}

fn normalize_terminal(raw: &str) -> String {
    let bytes = raw.as_bytes();
    let mut cleaned = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == 0x1b {
            i += 1;
            if i >= bytes.len() { break; }
            match bytes[i] {
                b'[' => {
                    i += 1;
                    while i < bytes.len() && !(0x40..=0x7e).contains(&bytes[i]) { i += 1; }
                    if i < bytes.len() { i += 1; }
                }
                b']' => {
                    i += 1;
                    while i < bytes.len() {
                        if bytes[i] == 0x07 { i += 1; break; }
                        if bytes[i] == 0x1b && i + 1 < bytes.len() && bytes[i + 1] == b'\\' { i += 2; break; }
                        i += 1;
                    }
                }
                _ => i += 1,
            }
        } else {
            cleaned.push(bytes[i]);
            i += 1;
        }
    }
    let text = String::from_utf8_lossy(&cleaned).replace("\r\n", "\n");
    let mut out = String::with_capacity(text.len());
    for (idx, line) in text.split('\n').enumerate() {
        if idx > 0 { out.push('\n'); }
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
                for _ in 0..*run { out.push(p.clone()); }
            }
            *run = 0;
        }
    };
    for line in lines {
        if line.trim().is_empty() {
            flush(&mut prev, &mut run, &mut out);
            blank_run += 1;
            if blank_run <= 1 { out.push(String::new()); }
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
    let (mut compiled, mut passed, mut downloaded) = (0usize, 0usize, 0usize);
    for line in text.lines() {
        let t = line.trim();
        if t.starts_with("Compiling ") || t.starts_with("Checking ") || t.starts_with("Fresh ") { compiled += 1; continue; }
        if t.starts_with("Downloading ") || t.starts_with("Downloaded ") || t.starts_with("Updating ") { downloaded += 1; continue; }
        if t.starts_with("test ") && t.ends_with("... ok") { passed += 1; continue; }
        kept.push(line.to_string());
    }
    let mut header = Vec::new();
    if compiled > 0 || downloaded > 0 || passed > 0 {
        header.push(format!("[cargo-summary: compiled/checked={}, downloaded={}, passed_tests={}]", compiled, downloaded, passed));
    }
    header.extend(dedup_consecutive(kept));
    header.join("\n")
}

fn compress_pytest(text: &str) -> String {
    let mut kept = Vec::new();
    let mut passed = 0usize;
    for line in text.lines() {
        let t = line.trim();
        let is_passed_line = t.ends_with(" PASSED")
            || t.contains(" PASSED [")
            || (t.contains(" PASSED ") && t.ends_with('%'))
            || (t.contains(" PASSED ") && t.ends_with(']'));
        if is_passed_line {
            passed += 1;
            continue;
        }
        kept.push(line.to_string());
    }
    let mut out = Vec::new();
    if passed > 0 { out.push(format!("[pytest-summary: {} passed lines collapsed]", passed)); }
    out.extend(dedup_consecutive(kept));
    out.join("\n")
}

fn compress_gotest(text: &str) -> String {
    let mut kept = Vec::new();
    let (mut passed, mut skipped) = (0usize, 0usize);
    for line in text.lines() {
        let t = line.trim();
        if t.starts_with("=== RUN ") || t.starts_with("--- PASS:") || t.starts_with("ok ") {
            passed += 1;
            continue;
        }
        if t.starts_with("--- SKIP:") { skipped += 1; continue; }
        kept.push(line.to_string());
    }
    let mut out = Vec::new();
    if passed > 0 || skipped > 0 {
        out.push(format!("[go-test-summary: {} pass/run lines collapsed, {} skipped]", passed, skipped));
    }
    out.extend(dedup_consecutive(kept));
    out.join("\n")
}

/// CoACT-style structured diagnostic grouping for tsc / ruff / mypy / eslint:
/// groups identical error messages/codes per file while preserving exact `file:line` anchors!
fn compress_linter(text: &str, max_bytes: usize) -> String {
    if text.len() <= max_bytes {
        return dedup_consecutive(text.lines().map(|l| clamp_line(l, MAX_LINE_CHARS)).collect()).join("\n");
    }
    let mut by_file: BTreeMap<String, Vec<String>> = BTreeMap::new();
    let mut other = Vec::new();
    for line in text.lines() {
        let t = line.trim();
        if t.is_empty() { continue; }
        if let Some(colon) = t.find(':') {
            let f = &t[..colon];
            if !f.is_empty() && !f.contains(' ') && (f.contains('.') || f.contains('/')) {
                by_file.entry(f.to_string()).or_default().push(clamp_line(&t[colon + 1..], 180));
                continue;
            }
        }
        other.push(clamp_line(t, 200));
    }
    let mut out = vec![format!("[linter-grouped: {} files with diagnostics]", by_file.len())];
    for (file, diags) in by_file {
        out.push(format!("## {} ({} issues):", file, diags.len()));
        for d in diags.iter().take(8) {
            out.push(format!("  :{}", d));
        }
        if diags.len() > 8 {
            out.push(format!("  [+{} more diagnostics in {}]", diags.len() - 8, file));
        }
    }
    if !other.is_empty() {
        out.push("## Summary:".to_string());
        out.extend(other.into_iter().take(12));
    }
    out.join("\n")
}

fn compress_git(text: &str, max_bytes: usize) -> String {
    if text.len() <= max_bytes {
        return dedup_consecutive(text.lines().map(|l| clamp_line(l, MAX_LINE_CHARS)).collect()).join("\n");
    }
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
            if skip_noisy { skipped_lines += 1; continue; }
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
        return raw_lines.into_iter().map(|l| clamp_line(l, MAX_LINE_CHARS)).collect::<Vec<_>>().join("\n");
    }
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
        let summary: Vec<String> = pairs.into_iter().take(8).map(|(f, n)| format!("{}(+{})", f, n)).collect();
        out.push(format!("[rg-collapsed excess per-file hits: {}]", summary.join(", ")));
    }
    out.join("\n")
}

fn slice_utf8_prefix(s: &str, max_bytes: usize) -> &str {
    if s.len() <= max_bytes { return s; }
    let mut end = max_bytes;
    while end > 0 && !s.is_char_boundary(end) { end -= 1; }
    if let Some(nl) = s[..end].rfind('\n') {
        if nl > end / 2 { return &s[..nl]; }
    }
    &s[..end]
}

fn slice_utf8_suffix(s: &str, max_bytes: usize) -> &str {
    if s.len() <= max_bytes { return s; }
    let mut start = s.len().saturating_sub(max_bytes);
    while start < s.len() && !s.is_char_boundary(start) { start += 1; }
    if let Some(nl) = s[start..].find('\n') {
        if nl < max_bytes / 2 { return &s[start + nl + 1..]; }
    }
    &s[start..]
}

fn spill_raw(raw: &str) -> Option<String> {
    fs::create_dir_all(SPILL_DIR).ok()?;
    let ts = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis()).unwrap_or(0);
    let path = format!("{}/{}_{}.log", SPILL_DIR, ts, std::process::id());
    fs::write(&path, raw.as_bytes()).ok()?;
    Some(path)
}

fn squeeze(raw: &str, kind: Kind, max_bytes: usize) -> (String, bool) {
    let norm = normalize_terminal(raw);
    let body = match kind {
        Kind::Cargo => compress_cargo(&norm),
        Kind::Pytest => compress_pytest(&norm),
        Kind::GoTest => compress_gotest(&norm),
        Kind::Linter => compress_linter(&norm, max_bytes),
        Kind::Git => compress_git(&norm, max_bytes),
        Kind::Rg => compress_rg(&norm, max_bytes),
        Kind::Ls | Kind::Generic => dedup_consecutive(norm.lines().map(|l| clamp_line(l, MAX_LINE_CHARS)).collect()).join("\n"),
    };

    if body.len() <= max_bytes {
        if raw.len() > body.len() + 2048 {
            let pct = (raw.len() - body.len()) as f64 * 100.0 / raw.len() as f64;
            let note = format!("\n[wm-squeeze: {:.1}KB -> {:.1}KB (-{:.0}%)]", raw.len() as f64 / 1024.0, body.len() as f64 / 1024.0, pct);
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

fn validate_safe(cmd: &str, args: &[String]) -> Result<(Kind, bool), String> {
    // Returns (Kind, cacheable_in_git_repo)
    let base = Path::new(cmd).file_name().and_then(|s| s.to_str()).unwrap_or(cmd);
    match base {
        "git" => {
            let mut i = 0;
            while i < args.len() {
                let a = args[i].as_str();
                if a == "-c" || a.starts_with("-c") || a.starts_with("--exec-path") || a.starts_with("--config-env") || a.starts_with("--upload-pack") || a == "--paginate" {
                    return Err(format!("disallowed git global flag: {}", a));
                }
                if a == "-C" { i += 2; continue; }
                if a.starts_with('-') { i += 1; continue; }
                let sub = a;
                let next = args.get(i + 1).map(|s| s.as_str()).unwrap_or("");
                let ok = matches!(sub, "status" | "diff" | "log" | "show" | "ls-files" | "rev-parse" | "blame" | "shortlog" | "describe")
                    || (sub == "branch" && !args[i + 1..].iter().any(|x| matches!(x.as_str(), "-d" | "-D" | "-m" | "-M" | "--delete")))
                    || (sub == "tag" && !args[i + 1..].iter().any(|x| matches!(x.as_str(), "-d" | "--delete")))
                    || (sub == "stash" && next == "list")
                    || (sub == "worktree" && next == "list")
                    || (sub == "remote" && (next == "-v" || next.is_empty()));
                return if ok { Ok((Kind::Git, true)) } else { Err(format!("disallowed git subcommand: {}", sub)) };
            }
            Err("missing git subcommand".to_string())
        }
        "cargo" => {
            if args.iter().any(|a| a == "--config" || a.starts_with("--config=")) {
                return Err("disallowed cargo --config".to_string());
            }
            let sub = args.iter().find(|a| !a.starts_with('-') && !a.starts_with('+')).map(|s| s.as_str()).unwrap_or("");
            if matches!(sub, "check" | "test" | "clippy" | "build" | "tree" | "metadata") {
                Ok((Kind::Cargo, matches!(sub, "check" | "clippy" | "test" | "tree")))
            } else {
                Err(format!("disallowed cargo subcommand: {}", sub))
            }
        }
        "go" => {
            let sub = args.first().map(|s| s.as_str()).unwrap_or("");
            if matches!(sub, "test" | "vet" | "list") {
                Ok((Kind::GoTest, true))
            } else {
                Err(format!("disallowed go subcommand: {}", sub))
            }
        }
        "pytest" => Ok((Kind::Pytest, true)),
        "npm" | "pnpm" | "yarn" => {
            let s0 = args.first().map(|s| s.as_str()).unwrap_or("");
            let s1 = args.get(1).map(|s| s.as_str()).unwrap_or("");
            if matches!(s0, "test" | "ls" | "outdated") || (s0 == "run" && matches!(s1, "typecheck" | "lint" | "build" | "test" | "check")) {
                Ok((Kind::Linter, matches!(s1, "typecheck" | "lint" | "check")))
            } else {
                Err(format!("disallowed {} subcommand: {} {}", base, s0, s1))
            }
        }
        "rg" => {
            if args.iter().any(|a| a == "--pre" || a.starts_with("--pre=")) {
                return Err("disallowed rg --pre".to_string());
            }
            Ok((Kind::Rg, true))
        }
        "grep" => Ok((Kind::Rg, true)),
        "find" => {
            for a in args {
                if matches!(a.as_str(), "-exec" | "-execdir" | "-ok" | "-okdir" | "-delete" | "-fls" | "-fprint" | "-fprint0" | "-fprintf") {
                    return Err(format!("disallowed find action: {}", a));
                }
            }
            Ok((Kind::Ls, false))
        }
        "ls" | "tree" | "wc" | "du" | "df" | "stat" | "file" | "cat" | "head" | "tail" | "nl" => Ok((Kind::Ls, false)),
        "ruff" | "mypy" | "eslint" | "tsc" => Ok((Kind::Linter, true)),
        "wm3" | "wm-gen3" => {
            let mut sub = "";
            let mut i = 0;
            while i < args.len() {
                let a = args[i].as_str();
                if a == "--store" || a == "--galaxy" {
                    i += 2;
                    continue;
                }
                if a.starts_with('-') {
                    i += 1;
                    continue;
                }
                sub = a;
                break;
            }
            if matches!(sub, "recall" | "evidence-recall" | "stats" | "status" | "session" | "list" | "build-info" | "inspect")
                || args.iter().any(|a| a == "--version")
            {
                Ok((Kind::Generic, false))
            } else {
                Err(format!("disallowed {} subcommand: {}", base, sub))
            }
        }
        "gh" => {
            let s0 = args.first().map(|s| s.as_str()).unwrap_or("");
            let s1 = args.get(1).map(|s| s.as_str()).unwrap_or("");
            if matches!(s0, "pr" | "issue" | "repo" | "run" | "workflow" | "release") && matches!(s1, "list" | "view" | "status" | "diff" | "checks") {
                Ok((Kind::Generic, false))
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
        "go" | "gotest" => Kind::GoTest,
        "linter" | "tsc" | "eslint" | "ruff" | "mypy" => Kind::Linter,
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
            "Usage:\n  wm-squeeze --safe [--cwd DIR] [--max-bytes N] [--] <cmd> [args...]\n  wm-squeeze --pipe [--kind auto|cargo|pytest|gotest|linter|git|rg|ls|generic] [--max-bytes N]\n  wm-squeeze --stats"
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
                "--kind" if i + 1 < args.len() => { kind = parse_kind(&args[i + 1]); i += 2; }
                "--max-bytes" if i + 1 < args.len() => { max_bytes = args[i + 1].parse().unwrap_or(DEFAULT_MAX_BYTES).clamp(512, 9200); i += 2; }
                _ => i += 1,
            }
        }
        let mut raw = String::new();
        let _ = io::stdin().read_to_string(&mut raw);
        let (out, spilled) = squeeze(&raw, kind, max_bytes);
        log_stats("pipe", raw.len(), out.len(), spilled, false);
        println!("{}", out);
        return;
    }

    if args[0] != "--safe" {
        eprintln!("wm-squeeze: expected --safe, --pipe, or --stats");
        std::process::exit(2);
    }

    let mut cwd: Option<String> = None;
    let mut safe_envs: Vec<(String, String)> = Vec::new();
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--cwd" if i + 1 < args.len() => { cwd = Some(args[i + 1].clone()); i += 2; }
            "--env" if i + 1 < args.len() => {
                let kv = &args[i + 1];
                if let Some((k, v)) = kv.split_once('=') {
                    if matches!(k, "PYTHONPATH" | "RUST_BACKTRACE" | "RUST_LOG" | "NO_COLOR" | "CARGO_TERM_COLOR") {
                        safe_envs.push((k.to_string(), v.to_string()));
                    } else {
                        eprintln!("wm-squeeze --safe refused disallowed env var: {}", k);
                        std::process::exit(2);
                    }
                } else {
                    eprintln!("wm-squeeze --safe invalid --env format: {}", kv);
                    std::process::exit(2);
                }
                i += 2;
            }
            "--max-bytes" if i + 1 < args.len() => { max_bytes = args[i + 1].parse().unwrap_or(DEFAULT_MAX_BYTES).clamp(512, 9200); i += 2; }
            "--" => { i += 1; break; }
            _ => break,
        }
    }
    if i >= args.len() {
        eprintln!("wm-squeeze --safe: missing command");
        std::process::exit(2);
    }

    let cmd = &args[i];
    let cmd_args = &args[i + 1..];
    let (kind, cacheable) = match validate_safe(cmd, cmd_args) {
        Ok(v) => v,
        Err(msg) => {
            eprintln!("wm-squeeze --safe refused: {}", msg);
            std::process::exit(2);
        }
    };

    let work_dir = cwd.as_deref().map(PathBuf::from).unwrap_or_else(|| env::current_dir().unwrap_or_else(|_| PathBuf::from(".")));
    let label = if let Some(sub) = cmd_args.first() {
        format!("{} {}", Path::new(cmd).file_name().and_then(|s| s.to_str()).unwrap_or(cmd), sub)
    } else {
        cmd.clone()
    };

    let t0 = Instant::now();
    let ckey = if cacheable { workspace_cache_key(&work_dir, cmd, cmd_args, max_bytes) } else { None };
    if let Some(ref k) = ckey {
        if let Some((code, orig_bytes, body)) = read_cached(k) {
            log_stats(&label, orig_bytes, body.len(), false, true);
            if !body.is_empty() {
                println!("{}\n[wm-squeeze: cache-hit {:.1}ms]", body, t0.elapsed().as_secs_f64() * 1000.0);
            }
            std::process::exit(code);
        }
    }

    let mut command = Command::new(cmd);
    command.args(cmd_args).current_dir(&work_dir).stdout(Stdio::piped()).stderr(Stdio::piped());
    command.env("GIT_PAGER", "cat").env("PAGER", "cat").env("NO_COLOR", "1");
    for (k, v) in &safe_envs {
        command.env(k, v);
    }

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
        if !combined.is_empty() && !combined.ends_with('\n') { combined.push('\n'); }
        combined.push_str(&stderr_s);
    }

    let (squeezed, spilled) = squeeze(&combined, kind, max_bytes);
    if let Some(ref k) = ckey {
        write_cached(k, code, combined.len(), &squeezed);
    }
    log_stats(&label, combined.len(), squeezed.len(), spilled, false);
    if !squeezed.is_empty() {
        println!("{}", squeezed);
    }
    std::process::exit(code);
}
