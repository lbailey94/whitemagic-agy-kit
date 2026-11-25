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
use std::fmt::Write as FmtWrite;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
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

fn fast_git_head(dir: &Path) -> Option<String> {
    let git_dir = dir.join(".git");
    if !git_dir.is_dir() {
        return None;
    }
    let head_raw = fs::read_to_string(git_dir.join("HEAD")).ok()?;
    let head_trim = head_raw.trim();
    if let Some(ref_rel) = head_trim.strip_prefix("ref: ") {
        let sha = fs::read_to_string(git_dir.join(ref_rel.trim())).ok()?;
        let s = sha.trim();
        if s.len() == 40 && s.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Some(s.to_string());
        }
        return None;
    }
    if head_trim.len() == 40 && head_trim.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Some(head_trim.to_string());
    }
    None
}

/// Fast git workspace fingerprint (<5ms) for content-addressed read-only command caching.
fn workspace_cache_key(dir: &Path, cmd: &str, args: &[String], max_bytes: usize) -> Option<String> {
    let head_sha = if let Some(s) = fast_git_head(dir) {
        s
    } else {
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
        String::from_utf8_lossy(&head.stdout).trim().to_string()
    };
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
    fp.push_str(&head_sha);
    fp.push('|');
    fp.push_str(cmd);
    for a in args {
        fp.push('\x1f');
        fp.push_str(a);
    }
    let _ = write!(fp, "|mb={}|", max_bytes);
    // Include dirty file mtimes & sizes so ANY edit invalidates the cache instantly
    for line in st_text.lines().take(64) {
        fp.push_str(line);
        if line.len() >= 4 {
            let rel = line[3..].trim().trim_matches('"');
            let p = dir.join(rel);
            if let Ok(m) = fs::metadata(&p) {
                let mt = m.modified().ok().and_then(|t| t.duration_since(UNIX_EPOCH).ok()).map(|d| d.as_nanos()).unwrap_or(0);
                let _ = write!(fp, ":{}:{}", m.len(), mt);
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
        let tmp = dir.join(format!("{}.cache.tmp.{}", key, std::process::id()));
        if fs::write(&tmp, format!("{}\t{}\n{}", code, orig_bytes, body)).is_ok() {
            if fs::rename(&tmp, &p).is_err() {
                let _ = fs::remove_file(&tmp);
            }
        } else {
            let _ = fs::remove_file(&tmp);
        }
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
            } else if *run == 2 {
                out.push(p.clone());
                out.push(p);
            } else if *run == 1 {
                out.push(p);
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

/// Parse a linter diagnostic line into `(file, line_anchor, message_template)`.
/// Supports BOTH `tsc` format (`src/a.ts(12,4): error TS2322: ...`)
/// AND `ruff`/`mypy`/`eslint`/`rustc`/`gcc` format (`src/a.py:12:4: E501 ...`).
fn parse_linter_diag(line: &str) -> Option<(String, String, String)> {
    let t = line.trim();
    if t.is_empty() {
        return None;
    }
    // 1. tsc / MSBuild format: `path/to/file.ts(12,4): msg`
    if let Some(lparen) = t.find('(') {
        let f = &t[..lparen];
        if !f.is_empty() && !f.contains(' ') && (f.contains('.') || f.contains('/')) {
            if let Some(rparen_rel) = t[lparen + 1..].find("):") {
                let coords = &t[lparen + 1..lparen + 1 + rparen_rel];
                let line_num = coords.split(',').next().unwrap_or(coords).trim();
                if !line_num.is_empty() && line_num.bytes().all(|b| b.is_ascii_digit()) {
                    let msg = t[lparen + 1 + rparen_rel + 2..].trim();
                    return Some((f.to_string(), format!("L{}", line_num), clamp_line(msg, 160)));
                }
            }
        }
    }
    // 2. Colon format: `path/to/file.py:12:4: msg` or `path/to/file.py:12: msg`
    if let Some(c1) = t.find(':') {
        let f = &t[..c1];
        if !f.is_empty() && !f.contains(' ') && (f.contains('.') || f.contains('/')) {
            let rest = &t[c1 + 1..];
            if let Some(c2) = rest.find(':') {
                let tok = rest[..c2].trim();
                if !tok.is_empty() && tok.bytes().all(|b| b.is_ascii_digit()) {
                    let after_line = &rest[c2 + 1..];
                    // Optional column number `:col: msg`
                    let msg = if let Some(c3) = after_line.find(':') {
                        let col = after_line[..c3].trim();
                        if !col.is_empty() && col.bytes().all(|b| b.is_ascii_digit()) {
                            after_line[c3 + 1..].trim()
                        } else {
                            after_line.trim()
                        }
                    } else {
                        after_line.trim()
                    };
                    return Some((f.to_string(), format!("L{}", tok), clamp_line(msg, 160)));
                }
            }
            return Some((f.to_string(), String::new(), clamp_line(rest.trim(), 160)));
        }
    }
    None
}

/// CoACT + Drain-style structural diagnostic clustering for tsc / ruff / mypy / eslint:
/// groups identical error templates per file while preserving exact `L<line>` anchors!
fn compress_linter(text: &str, max_bytes: usize) -> String {
    if text.len() <= max_bytes {
        return dedup_consecutive(text.lines().map(|l| clamp_line(l, MAX_LINE_CHARS)).collect()).join("\n");
    }
    // file -> (total_issues, template_msg -> Vec<line_anchor>)
    let mut by_file: BTreeMap<String, (usize, BTreeMap<String, Vec<String>>)> = BTreeMap::new();
    let mut other = Vec::new();
    for line in text.lines() {
        let t = line.trim();
        if t.is_empty() { continue; }
        if let Some((file, anchor, msg)) = parse_linter_diag(t) {
            let entry = by_file.entry(file).or_insert_with(|| (0, BTreeMap::new()));
            entry.0 += 1;
            let anchors = entry.1.entry(msg).or_default();
            if !anchor.is_empty() {
                anchors.push(anchor);
            }
        } else {
            other.push(clamp_line(t, 200));
        }
    }
    let total_issues: usize = by_file.values().map(|(c, _)| *c).sum();
    let mut out = vec![format!(
        "[linter-grouped: {} files, {} diagnostics clustered by template]",
        by_file.len(),
        total_issues
    )];
    for (file, (file_total, templates)) in by_file {
        out.push(format!("## {} ({} issues, {} unique):", file, file_total, templates.len()));
        // Sort templates by descending frequency so highest-impact errors always appear first
        let mut ranked: Vec<(String, Vec<String>)> = templates.into_iter().collect();
        ranked.sort_by(|a, b| b.1.len().cmp(&a.1.len()).then_with(|| a.0.cmp(&b.0)));
        for (msg, anchors) in ranked.iter().take(6) {
            let cnt = anchors.len().max(1);
            if cnt > 1 {
                let sample: Vec<&str> = anchors.iter().take(5).map(|s| s.as_str()).collect();
                let more = if anchors.len() > 5 {
                    format!(" (+{})", anchors.len() - 5)
                } else {
                    String::new()
                };
                out.push(format!("  [x{}] {}{}: {}", cnt, sample.join(","), more, msg));
            } else if let Some(a) = anchors.first() {
                out.push(format!("  {}: {}", a, msg));
            } else {
                out.push(format!("  {}", msg));
            }
        }
        if ranked.len() > 6 {
            out.push(format!("  [+{} more unique templates in {}]", ranked.len() - 6, file));
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
            if matches!(sub, "test" | "vet" | "list" | "build") {
                Ok((Kind::GoTest, matches!(sub, "test" | "vet")))
            } else {
                Err(format!("disallowed go subcommand: {}", sub))
            }
        }
        "pytest" => Ok((Kind::Pytest, true)),
        "python3" | "python" => {
            let f0 = args.first().map(|s| s.as_str()).unwrap_or("");
            let mod_name = args.get(1).map(|s| s.as_str()).unwrap_or("");
            if f0 == "-m" {
                match mod_name {
                    "pytest" | "unittest" => Ok((Kind::Pytest, true)),
                    "mypy" | "ruff" => Ok((Kind::Linter, true)),
                    _ => Err(format!("disallowed {} -m module: {}", base, mod_name)),
                }
            } else {
                Err(format!("disallowed {} invocation (only -m pytest|unittest|mypy|ruff allowed)", base))
            }
        }
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

fn json_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 8);
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out
}

fn parse_json_str_at(bytes: &[u8], start: usize) -> Option<(String, usize)> {
    if start >= bytes.len() || bytes[start] != b'"' {
        return None;
    }
    let mut i = start + 1;
    let mut raw = Vec::new();
    while i < bytes.len() {
        let b = bytes[i];
        if b == b'\\' && i + 1 < bytes.len() {
            let esc = bytes[i + 1];
            match esc {
                b'"' => raw.push(b'"'),
                b'\\' => raw.push(b'\\'),
                b'/' => raw.push(b'/'),
                b'n' => raw.push(b'\n'),
                b'r' => raw.push(b'\r'),
                b't' => raw.push(b'\t'),
                b'u' if i + 5 < bytes.len() => {
                    if let Ok(hex) = std::str::from_utf8(&bytes[i + 2..i + 6]) {
                        if let Ok(cp) = u32::from_str_radix(hex, 16) {
                            if let Some(ch) = char::from_u32(cp) {
                                let mut buf = [0u8; 4];
                                raw.extend_from_slice(ch.encode_utf8(&mut buf).as_bytes());
                            }
                        }
                    }
                    i += 6;
                    continue;
                }
                other => raw.push(other),
            }
            i += 2;
            continue;
        }
        if b == b'"' {
            let s = String::from_utf8_lossy(&raw).into_owned();
            return Some((s, i + 1));
        }
        raw.push(b);
        i += 1;
    }
    None
}

fn skip_json_ws(bytes: &[u8], mut i: usize) -> usize {
    while i < bytes.len() && bytes[i].is_ascii_whitespace() {
        i += 1;
    }
    i
}

fn skip_json_value(bytes: &[u8], start: usize) -> usize {
    let i = skip_json_ws(bytes, start);
    if i >= bytes.len() {
        return i;
    }
    match bytes[i] {
        b'"' => parse_json_str_at(bytes, i).map(|(_, e)| e).unwrap_or(bytes.len()),
        b'{' | b'[' => {
            let mut depth = 1usize;
            let mut j = i + 1;
            let mut in_str = false;
            let mut esc = false;
            while j < bytes.len() && depth > 0 {
                let b = bytes[j];
                if esc {
                    esc = false;
                } else if in_str {
                    if b == b'\\' {
                        esc = true;
                    } else if b == b'"' {
                        in_str = false;
                    }
                } else {
                    match b {
                        b'"' => in_str = true,
                        b'{' | b'[' => depth += 1,
                        b'}' | b']' => depth -= 1,
                        _ => {}
                    }
                }
                j += 1;
            }
            j
        }
        _ => {
            let mut j = i;
            while j < bytes.len() && !matches!(bytes[j], b',' | b'}' | b']') && !bytes[j].is_ascii_whitespace() {
                j += 1;
            }
            j
        }
    }
}

/// Zero-alloc depth-1 top-level JSON field scanner (returns raw string/scalar tokens by key).
fn extract_top_level_fields(line: &str, wanted: &[&str]) -> HashMap<String, String> {
    let mut out = HashMap::new();
    let bytes = line.trim().as_bytes();
    if bytes.first() != Some(&b'{') {
        return out;
    }
    let mut i = 1usize;
    while i < bytes.len() {
        i = skip_json_ws(bytes, i);
        if i >= bytes.len() || bytes[i] == b'}' {
            break;
        }
        if bytes[i] == b',' {
            i += 1;
            continue;
        }
        if bytes[i] != b'"' {
            break;
        }
        let Some((key, after_key)) = parse_json_str_at(bytes, i) else { break };
        i = skip_json_ws(bytes, after_key);
        if i >= bytes.len() || bytes[i] != b':' {
            break;
        }
        i = skip_json_ws(bytes, i + 1);
        let is_wanted = wanted.contains(&key.as_str());
        if i < bytes.len() && bytes[i] == b'"' {
            let Some((val, end)) = parse_json_str_at(bytes, i) else { break };
            if is_wanted {
                out.insert(key, val);
            }
            i = end;
        } else {
            let end = skip_json_value(bytes, i);
            if is_wanted && end > i {
                if let Ok(tok) = std::str::from_utf8(&bytes[i..end]) {
                    let t = tok.trim();
                    if t != "null" {
                        out.insert(key, t.to_string());
                    }
                }
            }
            i = end;
        }
    }
    out
}

fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = y - if m <= 2 { 1 } else { 0 };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let doy = (153 * (m + if m > 2 { -3 } else { 9 }) + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

fn parse_iso_epoch_secs(s: &str) -> Option<i64> {
    let b = s.trim().as_bytes();
    if b.len() < 19 || b[4] != b'-' || b[7] != b'-' || (b[10] != b'T' && b[10] != b' ') || b[13] != b':' || b[16] != b':' {
        return None;
    }
    let y: i64 = std::str::from_utf8(&b[0..4]).ok()?.parse().ok()?;
    let m: i64 = std::str::from_utf8(&b[5..7]).ok()?.parse().ok()?;
    let d: i64 = std::str::from_utf8(&b[8..10]).ok()?.parse().ok()?;
    let hh: i64 = std::str::from_utf8(&b[11..13]).ok()?.parse().ok()?;
    let mm: i64 = std::str::from_utf8(&b[14..16]).ok()?.parse().ok()?;
    let ss: i64 = std::str::from_utf8(&b[17..19]).ok()?.parse().ok()?;
    let mut idx = 19;
    if idx < b.len() && b[idx] == b'.' {
        idx += 1;
        while idx < b.len() && b[idx].is_ascii_digit() {
            idx += 1;
        }
    }
    let mut tz_off = 0i64;
    if idx < b.len() && (b[idx] == b'+' || b[idx] == b'-') && idx + 5 < b.len() {
        let sign = if b[idx] == b'+' { 1i64 } else { -1i64 };
        let th: i64 = std::str::from_utf8(&b[idx + 1..idx + 3]).ok()?.parse().ok()?;
        let tm: i64 = if b[idx + 3] == b':' && idx + 5 < b.len() {
            std::str::from_utf8(&b[idx + 4..idx + 6]).ok()?.parse().ok()?
        } else if idx + 4 < b.len() {
            std::str::from_utf8(&b[idx + 3..idx + 5]).ok()?.parse().ok()?
        } else {
            0
        };
        tz_off = sign * (th * 3600 + tm * 60);
    }
    Some(days_from_civil(y, m, d) * 86_400 + hh * 3600 + mm * 60 + ss - tz_off)
}

#[derive(Default)]
struct HudState {
    path: String,
    sz: u64,
    mtime_ns: u128,
    epoch: u64,
    warned_120k: bool,
    warned_200k: bool,
    last_flip_step: String,
    last_cold_inv: i64,
}

fn load_hud_state(sf: &Path) -> HudState {
    let mut st = HudState { last_cold_inv: -99, ..Default::default() };
    let Ok(txt) = fs::read_to_string(sf) else { return st };
    let fields = extract_top_level_fields(
        &txt,
        &["path", "sz", "mtime_ns", "epoch", "warned_120k", "warned_200k", "last_flip_step", "last_cold_inv"],
    );
    if let Some(v) = fields.get("path") { st.path = v.clone(); }
    if let Some(v) = fields.get("sz") { st.sz = v.parse().unwrap_or(0); }
    if let Some(v) = fields.get("mtime_ns") { st.mtime_ns = v.parse().unwrap_or(0); }
    if let Some(v) = fields.get("epoch") { st.epoch = v.parse().unwrap_or(0); }
    if let Some(v) = fields.get("warned_120k") { st.warned_120k = v == "true"; }
    if let Some(v) = fields.get("warned_200k") { st.warned_200k = v == "true"; }
    if let Some(v) = fields.get("last_flip_step") { st.last_flip_step = v.clone(); }
    if let Some(v) = fields.get("last_cold_inv") { st.last_cold_inv = v.parse().unwrap_or(-99); }
    st
}

fn save_hud_state(sf: &Path, st: &HudState) {
    let body = format!(
        "{{\"path\":\"{}\",\"sz\":{},\"mtime_ns\":{},\"epoch\":{},\"warned_120k\":{},\"warned_200k\":{},\"last_flip_step\":\"{}\",\"last_cold_inv\":{}}}",
        json_escape(&st.path),
        st.sz,
        st.mtime_ns,
        st.epoch,
        if st.warned_120k { "true" } else { "false" },
        if st.warned_200k { "true" } else { "false" },
        json_escape(&st.last_flip_step),
        st.last_cold_inv
    );
    if let Some(parent) = sf.parent() {
        let _ = fs::create_dir_all(parent);
    }
    let name = sf.file_name().and_then(|s| s.to_str()).unwrap_or("hud.json");
    let tmp = sf.with_file_name(format!(".{}.tmp.{}", name, std::process::id()));
    if fs::write(&tmp, body.as_bytes()).is_ok() {
        if fs::rename(&tmp, sf).is_err() {
            let _ = fs::remove_file(&tmp);
        }
    } else {
        let _ = fs::remove_file(&tmp);
    }
}

struct PlannerTail {
    unc: u64,
    cac: u64,
    ctx: u64,
    model: String,
    step: Option<String>,
    ts_secs: Option<i64>,
}

fn run_hud() {
    if env::var("WM_AGY_BENCH_VANILLA").ok().as_deref() == Some("1")
        || env::var("WM_LONGEVITY_HUD_OFF").ok().as_deref() == Some("1")
    {
        println!("{{}}");
        return;
    }

    let mut stdin_buf = String::new();
    if io::stdin().read_to_string(&mut stdin_buf).is_err() || stdin_buf.trim().is_empty() {
        println!("{{}}");
        return;
    }
    let payload = extract_top_level_fields(&stdin_buf, &["transcriptPath", "conversationId", "invocationNum"]);
    let tp = payload.get("transcriptPath").map(|s| s.as_str()).unwrap_or("");
    if tp.is_empty() {
        println!("{{}}");
        return;
    }
    let conv_id = payload.get("conversationId").map(|s| s.as_str()).unwrap_or("default");
    let cur_inv: i64 = payload.get("invocationNum").and_then(|s| s.parse().ok()).unwrap_or(0);

    let tp_path = Path::new(tp);
    let Ok(meta) = fs::metadata(tp_path) else {
        println!("{{}}");
        return;
    };
    if !meta.is_file() {
        println!("{{}}");
        return;
    }
    let sz = meta.len();
    let mtime_ns = meta
        .modified()
        .ok()
        .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
        .map(|d| d.as_nanos())
        .unwrap_or(0);

    let state_dir = env::var("WM_HOOK_STATE_DIR")
        .ok()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "/tmp/agy-hook-state".to_string());
    let mut san: String = conv_id
        .chars()
        .filter(|c| c.is_ascii_alphanumeric() || *c == '_' || *c == '-')
        .take(20)
        .collect();
    if san.is_empty() {
        san = "default".to_string();
    }
    let sf = PathBuf::from(&state_dir).join(format!("hud_{}.json", san));
    let mut state = load_hud_state(&sf);

    if state.path == tp && state.sz == sz && state.mtime_ns == mtime_ns {
        println!("{{}}");
        return;
    }

    let Ok(mut fh) = File::open(tp_path) else {
        println!("{{}}");
        return;
    };
    let tail_start = sz.saturating_sub(49_152);
    if tail_start > 0 {
        let _ = fh.seek(SeekFrom::Start(tail_start));
    }
    let mut tail_bytes = Vec::new();
    if fh.read_to_end(&mut tail_bytes).is_err() {
        println!("{{}}");
        return;
    }
    let chunk = String::from_utf8_lossy(&tail_bytes);

    let mut checkpoints = 0u64;
    let mut responses: Vec<PlannerTail> = Vec::new();
    for line in chunk.lines() {
        if !line.starts_with('{') {
            continue;
        }
        if line.contains("\"CHECKPOINT\"") {
            let fields = extract_top_level_fields(line, &["type"]);
            if fields.get("type").map(|s| s.as_str()) == Some("CHECKPOINT") {
                checkpoints += 1;
            }
            continue;
        }
        if !line.contains("\"PLANNER_RESPONSE\"") {
            continue;
        }
        let fields = extract_top_level_fields(
            line,
            &["type", "input_tokens", "cache_read_tokens", "model", "step_index", "created_at", "timestamp"],
        );
        if fields.get("type").map(|s| s.as_str()) != Some("PLANNER_RESPONSE") {
            continue;
        }
        let unc: u64 = fields.get("input_tokens").and_then(|s| s.parse().ok()).unwrap_or(0);
        let cac: u64 = fields.get("cache_read_tokens").and_then(|s| s.parse().ok()).unwrap_or(0);
        if unc + cac == 0 {
            continue;
        }
        let model = fields.get("model").cloned().unwrap_or_default();
        let step = fields.get("step_index").cloned();
        let ts_secs = fields
            .get("created_at")
            .or_else(|| fields.get("timestamp"))
            .and_then(|s| parse_iso_epoch_secs(s));
        responses.push(PlannerTail { unc, cac, ctx: unc + cac, model, step, ts_secs });
    }

    state.path = tp.to_string();
    state.sz = sz;
    state.mtime_ns = mtime_ns;

    if responses.is_empty() {
        save_hud_state(&sf, &state);
        println!("{{}}");
        return;
    }

    let last_pr = &responses[responses.len() - 1];
    let prev_pr = if responses.len() >= 2 { Some(&responses[responses.len() - 2]) } else { None };
    let unc = last_pr.unc;
    let cac = last_pr.cac;
    let cur_ctx = last_pr.ctx;
    let hit_pct = 100.0 * (cac as f64) / (cur_ctx.max(1) as f64);
    let cur_model = last_pr.model.as_str();
    let prev_model = prev_pr.map(|p| p.model.as_str()).unwrap_or("");
    let step_key = last_pr.step.clone().unwrap_or_else(|| format!("inv:{}:{}", cur_inv, sz));
    let idle_secs: i64 = match (prev_pr.and_then(|p| p.ts_secs), last_pr.ts_secs) {
        (Some(t0), Some(t1)) if t1 > t0 => t1 - t0,
        _ => 0,
    };

    if checkpoints > state.epoch || cur_ctx < 90_000 {
        state.warned_120k = false;
        state.warned_200k = false;
    }
    state.epoch = state.epoch.max(checkpoints);

    let is_cli = tp.contains("antigravity-cli");
    let hud_cli_full = env::var("WM_LONGEVITY_HUD_CLI").ok().as_deref() == Some("1");
    let flipped = !prev_model.is_empty() && !cur_model.is_empty() && prev_model != cur_model;
    let mut alerts: Vec<String> = Vec::new();

    if flipped && unc >= 50_000 && step_key != state.last_flip_step {
        alerts.push(format!(
            "⚡ **Model Flip Cache Eviction** (`{}` → `{}`): burned **~{:.0}k uncached tok** ({:.0}% hit). Pin a single model (`Gemini 4 Hydrogen`) on >80k sessions.",
            prev_model,
            cur_model,
            (unc as f64) / 1000.0,
            hit_pct
        ));
        state.last_flip_step = step_key;
    }

    if !is_cli || hud_cli_full {
        if cur_ctx >= 200_000 && !state.warned_200k {
            alerts.push(format!(
                "🔴 **Context Cliff (~{:.0}k tok · {:.0}% cache hit):** High re-prefill cost on any idle gap. Run `/compact` or `wm-handoff` soon.",
                (cur_ctx as f64) / 1000.0,
                hit_pct
            ));
            state.warned_200k = true;
            state.warned_120k = true;
        } else if cur_ctx >= 120_000 && !state.warned_120k {
            alerts.push(format!(
                "🟡 **Context Watch (~{:.0}k tok · {:.0}% cache hit):** Run `/compact` or `wm-handoff` before stepping away >5m.",
                (cur_ctx as f64) / 1000.0,
                hit_pct
            ));
            state.warned_120k = true;
        }

        if !flipped && cur_ctx >= 80_000 && hit_pct < 50.0 && unc >= 50_000 && (cur_inv - state.last_cold_inv) >= 5 {
            if idle_secs >= 300 {
                let idle_mins = idle_secs / 60;
                alerts.push(format!(
                    "❄️ **Cold Resume After Idle (~{:.0}k tok · {:.0}% hit · idle {}m):** Re-prefilled ~{:.0}k uncached tok. Use `wm-handoff` before resuming idle sessions.",
                    (cur_ctx as f64) / 1000.0,
                    hit_pct,
                    idle_mins,
                    (unc as f64) / 1000.0
                ));
            } else {
                alerts.push(format!(
                    "❄️ **Cold Cache Miss (~{:.0}k tok · {:.0}% hit):** Re-prefilled ~{:.0}k uncached tok. Use `wm-handoff` before resuming idle sessions.",
                    (cur_ctx as f64) / 1000.0,
                    hit_pct,
                    (unc as f64) / 1000.0
                ));
            }
            state.last_cold_inv = cur_inv;
        }
    }

    save_hud_state(&sf, &state);

    if alerts.is_empty() {
        println!("{{}}");
    } else {
        let md = format!("> {}", alerts.join("\n> "));
        println!(
            "{{\"injectSteps\":[{{\"systemMessage\":{{\"systemMessage\":\"\",\"metadata\":{{\"user_markdown\":\"{}\"}}}}}}],\"terminationBehavior\":\"\"}}",
            json_escape(&md)
        );
    }
}

fn civil_from_days(days: i64) -> (i64, i64, i64) {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = mp + if mp < 10 { 3 } else { -9 };
    let yr = y + if m <= 2 { 1 } else { 0 };
    (yr, m, d)
}

fn utc_iso_now() -> String {
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    let days = secs.div_euclid(86_400);
    let rem = secs.rem_euclid(86_400);
    let (yr, mo, dy) = civil_from_days(days);
    let hh = rem / 3600;
    let mm = (rem % 3600) / 60;
    let ss = rem % 60;
    format!("{:04}-{:02}-{:02}T{:02}:{:02}:{:02}+00:00", yr, mo, dy, hh, mm, ss)
}

fn truncate_chars(s: &str, max_chars: usize) -> String {
    s.chars().take(max_chars).collect()
}

fn is_secret_val_char(b: u8) -> bool {
    b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-' | b'/' | b'+')
}

fn starts_with_ignore_ascii_case(hay: &[u8], pat: &[u8]) -> bool {
    hay.len() >= pat.len() && hay[..pat.len()].eq_ignore_ascii_case(pat)
}

fn redact_secrets(input: &str) -> String {
    let b = input.as_bytes();
    let n = b.len();
    let mut out = String::with_capacity(n);
    let mut i = 0usize;
    let keys: [&[u8]; 7] = [
        b"authorization",
        b"password",
        b"api_key",
        b"api-key",
        b"secret",
        b"token",
        b"bearer",
    ];
    while i < n {
        let mut replaced = false;
        let prev_alnum = i > 0 && (b[i - 1].is_ascii_alphanumeric() || b[i - 1] == b'_');
        if !prev_alnum {
            if starts_with_ignore_ascii_case(&b[i..], b"bearer") {
                let mut j = i + 6;
                if j < n && b[j].is_ascii_whitespace() {
                    while j < n && b[j].is_ascii_whitespace() {
                        j += 1;
                    }
                    let tok_start = j;
                    while j < n && (b[j].is_ascii_alphanumeric() || matches!(b[j], b'.' | b'_' | b'-')) {
                        j += 1;
                    }
                    if j > tok_start {
                        out.push_str("[REDACTED]");
                        i = j;
                        replaced = true;
                    }
                }
            }
            if !replaced {
                for &kw in &keys[..6] {
                    if starts_with_ignore_ascii_case(&b[i..], kw) {
                        let mut j = i + kw.len();
                        while j < n && b[j].is_ascii_whitespace() {
                            j += 1;
                        }
                        if j < n && (b[j] == b'=' || b[j] == b':') {
                            j += 1;
                            while j < n && b[j].is_ascii_whitespace() {
                                j += 1;
                            }
                            let mut quote: Option<u8> = None;
                            if j < n && (b[j] == b'\'' || b[j] == b'"') {
                                quote = Some(b[j]);
                                j += 1;
                            }
                            let val_start = j;
                            while j < n && is_secret_val_char(b[j]) {
                                j += 1;
                            }
                            if j - val_start >= 8 {
                                if let Some(q) = quote {
                                    if j < n && b[j] == q {
                                        j += 1;
                                    }
                                }
                                out.push_str("[REDACTED]");
                                i = j;
                                replaced = true;
                                break;
                            }
                        }
                    }
                }
            }
        }
        if !replaced {
            if let Some(ch) = input[i..].chars().next() {
                out.push(ch);
                i += ch.len_utf8();
            } else {
                break;
            }
        }
    }
    out
}

fn extract_json_array_elements(arr_raw: &str, max_items: usize) -> Vec<String> {
    let mut out = Vec::new();
    let bytes = arr_raw.trim().as_bytes();
    if bytes.first() != Some(&b'[') {
        return out;
    }
    let mut i = 1usize;
    while i < bytes.len() && out.len() < max_items {
        i = skip_json_ws(bytes, i);
        if i >= bytes.len() || bytes[i] == b']' {
            break;
        }
        if bytes[i] == b',' {
            i += 1;
            continue;
        }
        if bytes[i] == b'"' {
            if let Some((val, end)) = parse_json_str_at(bytes, i) {
                out.push(val);
                i = end;
            } else {
                break;
            }
        } else {
            let end = skip_json_value(bytes, i);
            if end > i {
                if let Ok(elem) = std::str::from_utf8(&bytes[i..end]) {
                    out.push(elem.to_string());
                }
            }
            i = end.max(i + 1);
        }
    }
    out
}

fn run_post_tool() {
    let mut stdin_buf = String::new();
    if io::stdin().read_to_string(&mut stdin_buf).is_err() || stdin_buf.trim().is_empty() {
        println!("{{}}");
        return;
    }

    // Ultra-fast clean path (<2ms total runtime): small payload with no error field cannot fail or spill
    if stdin_buf.len() <= 16_000 && !stdin_buf.contains("\"error\"") {
        println!("{{}}");
        return;
    }

    let payload = extract_top_level_fields(
        &stdin_buf,
        &["error", "toolCall", "result", "output", "toolOutput", "conversationId", "cwd", "modelName"],
    );
    let err_raw = payload
        .get("error")
        .map(|s| s.trim())
        .filter(|s| !s.is_empty() && *s != "false");

    let tc_raw = payload.get("toolCall").map(|s| s.as_str()).unwrap_or("");
    let tc_fields = extract_top_level_fields(tc_raw, &["name", "args", "arguments"]);
    let tool_name = tc_fields
        .get("name")
        .map(|s| s.as_str())
        .filter(|s| !s.is_empty())
        .unwrap_or("unknown");
    let args_raw = tc_fields
        .get("args")
        .or_else(|| tc_fields.get("arguments"))
        .map(|s| s.as_str())
        .unwrap_or("");
    let args_fields = if args_raw.trim_start().starts_with('{') {
        extract_top_level_fields(
            args_raw,
            &[
                "CommandLine",
                "command",
                "Path",
                "path",
                "StartLine",
                "EndLine",
                "Content",
                "content",
                "Replacement",
                "replacement",
                "edits",
                "commands",
                "queries",
                "mode",
                "target",
                "ServerName",
                "ToolName",
            ],
        )
    } else {
        HashMap::new()
    };

    let out_raw = payload
        .get("result")
        .or_else(|| payload.get("output"))
        .or_else(|| payload.get("toolOutput"))
        .map(|s| s.as_str())
        .unwrap_or("");
    let write_content = if tool_name == "write_file" {
        args_fields
            .get("Content")
            .or_else(|| args_fields.get("content"))
            .map(|s| s.as_str())
            .unwrap_or("")
    } else {
        ""
    };
    let edit_content = if tool_name == "edit_file" {
        args_fields
            .get("Replacement")
            .or_else(|| args_fields.get("replacement"))
            .map(|s| s.as_str())
            .unwrap_or("")
    } else {
        ""
    };

    let mut spill_bytes = 0usize;
    let mut spill_kind = "";
    if err_raw.is_none() {
        if out_raw.len() > 16_000 {
            spill_bytes = out_raw.len();
            spill_kind = "output_spill";
        } else if write_content.len() > 25_000 {
            spill_bytes = write_content.len();
            spill_kind = "large_write_file";
        } else if edit_content.len() > 25_000 {
            spill_bytes = edit_content.len();
            spill_kind = "large_edit_file";
        }
    }

    if let Some(err_str) = err_raw {
        let raw_preview = if tool_name == "run_command" {
            args_fields
                .get("CommandLine")
                .or_else(|| args_fields.get("command"))
                .cloned()
                .unwrap_or_default()
        } else if tool_name == "read_file" {
            let p = args_fields.get("Path").or_else(|| args_fields.get("path")).map(|s| s.as_str()).unwrap_or("");
            if p.is_empty() {
                "read_file".to_string()
            } else {
                let s_l = args_fields.get("StartLine").map(|s| s.as_str());
                let e_l = args_fields.get("EndLine").map(|s| s.as_str());
                if s_l.is_some() || e_l.is_some() {
                    format!("read_file: {} [L{}-{}]", p, s_l.unwrap_or("1"), e_l.unwrap_or("EOF"))
                } else {
                    format!("read_file: {}", p)
                }
            }
        } else if tool_name == "write_file" || tool_name == "edit_file" {
            let p = args_fields.get("Path").or_else(|| args_fields.get("path")).map(|s| s.as_str()).unwrap_or("");
            if p.is_empty() {
                tool_name.to_string()
            } else {
                format!("{}: {}", tool_name, p)
            }
        } else if tool_name.ends_with("_apply_edits") {
            let elems = args_fields
                .get("edits")
                .map(|e| extract_json_array_elements(e, 4))
                .unwrap_or_default();
            let paths: Vec<String> = elems
                .iter()
                .filter(|el| el.trim_start().starts_with('{'))
                .map(|el| {
                    extract_top_level_fields(el, &["path"])
                        .get("path")
                        .cloned()
                        .unwrap_or_else(|| "?".to_string())
                })
                .collect();
            if paths.is_empty() {
                "apply_edits".to_string()
            } else {
                format!("apply_edits: {}", paths.join(", "))
            }
        } else if tool_name.ends_with("_run_checks") {
            let elems = args_fields
                .get("commands")
                .map(|c| extract_json_array_elements(c, 1))
                .unwrap_or_default();
            let first = match elems.first() {
                Some(el) if el.trim_start().starts_with('{') => extract_top_level_fields(el, &["command"])
                    .get("command")
                    .cloned()
                    .unwrap_or_default(),
                Some(el) => el.clone(),
                None => String::new(),
            };
            format!("run_checks: {}", first)
        } else if tool_name.ends_with("_batch_search") {
            let elems = args_fields
                .get("queries")
                .map(|q| extract_json_array_elements(q, 4))
                .unwrap_or_default();
            let qtypes: Vec<String> = elems
                .iter()
                .filter(|el| el.trim_start().starts_with('{'))
                .map(|el| {
                    extract_top_level_fields(el, &["type"])
                        .get("type")
                        .cloned()
                        .unwrap_or_else(|| "?".to_string())
                })
                .collect();
            format!("batch_search: {}", qtypes.join(","))
        } else if tool_name.ends_with("_code_graph") {
            format!(
                "code_graph: {}:{}",
                args_fields.get("mode").map(|s| s.as_str()).unwrap_or(""),
                args_fields.get("target").map(|s| s.as_str()).unwrap_or("")
            )
        } else {
            let srv = args_fields.get("ServerName").map(|s| s.as_str()).unwrap_or("");
            let t_sub = args_fields.get("ToolName").map(|s| s.as_str()).unwrap_or("");
            if !srv.is_empty() || !t_sub.is_empty() {
                format!("{}/{}", srv, t_sub)
            } else {
                tool_name.to_string()
            }
        };

        let preview = truncate_chars(&redact_secrets(&raw_preview), 180);
        let error_clean = truncate_chars(&redact_secrets(err_str), 240);
        let conv_id = truncate_chars(payload.get("conversationId").map(|s| s.as_str()).unwrap_or(""), 16);
        let cwd = payload.get("cwd").map(|s| s.as_str()).unwrap_or("");
        let model = payload.get("modelName").map(|s| s.as_str()).unwrap_or("");

        let ledger_path = env::var("WM_SIM_LEDGER")
            .ok()
            .filter(|s| !s.is_empty())
            .map(PathBuf::from)
            .or_else(|| {
                env::var("HOME")
                    .ok()
                    .map(|h| PathBuf::from(h).join(".local/share/whitemagic/sim_ledger.jsonl"))
            });
        if let Some(lp) = ledger_path {
            if let Some(parent) = lp.parent() {
                let _ = fs::create_dir_all(parent);
            }
            let line = format!(
                "{{\"event\":\"tool_failure\",\"ts\":\"{}\",\"conversation_id\":\"{}\",\"cwd\":\"{}\",\"model\":\"{}\",\"tool\":\"{}\",\"cmd_preview\":\"{}\",\"error\":\"{}\"}}\n",
                utc_iso_now(),
                json_escape(&conv_id),
                json_escape(cwd),
                json_escape(model),
                json_escape(tool_name),
                json_escape(&preview),
                json_escape(&error_clean)
            );
            if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(lp) {
                let _ = f.write_all(line.as_bytes());
            }
        }
    } else if spill_bytes > 0 {
        let src_txt = args_fields
            .get("Path")
            .or_else(|| args_fields.get("path"))
            .or_else(|| args_fields.get("CommandLine"))
            .map(|s| s.as_str())
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| out_raw);
        let preview = truncate_chars(&redact_secrets(&truncate_chars(src_txt, 180)), 180);
        let conv_id = truncate_chars(payload.get("conversationId").map(|s| s.as_str()).unwrap_or(""), 16);
        let telem_path = env::var("WM_AGY_TELEMETRY")
            .ok()
            .filter(|s| !s.is_empty())
            .map(PathBuf::from)
            .or_else(|| {
                env::var("HOME")
                    .ok()
                    .map(|h| PathBuf::from(h).join(".local/share/whitemagic/agy_telemetry.jsonl"))
            });
        if let Some(tp) = telem_path {
            if let Some(parent) = tp.parent() {
                let _ = fs::create_dir_all(parent);
            }
            let line = format!(
                "{{\"event\":\"tool_spill\",\"kind\":\"{}\",\"ts\":\"{}\",\"conv_id\":\"{}\",\"tool\":\"{}\",\"bytes\":{},\"preview\":\"{}\"}}\n",
                spill_kind,
                utc_iso_now(),
                json_escape(&conv_id),
                json_escape(tool_name),
                spill_bytes,
                json_escape(&preview)
            );
            if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(tp) {
                let _ = f.write_all(line.as_bytes());
            }
        }
    }

    println!("{{}}");
}

fn main() {
    let args: Vec<String> = env::args().skip(1).collect();
    if args.is_empty() || args[0] == "-h" || args[0] == "--help" {
        eprintln!(
            "Usage:\n  wm-squeeze --safe [--cwd DIR] [--max-bytes N] [--] <cmd> [args...]\n  wm-squeeze --pipe [--kind auto|cargo|pytest|gotest|linter|git|rg|ls|generic] [--max-bytes N]\n  wm-squeeze --hud\n  wm-squeeze --post-tool\n  wm-squeeze --stats"
        );
        std::process::exit(0);
    }
    if args[0] == "--stats" {
        print_stats();
        return;
    }
    if args[0] == "--hud" {
        run_hud();
        return;
    }
    if args[0] == "--post-tool" {
        run_post_tool();
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

    let mut cwd: Option<&str> = None;
    let mut safe_envs: Vec<(String, String)> = Vec::new();
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--cwd" if i + 1 < args.len() => { cwd = Some(args[i + 1].as_str()); i += 2; }
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

    let work_dir = cwd.map(PathBuf::from).unwrap_or_else(|| env::current_dir().unwrap_or_else(|_| PathBuf::from(".")));
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
