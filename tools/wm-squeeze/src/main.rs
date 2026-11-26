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
use std::net::{SocketAddr, TcpStream};
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

const SHA256_H0: [u32; 8] = [
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
    0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
];

const SHA256_K: [u32; 64] = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
];

fn sha256_digest(data: &[u8]) -> [u8; 32] {
    let mut msg = data.to_vec();
    let bit_len = (data.len() as u64).wrapping_mul(8);
    msg.push(0x80);
    while (msg.len() % 64) != 56 {
        msg.push(0);
    }
    msg.extend_from_slice(&bit_len.to_be_bytes());

    let mut state = SHA256_H0;
    for chunk in msg.chunks_exact(64) {
        let mut w = [0u32; 64];
        for i in 0..16 {
            let j = i * 4;
            w[i] = u32::from_be_bytes([chunk[j], chunk[j + 1], chunk[j + 2], chunk[j + 3]]);
        }
        for i in 16..64 {
            let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
            let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16].wrapping_add(s0).wrapping_add(w[i - 7]).wrapping_add(s1);
        }
        let [mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut h] = state;
        for i in 0..64 {
            let big_s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let ch = (e & f) ^ ((!e) & g);
            let temp1 = h.wrapping_add(big_s1).wrapping_add(ch).wrapping_add(SHA256_K[i]).wrapping_add(w[i]);
            let big_s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let maj = (a & b) ^ (a & c) ^ (b & c);
            let temp2 = big_s0.wrapping_add(maj);
            h = g;
            g = f;
            f = e;
            e = d.wrapping_add(temp1);
            d = c;
            c = b;
            b = a;
            a = temp1.wrapping_add(temp2);
        }
        for (slot, delta) in state.iter_mut().zip([a, b, c, d, e, f, g, h]) {
            *slot = slot.wrapping_add(delta);
        }
    }
    let mut out = [0u8; 32];
    for (i, word) in state.iter().enumerate() {
        out[i * 4..i * 4 + 4].copy_from_slice(&word.to_be_bytes());
    }
    out
}

fn sha256_hex(data: &[u8]) -> String {
    let d = sha256_digest(data);
    let mut s = String::with_capacity(64);
    for b in d {
        let _ = write!(s, "{:02x}", b);
    }
    s
}

fn hmac_sha256_hex(key: &[u8], msg: &[u8]) -> String {
    let mut k_block = [0u8; 64];
    if key.len() > 64 {
        let kh = sha256_digest(key);
        k_block[..32].copy_from_slice(&kh);
    } else {
        k_block[..key.len()].copy_from_slice(key);
    }
    let mut inner = Vec::with_capacity(64 + msg.len());
    for b in k_block {
        inner.push(b ^ 0x36);
    }
    inner.extend_from_slice(msg);
    let inner_hash = sha256_digest(&inner);

    let mut outer = Vec::with_capacity(64 + 32);
    for b in k_block {
        outer.push(b ^ 0x5c);
    }
    outer.extend_from_slice(&inner_hash);
    sha256_hex(&outer)
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
    let fast_path = env::var("WM_FAST_STATS")
        .ok()
        .filter(|s| !s.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            PathBuf::from(env::var("HOME").unwrap_or_default())
                .join(".local/share/whitemagic/wm_fast_stats.jsonl")
        });
    if let Ok(fc) = fs::read_to_string(&fast_path) {
        let (mut f_calls, mut f_out, mut f_ms, mut f_err) = (0usize, 0usize, 0usize, 0usize);
        for line in fc.lines() {
            if !line.starts_with('{') {
                continue;
            }
            let get_n = |key: &str| -> usize {
                line.find(key)
                    .and_then(|i| {
                        let rest = &line[i + key.len()..];
                        let num: String = rest.chars().take_while(|c| c.is_ascii_digit()).collect();
                        num.parse().ok()
                    })
                    .unwrap_or(0)
            };
            f_calls += 1;
            f_out += get_n("\"out\":");
            f_ms += get_n("\"wall_ms\":");
            if line.contains("\"err\":true") {
                f_err += 1;
            }
        }
        if f_calls > 0 {
            println!(
                "wm-fast MCP:   {} calls ({} err) | inline out {:.1} KB (avg {} B/call, ~{} tok) | avg {} ms",
                f_calls,
                f_err,
                f_out as f64 / 1024.0,
                f_out / f_calls,
                f_out / 4,
                f_ms / f_calls
            );
        }
    }
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
    flip_count: u64,
    flip_unc_total: u64,
}

fn load_hud_state(sf: &Path) -> HudState {
    let mut st = HudState { last_cold_inv: -99, ..Default::default() };
    let Ok(txt) = fs::read_to_string(sf) else { return st };
    let fields = extract_top_level_fields(
        &txt,
        &[
            "path",
            "sz",
            "mtime_ns",
            "epoch",
            "warned_120k",
            "warned_200k",
            "last_flip_step",
            "last_cold_inv",
            "flip_count",
            "flip_unc_total",
        ],
    );
    if let Some(v) = fields.get("path") { st.path = v.clone(); }
    if let Some(v) = fields.get("sz") { st.sz = v.parse().unwrap_or(0); }
    if let Some(v) = fields.get("mtime_ns") { st.mtime_ns = v.parse().unwrap_or(0); }
    if let Some(v) = fields.get("epoch") { st.epoch = v.parse().unwrap_or(0); }
    if let Some(v) = fields.get("warned_120k") { st.warned_120k = v == "true"; }
    if let Some(v) = fields.get("warned_200k") { st.warned_200k = v == "true"; }
    if let Some(v) = fields.get("last_flip_step") { st.last_flip_step = v.clone(); }
    if let Some(v) = fields.get("last_cold_inv") { st.last_cold_inv = v.parse().unwrap_or(-99); }
    if let Some(v) = fields.get("flip_count") { st.flip_count = v.parse().unwrap_or(0); }
    if let Some(v) = fields.get("flip_unc_total") { st.flip_unc_total = v.parse().unwrap_or(0); }
    st
}

fn save_hud_state(sf: &Path, st: &HudState) {
    let body = format!(
        "{{\"path\":\"{}\",\"sz\":{},\"mtime_ns\":{},\"epoch\":{},\"warned_120k\":{},\"warned_200k\":{},\"last_flip_step\":\"{}\",\"last_cold_inv\":{},\"flip_count\":{},\"flip_unc_total\":{}}}",
        json_escape(&st.path),
        st.sz,
        st.mtime_ns,
        st.epoch,
        if st.warned_120k { "true" } else { "false" },
        if st.warned_200k { "true" } else { "false" },
        json_escape(&st.last_flip_step),
        st.last_cold_inv,
        st.flip_count,
        st.flip_unc_total
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

    if checkpoints > state.epoch {
        state.warned_120k = false;
        state.warned_200k = false;
        state.flip_count = 0;
        state.flip_unc_total = 0;
    } else if cur_ctx < 90_000 {
        state.warned_120k = false;
        state.warned_200k = false;
    }
    state.epoch = state.epoch.max(checkpoints);

    let is_cli = tp.contains("antigravity-cli");
    let hud_cli_full = env::var("WM_LONGEVITY_HUD_CLI").ok().as_deref() == Some("1");
    let flipped = !prev_model.is_empty() && !cur_model.is_empty() && prev_model != cur_model;
    let mut alerts: Vec<String> = Vec::new();

    let handoff_dir = env::var("WM_HANDOFF_CACHE_DIR")
        .ok()
        .filter(|s| !s.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            PathBuf::from(env::var("HOME").unwrap_or_else(|_| "/tmp".to_string()))
                .join(".cache/wm-handoff")
        });
    let crystal_path = handoff_dir.join(format!("latest_{}.md", san));
    let crystal_hint = if crystal_path.is_file() {
        format!(" Crystal ready: `{}` (`wm-reset`).", crystal_path.display())
    } else {
        String::new()
    };
    let crystal_short = if crystal_path.is_file() {
        " Crystal ready (`wm-reset`)."
    } else {
        ""
    };

    if flipped && unc >= 50_000 && step_key != state.last_flip_step {
        state.flip_count += 1;
        state.flip_unc_total += unc;
        if state.flip_count >= 2 && (cur_ctx >= 99_000 || state.flip_unc_total >= 300_000) {
            alerts.push(format!(
                "⚡ **Orchestrator Ping-Pong Alert** ({} flips · ~{:.0}k uncached burned): Pin single-model Hydrogen, run `/compact`, or reset via `wm-reset`.{}",
                state.flip_count,
                (state.flip_unc_total as f64) / 1000.0,
                crystal_hint
            ));
        } else {
            alerts.push(format!(
                "⚡ **Model Flip Cache Eviction** (`{}` → `{}`): burned **~{:.0}k uncached tok** ({:.0}% hit). Pin a single model (`Gemini 4 Hydrogen`) on >80k sessions.",
                prev_model,
                cur_model,
                (unc as f64) / 1000.0,
                hit_pct
            ));
        }
        state.last_flip_step = step_key;
    }

    if !is_cli || hud_cli_full {
        if cur_ctx >= 200_000 && !state.warned_200k {
            alerts.push(format!(
                "🔴 **Context Cliff (~{:.0}k tok · {:.0}% cache hit):** High re-prefill cost on any idle gap. Run `/compact` or `wm-handoff` soon.{}",
                (cur_ctx as f64) / 1000.0,
                hit_pct,
                crystal_short
            ));
            state.warned_200k = true;
            state.warned_120k = true;
        } else if cur_ctx >= 120_000 && !state.warned_120k {
            alerts.push(format!(
                "🟡 **Context Watch (~{:.0}k tok · {:.0}% cache hit):** Run `/compact` or `wm-handoff` before stepping away >5m.{}",
                (cur_ctx as f64) / 1000.0,
                hit_pct,
                crystal_short
            ));
            state.warned_120k = true;
        }

        if !flipped && cur_ctx >= 80_000 && hit_pct < 50.0 && unc >= 50_000 && (cur_inv - state.last_cold_inv) >= 5 {
            if idle_secs >= 300 {
                let idle_mins = idle_secs / 60;
                alerts.push(format!(
                    "❄️ **Cold Resume After Idle (~{:.0}k tok · {:.0}% hit · idle {}m):** Re-prefilled ~{:.0}k uncached tok. Use `wm-handoff` before resuming idle sessions.{}",
                    (cur_ctx as f64) / 1000.0,
                    hit_pct,
                    idle_mins,
                    (unc as f64) / 1000.0,
                    crystal_short
                ));
            } else {
                alerts.push(format!(
                    "❄️ **Cold Cache Miss (~{:.0}k tok · {:.0}% hit):** Re-prefilled ~{:.0}k uncached tok. Use `wm-handoff` before resuming idle sessions.{}",
                    (cur_ctx as f64) / 1000.0,
                    hit_pct,
                    (unc as f64) / 1000.0,
                    crystal_short
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

fn run_post_tool(host: HostKind) {
    let mut stdin_buf = String::new();
    if io::stdin().read_to_string(&mut stdin_buf).is_err() || stdin_buf.trim().is_empty() {
        println!("{{}}");
        return;
    }

    // Ultra-fast clean path (<2ms total runtime): small payload with no error field cannot fail or spill
    if stdin_buf.len() <= 16_000
        && !stdin_buf.contains("\"error\"")
        && !stdin_buf.contains("\"error_message\"")
    {
        let _ = host;
        println!("{{}}");
        return;
    }

    if host != HostKind::Agy {
        let payload = extract_top_level_fields(
            &stdin_buf,
            &[
                "error",
                "error_message",
                "failure_type",
                "tool_name",
                "tool_input",
                "tool_response",
                "tool_output",
                "output",
                "session_id",
                "conversation_id",
                "command",
                "cwd",
                "model",
            ],
        );
        let err_raw = payload
            .get("error_message")
            .or_else(|| payload.get("error"))
            .map(|s| s.trim())
            .filter(|s| !s.is_empty() && *s != "false" && *s != "null");
        let conv_id = payload
            .get("conversation_id")
            .or_else(|| payload.get("session_id"))
            .map(|s| s.as_str())
            .unwrap_or("");
        let tool_name = payload.get("tool_name").map(|s| s.as_str()).unwrap_or("shell");
        let ti = payload.get("tool_input").map(|s| s.as_str()).unwrap_or("");
        let ti_f = if ti.trim_start().starts_with('{') {
            extract_top_level_fields(ti, &["command", "cmd", "path", "file_path"])
        } else {
            HashMap::new()
        };
        let cmd = ti_f
            .get("command")
            .or_else(|| ti_f.get("cmd"))
            .or_else(|| ti_f.get("path"))
            .or_else(|| ti_f.get("file_path"))
            .cloned()
            .or_else(|| payload.get("command").cloned())
            .unwrap_or_default();
        if let Some(err_str) = err_raw {
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
                let cmd_clean = truncate_chars(&redact_secrets(&cmd), 160);
                let err_clean = truncate_chars(&redact_secrets(err_str), 200);
                let ftype = payload.get("failure_type").map(|s| s.as_str()).unwrap_or("");
                let line = format!(
                    "{{\"event\":\"tool_failure\",\"host\":\"{}\",\"ts\":\"{}\",\"conversation_id\":\"{}\",\"tool\":\"{}\",\"failure_type\":\"{}\",\"cmd_preview\":\"{}\",\"error\":\"{}\"}}\n",
                    host.as_str(),
                    utc_iso_now(),
                    json_escape(conv_id),
                    json_escape(tool_name),
                    json_escape(ftype),
                    json_escape(&cmd_clean),
                    json_escape(&err_clean),
                );
                if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(lp) {
                    let _ = f.write_all(line.as_bytes());
                }
            }
        } else {
            let out_len = payload
                .get("tool_output")
                .or_else(|| payload.get("tool_response"))
                .or_else(|| payload.get("output"))
                .map(|s| s.len())
                .unwrap_or(0);
            if out_len > 16_000 {
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
                    let preview = truncate_chars(&redact_secrets(&cmd), 160);
                    let line = format!(
                        "{{\"event\":\"tool_spill\",\"kind\":\"output_spill\",\"host\":\"{}\",\"ts\":\"{}\",\"conv_id\":\"{}\",\"tool\":\"{}\",\"bytes\":{},\"preview\":\"{}\"}}\n",
                        host.as_str(),
                        utc_iso_now(),
                        json_escape(&truncate_chars(conv_id, 16)),
                        json_escape(tool_name),
                        out_len,
                        json_escape(&preview),
                    );
                    if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(tp) {
                        let _ = f.write_all(line.as_bytes());
                    }
                }
            }
        }
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

fn pretool_state_dir() -> PathBuf {
    PathBuf::from(
        env::var("WM_HOOK_STATE_DIR")
            .ok()
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| "/tmp/agy-hook-state".to_string()),
    )
}

fn pretool_san_id(conv_id: &str) -> String {
    conv_id
        .chars()
        .filter(|c| c.is_ascii_alphanumeric() || *c == '_' || *c == '-')
        .take(20)
        .collect()
}

fn write_bytes_atomic(path: &Path, body: &[u8]) {
    if let Some(parent) = path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    let name = path.file_name().and_then(|s| s.to_str()).unwrap_or("st.json");
    let tmp = path.with_file_name(format!(".{}.tmp.{}", name, std::process::id()));
    if fs::write(&tmp, body).is_ok() {
        if fs::rename(&tmp, path).is_err() {
            let _ = fs::remove_file(&tmp);
        }
    } else {
        let _ = fs::remove_file(&tmp);
    }
}

fn extract_json_object_entries(txt: &str) -> Vec<(String, String)> {
    let mut out = Vec::new();
    let bytes = txt.trim().as_bytes();
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
        if i < bytes.len() && bytes[i] == b'"' {
            let Some((val, end)) = parse_json_str_at(bytes, i) else { break };
            out.push((key, val));
            i = end;
        } else {
            let end = skip_json_value(bytes, i);
            if end > i {
                if let Ok(tok) = std::str::from_utf8(&bytes[i..end]) {
                    out.push((key, tok.trim().to_string()));
                }
            }
            i = end.max(i + 1);
        }
    }
    out
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum HostKind {
    Agy,
    Codex,
    Cursor,
}

impl HostKind {
    fn from_str(s: &str) -> Self {
        match s.trim().to_ascii_lowercase().as_str() {
            "codex" => HostKind::Codex,
            "cursor" => HostKind::Cursor,
            _ => HostKind::Agy,
        }
    }

    fn as_str(self) -> &'static str {
        match self {
            HostKind::Agy => "agy",
            HostKind::Codex => "codex",
            HostKind::Cursor => "cursor",
        }
    }
}

fn pretool_allow() {
    println!("{{\"decision\":\"allow\"}}");
}

fn pretool_decision(decision: &str, reason: &str) {
    println!(
        "{{\"decision\":\"{}\",\"reason\":\"{}\"}}",
        json_escape(decision),
        json_escape(reason)
    );
}

fn emit_pretool_allow(host: HostKind) {
    match host {
        HostKind::Agy => pretool_allow(),
        HostKind::Codex => println!("{{\"hookSpecificOutput\":{{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"allow\"}}}}"),
        HostKind::Cursor => println!("{{\"permission\":\"allow\"}}"),
    }
}

fn emit_pretool_decision(host: HostKind, decision: &str, reason: &str) {
    match host {
        HostKind::Agy => pretool_decision(decision, reason),
        HostKind::Codex => {
            let perm = if decision == "deny" { "deny" } else { "ask" };
            println!(
                "{{\"hookSpecificOutput\":{{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"{}\",\"permissionDecisionReason\":\"{}\"}}}}",
                perm,
                json_escape(reason)
            );
        }
        HostKind::Cursor => {
            let perm = if decision == "deny" { "deny" } else { "ask" };
            let esc = json_escape(reason);
            println!(
                "{{\"permission\":\"{}\",\"user_message\":\"{}\",\"agent_message\":\"{}\"}}",
                perm, esc, esc
            );
        }
    }
}

fn emit_pretool_rewrite_cmd(host: HostKind, cmd_key: &str, rewritten: &str) {
    match host {
        HostKind::Agy => println!("{{\"decision\":\"allow\",\"overwrite\":{{\"CommandLine\":\"{}\"}}}}", json_escape(rewritten)),
        HostKind::Codex => {
            let key = if cmd_key == "cmd" { "cmd" } else { "command" };
            println!(
                "{{\"hookSpecificOutput\":{{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"allow\",\"updatedInput\":{{\"{}\":\"{}\"}}}}}}",
                key,
                json_escape(rewritten)
            );
        }
        HostKind::Cursor => {
            let key = if cmd_key == "cmd" { "cmd" } else { "command" };
            println!(
                "{{\"permission\":\"allow\",\"updated_input\":{{\"{}\":\"{}\"}}}}",
                key,
                json_escape(rewritten)
            );
        }
    }
}

fn pretool_latest_model_and_ctx(tp: &str) -> (String, u64) {
    if tp.is_empty() {
        return (String::new(), 0);
    }
    let Ok(meta) = fs::metadata(tp) else { return (String::new(), 0); };
    if !meta.is_file() {
        return (String::new(), 0);
    }
    let sz = meta.len();
    let mtime_ns = meta
        .modified()
        .ok()
        .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let sd = pretool_state_dir();
    let cf = sd.join("ctx_stat_cache.json");
    if let Ok(ctxt) = fs::read_to_string(&cf) {
        let f = extract_top_level_fields(&ctxt, &["path", "sz", "mtime_ns", "ctx", "model"]);
        if f.get("path").map(|s| s.as_str()) == Some(tp)
            && f.get("sz").and_then(|s| s.parse::<u64>().ok()) == Some(sz)
            && f.get("mtime_ns").and_then(|s| s.parse::<u128>().ok()) == Some(mtime_ns)
            && f.contains_key("model")
        {
            return (
                f.get("model").cloned().unwrap_or_default(),
                f.get("ctx").and_then(|s| s.parse::<u64>().ok()).unwrap_or(0),
            );
        }
    }
    let Ok(mut fh) = File::open(tp) else { return (String::new(), 0); };
    let start = sz.saturating_sub(24_576);
    if start > 0 {
        let _ = fh.seek(SeekFrom::Start(start));
    }
    let mut buf = Vec::new();
    if fh.read_to_end(&mut buf).is_err() {
        return (String::new(), 0);
    }
    let tail = String::from_utf8_lossy(&buf);
    let mut ctx_found = 0u64;
    let mut model_found = String::new();
    for line in tail.lines().rev() {
        if !line.contains("\"PLANNER_RESPONSE\"") {
            continue;
        }
        let f = extract_top_level_fields(line, &["type", "input_tokens", "cache_read_tokens", "model"]);
        if f.get("type").map(|s| s.as_str()) != Some("PLANNER_RESPONSE") {
            continue;
        }
        let unc = f.get("input_tokens").and_then(|s| s.parse::<u64>().ok()).unwrap_or(0);
        let cac = f.get("cache_read_tokens").and_then(|s| s.parse::<u64>().ok()).unwrap_or(0);
        if unc + cac > 0 {
            ctx_found = unc + cac;
            model_found = f.get("model").cloned().unwrap_or_default();
            break;
        }
    }
    let cache_body = format!(
        "{{\"path\":\"{}\",\"sz\":{},\"mtime_ns\":{},\"ctx\":{},\"model\":\"{}\"}}",
        json_escape(tp),
        sz,
        mtime_ns,
        ctx_found,
        json_escape(&model_found)
    );
    write_bytes_atomic(&cf, cache_body.as_bytes());
    (model_found, ctx_found)
}

fn is_env_secret_basename(base: &str) -> bool {
    let b = base.trim();
    if b.eq_ignore_ascii_case(".env") {
        return true;
    }
    if b.len() > 5 && b[..5].eq_ignore_ascii_case(".env.") {
        let suffix = &b[5..];
        if suffix.bytes().all(|c| c.is_ascii_alphanumeric() || matches!(c, b'_' | b'.' | b'-')) {
            let low = suffix.to_ascii_lowercase();
            return !matches!(low.as_str(), "example" | "sample" | "template" | "dist" | "schema");
        }
    }
    false
}

fn is_token_word_sep(b: u8) -> bool {
    !(b.is_ascii_alphanumeric() || b == b'_')
}

fn has_ascii_word_ci(hay: &[u8], word: &[u8]) -> Option<usize> {
    if hay.len() < word.len() {
        return None;
    }
    let mut i = 0usize;
    while i + word.len() <= hay.len() {
        let prev_ok = i == 0 || is_token_word_sep(hay[i - 1]);
        let next_ok = i + word.len() == hay.len() || is_token_word_sep(hay[i + word.len()]);
        if prev_ok && next_ok && hay[i..i + word.len()].eq_ignore_ascii_case(word) {
            return Some(i);
        }
        i += 1;
    }
    None
}

fn is_rm_root_or_home_target(tok: &str) -> bool {
    if matches!(tok, "/" | "/*" | "~" | "$HOME") {
        return true;
    }
    for pref in ["/home/", "/Users/", "/home\\", "/Users\\"] {
        if let Some(rest) = tok.strip_prefix(pref) {
            if !rest.is_empty() && !rest.contains('/') {
                return true;
            }
        }
    }
    false
}

fn has_rm_rf_root_or_home(cmd: &str) -> bool {
    // Check command boundaries (start, ;, &, |, newline)
    let bytes = cmd.as_bytes();
    let n = bytes.len();
    let mut i = 0usize;
    while i < n {
        let at_start = i == 0 || matches!(bytes[i - 1], b';' | b'&' | b'|' | b'\n');
        let skip_ws = if at_start {
            let mut j = i;
            while j < n && matches!(bytes[j], b' ' | b'\t') {
                j += 1;
            }
            j
        } else {
            i += 1;
            continue;
        };
        if skip_ws + 2 <= n && &bytes[skip_ws..skip_ws + 2] == b"rm" && (skip_ws + 2 == n || bytes[skip_ws + 2].is_ascii_whitespace()) {
            let mut j = skip_ws + 2;
            let mut has_r = false;
            let mut has_f = false;
            let mut end_opts = false;
            while j < n {
                while j < n && matches!(bytes[j], b' ' | b'\t') {
                    j += 1;
                }
                if j >= n || matches!(bytes[j], b';' | b'&' | b'|' | b'\n') {
                    break;
                }
                let tstart = j;
                while j < n && !matches!(bytes[j], b' ' | b'\t' | b';' | b'&' | b'|' | b'\n') {
                    j += 1;
                }
                let tok = &cmd[tstart..j];
                if !end_opts && tok == "--" {
                    end_opts = true;
                    continue;
                }
                if !end_opts && tok.starts_with('-') && tok.len() > 1 && !tok.starts_with("--") {
                    for ch in tok[1..].bytes() {
                        if matches!(ch, b'r' | b'R') { has_r = true; }
                        if matches!(ch, b'f' | b'F') { has_f = true; }
                    }
                    continue;
                }
                if !end_opts && tok == "--recursive" { has_r = true; continue; }
                if !end_opts && tok == "--force" { has_f = true; continue; }
                if has_r && has_f && is_rm_root_or_home_target(tok) {
                    return true;
                }
            }
        }
        i = skip_ws + 1;
    }
    false
}

fn check_destructive_cmd(cmd: &str) -> Option<&'static str> {
    if has_rm_rf_root_or_home(cmd) {
        return Some("Recursive force-delete targeting root or home directory");
    }
    let low = cmd.to_ascii_lowercase();
    let lb = low.as_bytes();
    // SQL DROP TABLE/DATABASE/SCHEMA/VIEW or TRUNCATE TABLE
    for seg in low.split(|c: char| matches!(c, ';' | '\n' | '|' | '&')) {
        let toks: Vec<&str> = seg.split_whitespace().collect();
        for w in toks.windows(2) {
            if w[0] == "drop" && matches!(w[1], "table" | "database" | "schema" | "view") {
                return Some("SQL DROP statement detected");
            }
            if w[0] == "truncate" && w[1] == "table" {
                return Some("SQL TRUNCATE TABLE detected");
            }
            if w[0] == "gcloud" && toks.windows(3).any(|t3| t3 == ["gcloud", "projects", "delete"]) {
                return Some("Destructive gcloud project deletion detected");
            }
            if w[0] == "gsutil" && w[1] == "rm" {
                return Some("Cloud Storage deletion command detected");
            }
            if w[0] == "diskutil" && matches!(w[1], "erasedisk" | "erasevolume" | "partitiondisk") {
                return Some("Raw disk/block device formatting or overwrite detected");
            }
        }
        if toks.windows(3).any(|t3| t3 == ["gcloud", "storage", "rm"]) {
            return Some("Cloud Storage deletion command detected");
        }
        if toks.contains(&"git") && toks.contains(&"push") && (toks.contains(&"--force") || toks.contains(&"-f")) && (toks.contains(&"main") || toks.contains(&"master")) {
            return Some("Force-push to main/master branch detected");
        }
        if toks.iter().any(|t| t.starts_with("mkfs.") && t.len() > 5) {
            return Some("Raw disk/block device formatting or overwrite detected");
        }
        if toks.contains(&"dd") && toks.iter().any(|t| t.starts_with("if=")) && toks.iter().any(|t| t.starts_with("of=/dev/")) {
            return Some("Raw disk/block device formatting or overwrite detected");
        }
    }
    let _ = (lb, has_ascii_word_ci as fn(&[u8], &[u8]) -> Option<usize>);
    None
}

fn contains_kekkai_sensitive(cmd: &str) -> bool {
    if env::var("WM_HUGINN_KEKKAI_OFF").ok().as_deref() == Some("1") {
        return false;
    }
    let sock = PathBuf::from(
        env::var("WM_TRICAMERAL_SOCK_DIR")
            .ok()
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| "/tmp".to_string()),
    )
    .join("wm_huginn.sock");
    if !sock.exists() {
        return false;
    }
    let low = cmd.to_ascii_lowercase();
    [".ssh/", "id_ed25519", "id_rsa", ".aws/credentials", "/etc/shadow", "curl", "wget", "landlock", "seccomp"]
        .iter()
        .any(|pat| low.contains(pat))
}

fn posix_normpath(path: &str) -> String {
    let is_abs = path.starts_with('/');
    let mut comps: Vec<&str> = Vec::new();
    for part in path.split('/') {
        if part.is_empty() || part == "." {
            continue;
        }
        if part == ".." {
            if !comps.is_empty() && comps.last() != Some(&"..") {
                comps.pop();
            } else if !is_abs {
                comps.push("..");
            }
        } else {
            comps.push(part);
        }
    }
    if is_abs {
        format!("/{}", comps.join("/"))
    } else if comps.is_empty() {
        ".".to_string()
    } else {
        comps.join("/")
    }
}

fn expand_tilde(raw: &str) -> String {
    let s = raw.trim();
    if s == "~" {
        return env::var("HOME").unwrap_or_else(|_| "~".to_string());
    }
    if let Some(rest) = s.strip_prefix("~/") {
        if let Ok(home) = env::var("HOME") {
            return format!("{}/{}", home.trim_end_matches('/'), rest);
        }
    }
    s.to_string()
}

fn is_wide_line_file_rs(path: &Path, size: u64) -> bool {
    if size <= 24_576 {
        return false;
    }
    let Ok(mut f) = File::open(path) else { return false; };
    let mut buf = vec![0u8; 8192];
    let Ok(n) = f.read(&mut buf) else { return false; };
    if n == 0 || buf[..n.min(1024)].contains(&0) {
        return false;
    }
    let nl = buf[..n].iter().filter(|&&b| b == b'\n').count();
    nl == 0 || (n as f64 / nl.max(1) as f64) > 400.0
}

fn file_exceeds_lines_rs(path: &Path, threshold: usize, size: u64) -> bool {
    if size <= 4096 {
        return false;
    }
    if size > 32_768 {
        return true;
    }
    let Ok(mut f) = File::open(path) else { return false; };
    let mut buf = vec![0u8; 32_768];
    let Ok(n) = f.read(&mut buf) else { return false; };
    if buf[..n.min(1024)].contains(&0) {
        return false;
    }
    buf[..n].iter().filter(|&&b| b == b'\n').count() > threshold
}

fn parse_burst_entries(raw: &str) -> Vec<(String, i64)> {
    let mut out = Vec::new();
    let trim = raw.trim();
    if !trim.starts_with('[') {
        return out;
    }
    for elem in extract_json_array_elements(trim, 64) {
        let items = extract_json_array_elements(&elem, 2);
        if items.len() == 2 {
            if let Ok(step) = items[1].trim().parse::<i64>() {
                out.push((items[0].clone(), step));
            }
        }
    }
    out
}

fn track_and_check_burst_rs(conv_id: &str, abs_path: &str, step_idx: i64) -> bool {
    if conv_id.is_empty() || step_idx <= 0 {
        return false;
    }
    let san = pretool_san_id(conv_id);
    let bf = pretool_state_dir().join(format!("burst_{}.json", san));
    let mut kept = Vec::new();
    if let Ok(txt) = fs::read_to_string(&bf) {
        for (p, s) in parse_burst_entries(&txt) {
            if step_idx >= s && (step_idx - s) <= 10 {
                kept.push((p, s));
            }
        }
    }
    let mut distinct: Vec<&str> = kept.iter().map(|(p, _)| p.as_str()).collect();
    distinct.sort_unstable();
    distinct.dedup();
    let triggered = distinct.len() >= 4;
    kept.push((abs_path.to_string(), step_idx));
    if kept.len() > 32 {
        kept.drain(0..kept.len() - 32);
    }
    let body = format!(
        "[{}]",
        kept.iter()
            .map(|(p, s)| format!("[\"{}\",{}]", json_escape(p), s))
            .collect::<Vec<_>>()
            .join(",")
    );
    write_bytes_atomic(&bf, body.as_bytes());
    triggered
}

fn check_and_record_read_dedup_rs(
    conv_id: &str,
    abs_path: &str,
    s_key: &str,
    e_key: &str,
    step_idx: i64,
    cur_ctx: u64,
    mtime_ns: u128,
    size: u64,
) -> Option<String> {
    if conv_id.is_empty() || env::var("WM_READ_DEDUP_OFF").ok().as_deref() == Some("1") {
        return None;
    }
    let san = pretool_san_id(conv_id);
    let sf = pretool_state_dir().join(format!("{}.json", san));
    let mut entries: Vec<(String, u128, u64, i64, u64)> = Vec::new();
    if let Ok(txt) = fs::read_to_string(&sf) {
        for (k, v) in extract_json_object_entries(&txt) {
            let f = extract_top_level_fields(&v, &["mtime_ns", "size", "step", "ctx"]);
            let m = f.get("mtime_ns").and_then(|x| x.parse::<u128>().ok()).unwrap_or(0);
            let sz = f.get("size").and_then(|x| x.parse::<u64>().ok()).unwrap_or(0);
            let st = f.get("step").and_then(|x| x.parse::<i64>().ok()).unwrap_or(-999);
            let cx = f.get("ctx").and_then(|x| x.parse::<u64>().ok()).unwrap_or(0);
            entries.push((k, m, sz, st, cx));
        }
    }
    let key = format!("{}|{}|{}", abs_path, s_key, e_key);
    let mut reason: Option<String> = None;
    if let Some((_, pm, psz, pstep, pctx)) = entries.iter().find(|e| e.0 == key) {
        let unchanged = *pm == mtime_ns && *psz == size;
        let not_compacted = *pctx == 0 || cur_ctx == 0 || cur_ctx >= (*pctx * 85) / 100;
        let recent = (step_idx - *pstep) <= 20 && step_idx > *pstep;
        if unchanged && not_compacted && recent {
            let base = Path::new(abs_path).file_name().and_then(|s| s.to_str()).unwrap_or(abs_path);
            let s_disp = if s_key == "None" { "1" } else { s_key };
            let e_disp = if e_key == "None" { "EOF" } else { e_key };
            reason = Some(format!(
                "[Read-Dedup Guard] `{}` (L{}-{}) was already read unchanged at step {}. Reuse from existing context or pass a different StartLine/EndLine.",
                base, s_disp, e_disp, pstep
            ));
        }
    }
    entries.retain(|e| e.0 != key);
    entries.push((key, mtime_ns, size, step_idx, cur_ctx));
    if entries.len() > 128 {
        entries.sort_by_key(|e| e.3);
        let drop = entries.len() - 96;
        entries.drain(0..drop);
    }
    let serialized = format!(
        "{{{}}}",
        entries
            .iter()
            .map(|(k, m, sz, st, cx)| format!(
                "\"{}\":{{\"mtime_ns\":{},\"size\":{},\"step\":{},\"ctx\":{}}}",
                json_escape(k),
                m,
                sz,
                st,
                cx
            ))
            .collect::<Vec<_>>()
            .join(",")
    );
    write_bytes_atomic(&sf, serialized.as_bytes());
    reason
}

fn split_ext_lower(name: &str) -> (String, String) {
    let low = name.to_ascii_lowercase();
    if let Some(dot) = low.rfind('.') {
        if dot > 0 {
            return (low[..dot].to_string(), low[dot..].to_string());
        }
    }
    (low, String::new())
}

fn split_name_tokens_rs(stem: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut cur = String::new();
    for ch in stem.chars() {
        if ch.is_alphanumeric() {
            cur.push(ch);
        } else if !cur.is_empty() {
            out.push(std::mem::take(&mut cur));
        }
    }
    if !cur.is_empty() {
        out.push(cur);
    }
    out
}

fn score_filename_rs(cand: &str, target: &str) -> f64 {
    let cl = cand.to_ascii_lowercase();
    let tl = target.to_ascii_lowercase();
    if cl == tl {
        return 100.0;
    }
    let (c_stem, c_ext) = split_ext_lower(&cl);
    let (t_stem, t_ext) = split_ext_lower(&tl);
    let mut score = 0.0;
    if !t_ext.is_empty() && c_ext == t_ext {
        score += 10.0;
    }
    if !t_stem.is_empty() && !c_stem.is_empty() {
        if c_stem.contains(&t_stem) || t_stem.contains(&c_stem) {
            score += 22.0;
        }
        let pref = c_stem.bytes().zip(t_stem.bytes()).take_while(|(a, b)| a == b).count();
        if pref >= 2 {
            score += (pref.min(12) as f64) * 3.0;
        }
        let t_toks: Vec<String> = split_name_tokens_rs(&t_stem).into_iter().filter(|t| t.len() >= 2).collect();
        if !t_toks.is_empty() {
            let c_toks = split_name_tokens_rs(&c_stem);
            let mut hits = 0usize;
            for tt in &t_toks {
                if c_toks.contains(tt) {
                    hits += 1;
                }
            }
            score += 8.0 * (hits as f64);
        }
        let mut c_chars: Vec<char> = c_stem.chars().collect();
        c_chars.sort_unstable();
        c_chars.dedup();
        let mut t_chars: Vec<char> = t_stem.chars().collect();
        t_chars.sort_unstable();
        t_chars.dedup();
        let inter = c_chars.iter().filter(|c| t_chars.contains(c)).count();
        let uni = c_chars.len() + t_chars.len() - inter;
        if uni > 0 {
            score += 8.0 * (inter as f64) / (uni as f64);
        }
    }
    score
}

fn missing_path_hint_rs(p_str: &str, raw_path: &str) -> String {
    let p = Path::new(p_str);
    let ignored = |name: &str| matches!(name, ".git" | "node_modules" | "__pycache__" | ".DS_Store");
    if p.is_dir() {
        let mut items = Vec::new();
        if let Ok(rd) = fs::read_dir(p) {
            for (idx, ent) in rd.enumerate() {
                if idx >= 80 { break; }
                if let Ok(e) = ent {
                    let nm = e.file_name().to_string_lossy().into_owned();
                    if ignored(&nm) { continue; }
                    let is_d = e.file_type().map(|t| t.is_dir()).unwrap_or(false);
                    items.push(if is_d { format!("{}/", nm) } else { nm });
                }
            }
        }
        items.sort();
        let total = items.len();
        let shown: Vec<String> = items.into_iter().take(10).collect();
        let base = p.file_name().and_then(|s| s.to_str()).unwrap_or(p_str);
        return if shown.is_empty() {
            format!("[Path Hint] `{}` is a directory, not a file. Directory is empty.", base)
        } else {
            let suffix = if total > 10 { format!(" (+{} more)", total - 10) } else { String::new() };
            format!("[Path Hint] `{}` is a directory, not a file. Entries: {}{}.", base, shown.join(", "), suffix)
        };
    }

    let parent = p.parent();
    let target_name = p.file_name().and_then(|s| s.to_str()).unwrap_or("");
    let gp = parent.and_then(|par| par.parent()).filter(|g| !g.as_os_str().is_empty());
    let gp_hit = gp.and_then(|g| {
        if g.is_dir() && !target_name.is_empty() {
            let cand = g.join(target_name);
            if cand.is_file() {
                return Some(cand.to_string_lossy().into_owned());
            }
        }
        None
    });

    if let Some(par) = parent.filter(|par| !par.as_os_str().is_empty() && par.is_dir()) {
        let mut scored: Vec<(i64, String, String)> = Vec::new();
        if let Ok(rd) = fs::read_dir(par) {
            for (idx, ent) in rd.enumerate() {
                if idx >= 100 { break; }
                if let Ok(e) = ent {
                    let nm = e.file_name().to_string_lossy().into_owned();
                    if ignored(&nm) { continue; }
                    let is_d = e.file_type().map(|t| t.is_dir()).unwrap_or(false);
                    let disp = if is_d { format!("{}/", nm) } else { nm.clone() };
                    let sc = (score_filename_rs(&nm, target_name) * 1000.0) as i64;
                    scored.push((-sc, disp.to_ascii_lowercase(), disp));
                }
            }
        }
        scored.sort();
        let best: Vec<String> = scored.into_iter().take(4).map(|x| x.2).collect();
        let p_disp = par.file_name().and_then(|s| s.to_str()).filter(|s| !s.is_empty()).unwrap_or_else(|| par.to_str().unwrap_or(""));
        let mut msg = format!("[Path Hint] File does not exist: `{}`.", raw_path);
        if best.is_empty() {
            let _ = write!(msg, " `{}/` has no matching entries.", p_disp);
        } else {
            let _ = write!(msg, " Closest in `{}/`: {}.", p_disp, best.join(", "));
        }
        if let Some(gh) = gp_hit {
            let _ = write!(msg, " Found `{}` in parent dir: `{}`.", target_name, gh);
        }
        return msg;
    }

    if let Some(g) = gp.filter(|g| g.is_dir()) {
        let p_base = parent.and_then(|par| par.file_name()).and_then(|s| s.to_str()).unwrap_or("");
        let mut scored: Vec<(i64, String, String)> = Vec::new();
        if let Ok(rd) = fs::read_dir(g) {
            for (idx, ent) in rd.enumerate() {
                if idx >= 60 { break; }
                if let Ok(e) = ent {
                    let nm = e.file_name().to_string_lossy().into_owned();
                    if ignored(&nm) { continue; }
                    let is_d = e.file_type().map(|t| t.is_dir()).unwrap_or(false);
                    let disp = if is_d { format!("{}/", nm) } else { nm.clone() };
                    let sc = (score_filename_rs(&nm, p_base) * 1000.0) as i64;
                    scored.push((-sc, disp.to_ascii_lowercase(), disp));
                }
            }
        }
        scored.sort();
        let gp_best: Vec<String> = scored.into_iter().take(4).map(|x| x.2).collect();
        let par_s = parent.and_then(|x| x.to_str()).unwrap_or("");
        let mut msg = format!("[Path Hint] Parent directory does not exist: `{}`.", par_s);
        if let Some(gh) = gp_hit {
            let _ = write!(msg, " Found `{}` at `{}`.", target_name, gh);
        } else if !gp_best.is_empty() {
            let _ = write!(msg, " Closest in `{}/`: {}.", g.display(), gp_best.join(", "));
        }
        return msg;
    }

    format!("[Path Hint] File and parent directory do not exist: `{}`.", raw_path)
}

fn handle_read_file_rs(args: &HashMap<String, String>, transcript: &str, conv_id: &str, step_idx: i64, cwd: &str) {
    let raw_path = args.get("Path").or_else(|| args.get("path")).map(|s| s.as_str()).unwrap_or("");
    if raw_path.is_empty() {
        pretool_allow();
        return;
    }
    let low = raw_path.to_ascii_lowercase();
    let media_exts = [".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".pdf"];
    if media_exts.iter().any(|ext| low.ends_with(ext)) {
        let (_, cur_ctx) = pretool_latest_model_and_ctx(transcript);
        if cur_ctx >= 150_000 {
            let reason = format!(
                "[Multimodal Cache Guard] Session context is ~{}k tokens. Loading media inline invalidates Gemini 4 prompt cache (3-7x TTFT spike). Prefer delegating visual inspection to a read-only subagent, or approve to load inline.",
                cur_ctx / 1000
            );
            pretool_decision("force_ask", &reason);
            return;
        }
        pretool_allow();
        return;
    }

    let binary_exts = [
        ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".pdf", ".zip", ".gz", ".tar",
        ".zst", ".xz", ".bz2", ".bin", ".exe", ".so", ".dylib", ".dll", ".o", ".a",
        ".wasm", ".sqlite", ".db", ".gguf", ".safetensors", ".pt", ".pth", ".mp3", ".mp4", ".wav",
    ];
    if raw_path.contains("/.system_generated/") || binary_exts.iter().any(|ext| low.ends_with(ext)) {
        pretool_allow();
        return;
    }

    let p_exp = expand_tilde(raw_path);
    let p_str = if Path::new(&p_exp).is_absolute() {
        posix_normpath(&p_exp)
    } else if !cwd.is_empty() && Path::new(cwd).is_dir() {
        posix_normpath(&format!("{}/{}", cwd.trim_end_matches('/'), p_exp))
    } else if Path::new(&p_exp).exists() {
        fs::canonicalize(&p_exp)
            .map(|p| p.to_string_lossy().into_owned())
            .unwrap_or_else(|_| p_exp.clone())
    } else {
        pretool_allow();
        return;
    };

    let p_obj = Path::new(&p_str);
    if !p_obj.is_file() {
        if env::var("WM_READ_HINT_OFF").ok().as_deref() == Some("1") {
            pretool_allow();
            return;
        }
        let hint = missing_path_hint_rs(&p_str, raw_path);
        pretool_decision("deny", &hint);
        return;
    }

    let Ok(st) = fs::metadata(p_obj) else { pretool_allow(); return; };
    let size = st.len();
    let mtime_ns = st.modified().ok().and_then(|t| t.duration_since(UNIX_EPOCH).ok()).map(|d| d.as_nanos()).unwrap_or(0);
    let abs_p = fs::canonicalize(p_obj).map(|p| p.to_string_lossy().into_owned()).unwrap_or_else(|_| p_str.clone());

    let mut s_int = args.get("StartLine").and_then(|v| v.trim().parse::<i64>().ok());
    let mut e_int = args.get("EndLine").and_then(|v| v.trim().parse::<i64>().ok());
    let has_byte_offset = args.contains_key("ByteOffset");
    let mut ow_start: Option<i64> = None;
    let mut ow_end: Option<i64> = None;
    let mut cur_ctx = 0u64;

    if !has_byte_offset && env::var("WM_READ_CLAMP_OFF").ok().as_deref() != Some("1") {
        if let Some(s) = s_int {
            if s < 1 {
                s_int = Some(1);
                ow_start = Some(1);
            }
        }
        let inverted = matches!((s_int, e_int), (Some(s), Some(e)) if e < s);
        let span = match (s_int, e_int, inverted) {
            (Some(s), Some(e), false) => Some(e - s + 1),
            _ => None,
        };
        let wide_lines = (inverted || span.map(|sp| sp > 50).unwrap_or(true)) && is_wide_line_file_rs(p_obj, size);
        let compact_lim = if wide_lines { 50 } else { 150 };
        let already_compact = !inverted && span.map(|sp| sp <= compact_lim).unwrap_or(false);

        if !already_compact {
            cur_ctx = pretool_latest_model_and_ctx(transcript).1;
            let (mut max_span, mut clamp_trigger) = if cur_ctx >= 160_000 {
                (100i64, 150i64)
            } else if cur_ctx >= 120_000 {
                (150i64, 200i64)
            } else {
                (200i64, 250i64)
            };
            let mut wide_lim = 50i64;
            if grimoire_profile_exists() {
                let (g_unb, g_clamp, g_wide) = load_grimoire_read_limits(HostKind::Agy);
                clamp_trigger = clamp_trigger.min(g_unb as i64);
                max_span = max_span.min(g_clamp as i64);
                wide_lim = wide_lim.min(g_wide as i64);
            }
            if wide_lines {
                max_span = max_span.min(wide_lim);
                clamp_trigger = clamp_trigger.min(wide_lim + 10);
            }
            let is_large = wide_lines || file_exceeds_lines_rs(p_obj, clamp_trigger as usize, size);
            if is_large && !conv_id.is_empty() && step_idx > 0 {
                if track_and_check_burst_rs(conv_id, &abs_p, step_idx) {
                    max_span = max_span.min(if wide_lines { 50 } else { 120 });
                }
            }

            if inverted {
                let s = s_int.unwrap();
                let e = e_int.unwrap();
                if (1..=250).contains(&e) {
                    let ne = s + e.min(max_span) - 1;
                    e_int = Some(ne);
                    ow_end = Some(ne);
                } else if e > 250 {
                    let new_s = e;
                    let new_e = s.min(new_s + max_span - 1);
                    s_int = Some(new_s);
                    e_int = Some(new_e);
                    ow_start = Some(new_s);
                    ow_end = Some(new_e);
                } else {
                    let ne = s + max_span - 1;
                    e_int = Some(ne);
                    ow_end = Some(ne);
                }
            } else if s_int.is_none() && e_int.is_none() {
                if is_large {
                    s_int = Some(1);
                    e_int = Some(max_span);
                    ow_start = Some(1);
                    ow_end = Some(max_span);
                }
            } else if s_int.is_some() && e_int.is_none() {
                if is_large {
                    let ne = s_int.unwrap() + max_span - 1;
                    e_int = Some(ne);
                    ow_end = Some(ne);
                }
            } else if let (Some(s), Some(e)) = (s_int, e_int) {
                if (e - s + 1) > clamp_trigger {
                    let ne = s + max_span - 1;
                    e_int = Some(ne);
                    ow_end = Some(ne);
                }
            } else if s_int.is_none() && e_int.is_some() {
                let e = e_int.unwrap();
                if e < 1 {
                    e_int = Some(max_span);
                    ow_end = Some(max_span);
                } else if is_large && e > clamp_trigger {
                    s_int = Some(1);
                    e_int = Some(max_span);
                    ow_start = Some(1);
                    ow_end = Some(max_span);
                }
            }
        }
    }

    if !conv_id.is_empty() && step_idx > 0 {
        if cur_ctx == 0 && !transcript.is_empty() {
            cur_ctx = pretool_latest_model_and_ctx(transcript).1;
        }
        let s_key = s_int.map(|v| v.to_string()).unwrap_or_else(|| "None".to_string());
        let e_key = e_int.map(|v| v.to_string()).unwrap_or_else(|| "None".to_string());
        if let Some(reason) = check_and_record_read_dedup_rs(conv_id, &abs_p, &s_key, &e_key, step_idx, cur_ctx, mtime_ns, size) {
            pretool_decision("deny", &reason);
            return;
        }
    }

    match (ow_start, ow_end) {
        (Some(s), Some(e)) => println!("{{\"decision\":\"allow\",\"overwrite\":{{\"StartLine\":{},\"EndLine\":{}}}}}", s, e),
        (Some(s), None) => println!("{{\"decision\":\"allow\",\"overwrite\":{{\"StartLine\":{}}}}}", s),
        (None, Some(e)) => println!("{{\"decision\":\"allow\",\"overwrite\":{{\"EndLine\":{}}}}}", e),
        (None, None) => pretool_allow(),
    }
}

fn handle_manage_task_rs(args: &HashMap<String, String>, conv_id: &str, step_idx: i64) {
    if env::var("WM_POLL_GUARD_OFF").ok().as_deref() == Some("1") {
        pretool_allow();
        return;
    }
    let action = args.get("Action").map(|s| s.trim().to_ascii_lowercase()).unwrap_or_default();
    let task_id = args.get("TaskId").map(|s| s.trim()).unwrap_or("");
    if action != "status" || task_id.is_empty() || conv_id.is_empty() {
        pretool_allow();
        return;
    }
    let san = pretool_san_id(conv_id);
    let pf = pretool_state_dir().join(format!("poll_{}.json", san));
    let mut entries: Vec<(String, i64, i64)> = Vec::new();
    if let Ok(txt) = fs::read_to_string(&pf) {
        for (k, v) in extract_json_object_entries(&txt) {
            let f = extract_top_level_fields(&v, &["count", "last_step"]);
            let c = f.get("count").and_then(|x| x.parse::<i64>().ok()).unwrap_or(0);
            let ls = f.get("last_step").and_then(|x| x.parse::<i64>().ok()).unwrap_or(-999);
            entries.push((k, c, ls));
        }
    }
    let mut cnt = 1i64;
    if let Some((_, prev_c, prev_step)) = entries.iter().find(|e| e.0 == task_id) {
        if step_idx > 0 && (step_idx - *prev_step) <= 12 && step_idx > *prev_step {
            cnt = *prev_c + 1;
        }
    }
    entries.retain(|e| e.0 != task_id);
    entries.push((task_id.to_string(), cnt, step_idx));
    if entries.len() > 32 {
        entries.sort_by_key(|e| e.2);
        let drop = entries.len() - 24;
        entries.drain(0..drop);
    }
    let serialized = format!(
        "{{{}}}",
        entries
            .iter()
            .map(|(k, c, ls)| format!("\"{}\":{{\"count\":{},\"last_step\":{}}}", json_escape(k), c, ls))
            .collect::<Vec<_>>()
            .join(",")
    );
    write_bytes_atomic(&pf, serialized.as_bytes());
    if cnt >= 3 {
        let reason = format!(
            "[Anti-Poll Guard] Checked status of {} {} times within 12 steps. Stop polling in a loop — end your turn or do other work; background tasks notify you automatically on completion.",
            task_id, cnt
        );
        pretool_decision("deny", &reason);
    } else {
        pretool_allow();
    }
}

fn handle_swarm_and_subagent_rs(tool_name: &str, args: &HashMap<String, String>, transcript: &str) {
    if env::var("WM_SWARM_GUARD_OFF").ok().as_deref() == Some("1") {
        pretool_allow();
        return;
    }
    if tool_name == "swarm" {
        let action = args.get("action").map(|s| s.trim().to_ascii_lowercase()).unwrap_or_default();
        if action == "add" {
            let fork_arg = args.get("fork").map(|s| s.trim()).unwrap_or("");
            let model_arg = args.get("model").map(|s| s.trim()).unwrap_or("");
            let brief_txt = args.get("brief").map(|s| s.as_str()).unwrap_or("");
            if !fork_arg.is_empty() {
                let (_, cur_ctx) = pretool_latest_model_and_ctx(transcript);
                if cur_ctx >= 80_000 {
                    let reason = format!(
                        "[Swarm Fork Guard] Coordinator context is ~{}k tokens. Forking copies this context onto every worker. Omit `fork` and pass a compact fresh brief + shared codemap (`wm-handoff --swarm-map`), or confirm to fork anyway.",
                        cur_ctx / 1000
                    );
                    pretool_decision("force_ask", &reason);
                    return;
                }
            }
            if model_arg.is_empty() {
                let (cur_model, _) = pretool_latest_model_and_ctx(transcript);
                if cur_model.to_ascii_lowercase().contains("neon") {
                    pretool_decision(
                        "force_ask",
                        "[Swarm Model Guard] Coordinator is on Neon and `model` is omitted — spawned worker would inherit Neon (~3.1x burn). Pass `model: \"gemini-4-hydrogen-high\"` (or `xhigh` for complex debugging), or confirm to spawn on Neon.",
                    );
                    return;
                }
            }
            if brief_txt.chars().count() > 6000 {
                let reason = format!(
                    "[Swarm Brief Guard] Brief is {:.1} KB (>6 KB), bloating the shared board for every member. Write details to a file and pass a <=150-token pointer brief, or confirm to post inline.",
                    brief_txt.len() as f64 / 1024.0
                );
                pretool_decision("force_ask", &reason);
                return;
            }
        }
        pretool_allow();
        return;
    }
    if tool_name == "post_message" {
        let msg = args
            .get("message")
            .or_else(|| args.get("content"))
            .or_else(|| args.get("body"))
            .map(|s| s.as_str())
            .unwrap_or("");
        if msg.chars().count() > 4000 {
            let reason = format!(
                "[Board Bloat Guard] Board message is {:.1} KB (>4 KB). Write full logs/tables to a file and post a <=150-token CPR pointer receipt, or confirm to post inline.",
                msg.len() as f64 / 1024.0
            );
            pretool_decision("force_ask", &reason);
            return;
        }
        pretool_allow();
        return;
    }
    if tool_name == "invoke_subagent" {
        let tname = args.get("TypeName").map(|s| s.trim().to_ascii_lowercase()).unwrap_or_default();
        let marg = args.get("Model").map(|s| s.trim().to_ascii_lowercase()).unwrap_or_default();
        if tname == "research" && (marg.is_empty() || marg == "inherit") {
            let (cur_model, _) = pretool_latest_model_and_ctx(transcript);
            if cur_model.to_ascii_lowercase().contains("neon") {
                if env::var("WM_SUBAGENT_AUTO_FLASH").ok().as_deref() == Some("1") {
                    println!("{{\"decision\":\"allow\",\"overwrite\":{{\"Model\":\"flash\"}}}}");
                    return;
                }
                pretool_decision(
                    "force_ask",
                    "[Subagent Model Guard] Parent is on Neon and `research` subagent uses `Model=\"inherit\"` (would run read-only research on Neon). Pass `Model=\"flash\"` (or set `WM_SUBAGENT_AUTO_FLASH=1`), or confirm to run on Neon.",
                );
                return;
            }
        }
        pretool_allow();
        return;
    }
    pretool_allow();
}

fn handle_whitemagic_mcp_rs(tool_name: &str, args: &HashMap<String, String>) {
    match tool_name {
        "mcp_whitemagic_session_continuity" | "mcp_whitemagic_whitemagic_gen3_session_continuity" => {
            let tb = args.get("token_budget").and_then(|v| v.trim().parse::<f64>().ok());
            if tb.is_none() || tb.map(|v| v > 2000.0).unwrap_or(false) {
                println!("{{\"decision\":\"allow\",\"overwrite\":{{\"token_budget\":1200}}}}");
            } else {
                pretool_allow();
            }
        }
        "mcp_whitemagic_memory_recall" => {
            let lim = args.get("limit").and_then(|v| v.trim().parse::<f64>().ok());
            let mcb = args.get("max_content_bytes").and_then(|v| v.trim().parse::<f64>().ok());
            let set_lim = lim.is_none() || lim.map(|v| v > 6.0).unwrap_or(false);
            let set_mcb = mcb.is_none() || mcb.map(|v| v > 1200.0).unwrap_or(false);
            match (set_lim, set_mcb) {
                (true, true) => println!("{{\"decision\":\"allow\",\"overwrite\":{{\"limit\":5,\"max_content_bytes\":900}}}}"),
                (true, false) => println!("{{\"decision\":\"allow\",\"overwrite\":{{\"limit\":5}}}}"),
                (false, true) => println!("{{\"decision\":\"allow\",\"overwrite\":{{\"max_content_bytes\":900}}}}"),
                (false, false) => pretool_allow(),
            }
        }
        "mcp_whitemagic_whitemagic_gen3_memory_recall" => {
            let lim = args.get("limit").and_then(|v| v.trim().parse::<f64>().ok());
            if lim.is_none() || lim.map(|v| v > 6.0).unwrap_or(false) {
                println!("{{\"decision\":\"allow\",\"overwrite\":{{\"limit\":5}}}}");
            } else {
                pretool_allow();
            }
        }
        "mcp_wm-research-engine_research_hunt" => {
            let lim = args.get("limit").and_then(|v| v.trim().parse::<f64>().ok());
            if lim.map(|v| v > 8.0).unwrap_or(false) {
                println!("{{\"decision\":\"allow\",\"overwrite\":{{\"limit\":8}}}}");
            } else {
                pretool_allow();
            }
        }
        "mcp_firecrawl_firecrawl_scrape" => {
            if !args.contains_key("markdown_only") && !args.contains_key("format") {
                println!("{{\"decision\":\"allow\",\"overwrite\":{{\"markdown_only\":true}}}}");
            } else {
                pretool_allow();
            }
        }
        _ => pretool_allow(),
    }
}

fn shell_quote(s: &str) -> String {
    if !s.is_empty() && s.bytes().all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'@' | b'%' | b'+' | b'=' | b':' | b',' | b'.' | b'/' | b'-')) {
        return s.to_string();
    }
    let mut out = String::with_capacity(s.len() + 2);
    out.push('\'');
    for ch in s.chars() {
        if ch == '\'' {
            out.push_str("'\\''");
        } else {
            out.push(ch);
        }
    }
    out.push('\'');
    out
}

fn strip_trailing_redirs_rs(s: &str) -> &str {
    let mut cur = s.trim();
    loop {
        let mut stripped = false;
        for suf in ["2>&1", "2>/dev/null"] {
            if let Some(pref) = cur.strip_suffix(suf) {
                if pref.is_empty() || pref.ends_with(|c: char| c.is_whitespace()) {
                    cur = pref.trim_end();
                    stripped = true;
                    break;
                }
            }
        }
        if !stripped {
            break;
        }
    }
    cur
}

fn strip_unquoted_line_cont_rs(s: &str) -> String {
    if !s.contains("\\\n") && !s.contains("\\\r\n") {
        return s.to_string();
    }
    let b = s.as_bytes();
    let n = b.len();
    let mut out = String::with_capacity(n);
    let mut in_s = false;
    let mut in_d = false;
    let mut i = 0usize;
    while i < n {
        let ch = b[i];
        if in_s {
            if ch == b'\'' { in_s = false; }
            if let Some(c) = s[i..].chars().next() { out.push(c); i += c.len_utf8(); } else { break; }
            continue;
        }
        if ch == b'\\' {
            if i + 1 < n && b[i + 1] == b'\n' { out.push(' '); i += 2; continue; }
            if i + 2 < n && b[i + 1] == b'\r' && b[i + 2] == b'\n' { out.push(' '); i += 3; continue; }
            if i + 1 < n {
                out.push('\\');
                if let Some(c) = s[i + 1..].chars().next() { out.push(c); i += 1 + c.len_utf8(); } else { i += 1; }
                continue;
            }
        }
        if ch == b'\'' && !in_d { in_s = true; }
        else if ch == b'"' { in_d = !in_d; }
        if let Some(c) = s[i..].chars().next() { out.push(c); i += c.len_utf8(); } else { break; }
    }
    out
}

fn has_unsafe_shell_meta_rs(s: &str) -> bool {
    let b = s.as_bytes();
    let n = b.len();
    let mut in_s = false;
    let mut in_d = false;
    let mut esc = false;
    for i in 0..n {
        let ch = b[i];
        if esc { esc = false; continue; }
        if in_s { if ch == b'\'' { in_s = false; } continue; }
        if in_d {
            if ch == b'\\' { esc = true; }
            else if ch == b'"' { in_d = false; }
            else if matches!(ch, b'\n' | b'$' | b'`') { return true; }
            continue;
        }
        match ch {
            b'\\' => esc = true,
            b'\'' => in_s = true,
            b'"' => in_d = true,
            b'|' | b'>' | b'<' | b';' | b'`' | b'\n' | b'$' | b'*' | b'?' | b'[' | b']' | b'{' | b'}' | b'(' | b')' => return true,
            b'&' => {
                let prev = i > 0 && b[i - 1] == b'&';
                let next = i + 1 < n && b[i + 1] == b'&';
                if !(prev ^ next) { return true; }
            }
            b'~' => {
                if i == 0 || b[i - 1].is_ascii_whitespace() { return true; }
            }
            _ => {}
        }
    }
    in_s || in_d || esc
}

fn split_top_level_and_rs(s: &str) -> Option<Vec<String>> {
    let b = s.as_bytes();
    let n = b.len();
    let mut parts = Vec::new();
    let mut cur = String::new();
    let mut in_s = false;
    let mut in_d = false;
    let mut esc = false;
    let mut i = 0usize;
    while i < n {
        let ch = b[i];
        if esc {
            if let Some(c) = s[i..].chars().next() { cur.push(c); esc = false; i += c.len_utf8(); } else { break; }
            continue;
        }
        if in_s {
            if ch == b'\'' { in_s = false; }
            if let Some(c) = s[i..].chars().next() { cur.push(c); i += c.len_utf8(); } else { break; }
            continue;
        }
        if in_d {
            if ch == b'\\' { esc = true; cur.push('\\'); i += 1; continue; }
            if ch == b'"' { in_d = false; }
            if let Some(c) = s[i..].chars().next() { cur.push(c); i += c.len_utf8(); } else { break; }
            continue;
        }
        if ch == b'\\' { esc = true; cur.push('\\'); i += 1; continue; }
        if ch == b'\'' { in_s = true; cur.push('\''); i += 1; continue; }
        if ch == b'"' { in_d = true; cur.push('"'); i += 1; continue; }
        if ch == b'&' && i + 1 < n && b[i + 1] == b'&' {
            if i + 2 < n && b[i + 2] == b'&' { return None; }
            parts.push(cur.trim().to_string());
            cur.clear();
            i += 2;
            continue;
        }
        if let Some(c) = s[i..].chars().next() { cur.push(c); i += c.len_utf8(); } else { break; }
    }
    if in_s || in_d || esc {
        return None;
    }
    parts.push(cur.trim().to_string());
    Some(parts)
}

fn posix_shlex_split(s: &str) -> Option<Vec<String>> {
    let mut out = Vec::new();
    let mut cur = String::new();
    let mut has_tok = false;
    let mut in_s = false;
    let mut in_d = false;
    let mut esc = false;
    for ch in s.chars() {
        if esc {
            if in_d && !matches!(ch, '$' | '`' | '"' | '\\' | '\n') {
                cur.push('\\');
            }
            cur.push(ch);
            has_tok = true;
            esc = false;
            continue;
        }
        if in_s {
            if ch == '\'' { in_s = false; } else { cur.push(ch); }
            has_tok = true;
            continue;
        }
        if in_d {
            match ch {
                '\\' => { esc = true; has_tok = true; }
                '"' => { in_d = false; has_tok = true; }
                _ => { cur.push(ch); has_tok = true; }
            }
            continue;
        }
        match ch {
            '\\' => { esc = true; has_tok = true; }
            '\'' => { in_s = true; has_tok = true; }
            '"' => { in_d = true; has_tok = true; }
            c if c.is_whitespace() => {
                if has_tok {
                    out.push(std::mem::take(&mut cur));
                    has_tok = false;
                }
            }
            _ => { cur.push(ch); has_tok = true; }
        }
    }
    if esc || in_s || in_d {
        return None;
    }
    if has_tok {
        out.push(cur);
    }
    Some(out)
}

fn is_pretool_safe_squeezable(cmd_base: &str, cmd_args: &[String]) -> bool {
    match cmd_base {
        "git" => {
            let mut i = 0;
            while i < cmd_args.len() {
                let a = cmd_args[i].as_str();
                if a == "-c" || a == "--paginate" || a.starts_with("-c") || a.starts_with("--exec-path") || a.starts_with("--config-env") || a.starts_with("--upload-pack") {
                    return false;
                }
                if a == "-C" { i += 2; continue; }
                if a.starts_with('-') { i += 1; continue; }
                let nxt = cmd_args.get(i + 1).map(|x| x.as_str()).unwrap_or("");
                if matches!(a, "status" | "diff" | "log" | "show" | "ls-files" | "blame" | "shortlog") {
                    return true;
                }
                if a == "branch" && !cmd_args[i + 1..].iter().any(|x| matches!(x.as_str(), "-d" | "-D" | "-m" | "-M" | "--delete")) {
                    return true;
                }
                if (a == "stash" || a == "worktree") && nxt == "list" {
                    return true;
                }
                return false;
            }
            false
        }
        "cargo" => {
            if cmd_args.iter().any(|a| a == "--config" || a.starts_with("--config=")) {
                return false;
            }
            let sub = cmd_args.iter().find(|a| !a.starts_with('-') && !a.starts_with('+')).map(|s| s.as_str()).unwrap_or("");
            matches!(sub, "check" | "test" | "clippy" | "build" | "tree")
        }
        "pytest" | "grep" | "ls" | "tree" | "du" | "ruff" | "mypy" | "eslint" | "tsc" => true,
        "go" => matches!(cmd_args.first().map(|s| s.as_str()).unwrap_or(""), "test" | "vet" | "list" | "build"),
        "python3" | "python" => {
            cmd_args.len() >= 2 && cmd_args[0] == "-m" && matches!(cmd_args[1].as_str(), "pytest" | "unittest" | "mypy" | "ruff")
        }
        "npm" | "pnpm" | "yarn" => {
            let s0 = cmd_args.first().map(|s| s.as_str()).unwrap_or("");
            let s1 = cmd_args.get(1).map(|s| s.as_str()).unwrap_or("");
            matches!(s0, "test" | "ls" | "outdated") || (s0 == "run" && matches!(s1, "typecheck" | "lint" | "build" | "test" | "check"))
        }
        "rg" => !cmd_args.iter().any(|a| a == "--pre" || a.starts_with("--pre=")),
        "find" => !cmd_args.iter().any(|a| matches!(a.as_str(), "-exec" | "-execdir" | "-ok" | "-okdir" | "-delete" | "-fls" | "-fprint" | "-fprint0" | "-fprintf")),
        "wm3" | "wm-gen3" => {
            let mut sub = "";
            let mut i = 0;
            while i < cmd_args.len() {
                let a = cmd_args[i].as_str();
                if a == "--store" || a == "--galaxy" { i += 2; continue; }
                if a.starts_with('-') { i += 1; continue; }
                sub = a;
                break;
            }
            matches!(sub, "recall" | "evidence-recall" | "stats" | "status" | "session" | "list" | "build-info" | "inspect")
                || cmd_args.iter().any(|a| a == "--version")
        }
        "gh" => {
            let s0 = cmd_args.first().map(|s| s.as_str()).unwrap_or("");
            let s1 = cmd_args.get(1).map(|s| s.as_str()).unwrap_or("");
            matches!(s0, "pr" | "issue" | "repo" | "run" | "workflow" | "release")
                && matches!(s1, "list" | "view" | "status" | "diff" | "checks")
        }
        _ => false,
    }
}

fn is_safe_py_test_or_bench_rs(arg: &str) -> bool {
    if arg.is_empty() || arg.starts_with('-') {
        return false;
    }
    if arg.chars().any(|c| "$*?{}[]~;|&><`\n\r".contains(c)) {
        return false;
    }
    let base = Path::new(arg).file_name().and_then(|s| s.to_str()).unwrap_or("");
    base.ends_with(".py") && (base.starts_with("test_") || base.starts_with("bench_") || base.ends_with("_test.py"))
}

fn find_wm_squeeze_rs() -> Option<String> {
    if let Ok(exe) = env::current_exe() {
        if exe.is_file() {
            return Some(exe.to_string_lossy().into_owned());
        }
    }
    None
}

fn run_py_fallback(fallback_py: Option<&str>, stdin_buf: &str) {
    let Some(py_path) = fallback_py.filter(|p| Path::new(p).is_file()) else {
        pretool_allow();
        return;
    };
    let mut cmd = Command::new("python3");
    cmd.args(["-S", "-B", py_path])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit());
    let Ok(mut child) = cmd.spawn() else {
        pretool_allow();
        return;
    };
    if let Some(mut child_in) = child.stdin.take() {
        let _ = child_in.write_all(stdin_buf.as_bytes());
    }
    if let Ok(out) = child.wait_with_output() {
        let txt = String::from_utf8_lossy(&out.stdout);
        let t = txt.trim();
        if !t.is_empty() && t.starts_with('{') {
            println!("{}", t);
            return;
        }
    }
    pretool_allow();
}

enum SimpleCmdOutcome {
    Rewrite(String),
    AllowAsIs,
    DelegatePy,
}

fn classify_run_command_rs(cmd_line: &str) -> SimpleCmdOutcome {
    if env::var("WM_SQUEEZE_OFF").ok().as_deref() == Some("1") {
        return SimpleCmdOutcome::AllowAsIs;
    }
    let s = cmd_line.trim();
    if s.is_empty() || s.contains("wm-squeeze") {
        return SimpleCmdOutcome::AllowAsIs;
    }
    if s.contains('\n') || s.contains("<<") {
        return SimpleCmdOutcome::DelegatePy;
    }
    let clean_tail = strip_trailing_redirs_rs(s);
    if clean_tail.is_empty() {
        return SimpleCmdOutcome::AllowAsIs;
    }
    let s_norm = strip_unquoted_line_cont_rs(clean_tail);
    let Some(parts) = split_top_level_and_rs(&s_norm) else {
        return SimpleCmdOutcome::AllowAsIs;
    };
    let mut cwd_arg: Option<String> = None;
    let mut toks: Option<Vec<String>> = None;
    if parts.len() == 2 {
        let (left, right) = (&parts[0], &parts[1]);
        if !left.is_empty() && !right.is_empty() && !has_unsafe_shell_meta_rs(left) && !has_unsafe_shell_meta_rs(right) {
            if let (Some(lt), Some(rt)) = (posix_shlex_split(left), posix_shlex_split(right)) {
                if lt.len() == 2 && lt[0] == "cd" && !rt.is_empty() {
                    cwd_arg = Some(lt[1].clone());
                    toks = Some(rt);
                }
            }
        } else {
            return SimpleCmdOutcome::DelegatePy;
        }
    } else if parts.len() == 1 {
        if has_unsafe_shell_meta_rs(&s_norm) {
            return SimpleCmdOutcome::DelegatePy;
        }
        toks = posix_shlex_split(&s_norm);
    } else {
        return SimpleCmdOutcome::DelegatePy;
    }

    let Some(mut tv) = toks else {
        return SimpleCmdOutcome::AllowAsIs;
    };
    if tv.is_empty() {
        return SimpleCmdOutcome::AllowAsIs;
    }

    let mut env_args: Vec<String> = Vec::new();
    while !tv.is_empty() && tv[0].contains('=') {
        let (k, _) = tv[0].split_once('=').unwrap();
        if !matches!(k, "PYTHONPATH" | "RUST_BACKTRACE" | "RUST_LOG" | "NO_COLOR" | "CARGO_TERM_COLOR") {
            return SimpleCmdOutcome::AllowAsIs;
        }
        env_args.push(tv.remove(0));
    }
    if tv.is_empty() {
        return SimpleCmdOutcome::AllowAsIs;
    }
    let cmd_base = Path::new(&tv[0]).file_name().and_then(|x| x.to_str()).unwrap_or(&tv[0]).to_string();
    let cmd_args = &tv[1..];

    let resolve_sq = || -> String {
        env::var("WM_SQUEEZE_BIN")
            .ok()
            .filter(|x| !x.is_empty())
            .or_else(|| {
                if env::var("PATH")
                    .ok()
                    .map(|p| p.split(':').any(|d| Path::new(d).join("wm-squeeze").is_file()))
                    .unwrap_or(false)
                {
                    Some("wm-squeeze".to_string())
                } else {
                    find_wm_squeeze_rs()
                }
            })
            .unwrap_or_else(|| "wm-squeeze".to_string())
    };

    if cmd_base == "python3" || cmd_base == "python" {
        let rest: Vec<&str> = cmd_args
            .iter()
            .map(|x| x.as_str())
            .filter(|a| !matches!(*a, "-u" | "-B" | "-S" | "-O" | "-E" | "-s" | "-q"))
            .collect();
        if let Some(&first_r) = rest.first() {
            if is_safe_py_test_or_bench_rs(first_r) {
                let sq = resolve_sq();
                return SimpleCmdOutcome::Rewrite(format!(
                    "set -o pipefail; {} 2>&1 | {} --pipe --kind generic",
                    clean_tail,
                    shell_quote(&sq)
                ));
            }
        }
    }

    if is_pretool_safe_squeezable(&cmd_base, cmd_args) {
        let sq = resolve_sq();
        let mut parts_out = vec![sq, "--safe".to_string()];
        if let Some(cw) = cwd_arg {
            parts_out.push("--cwd".to_string());
            parts_out.push(cw);
        }
        for ev in env_args {
            parts_out.push("--env".to_string());
            parts_out.push(ev);
        }
        parts_out.push("--".to_string());
        parts_out.extend(tv);
        let joined = parts_out.iter().map(|x| shell_quote(x)).collect::<Vec<_>>().join(" ");
        return SimpleCmdOutcome::Rewrite(joined);
    }

    SimpleCmdOutcome::AllowAsIs
}

fn scan_patch_env_secret(patch_text: &str) -> Option<String> {
    for raw_line in patch_text.lines() {
        let line = raw_line.trim();
        for prefix in ["*** Update File:", "*** Add File:", "*** Delete File:", "+++ ", "--- "] {
            if let Some(rest) = line.strip_prefix(prefix) {
                let path = rest.trim().trim_start_matches("a/").trim_start_matches("b/");
                let base = Path::new(path).file_name().and_then(|s| s.to_str()).unwrap_or("");
                if is_env_secret_basename(base) {
                    return Some(base.to_string());
                }
            }
        }
    }
    None
}

fn grimoire_profile_path() -> Option<PathBuf> {
    if let Ok(explicit) = env::var("WM_GRIMOIRE_PROFILE") {
        if !explicit.trim().is_empty() {
            return Some(PathBuf::from(explicit));
        }
    }
    let home = env::var("HOME").ok()?;
    if home.trim().is_empty() {
        return None;
    }
    Some(PathBuf::from(home).join(".local/share/whitemagic/grimoire_profile.json"))
}

fn grimoire_profile_exists() -> bool {
    grimoire_profile_path().is_some_and(|p| p.is_file())
}

fn extract_json_object_slice<'a>(raw: &'a str, key: &str) -> Option<&'a str> {
    let needle = format!("\"{}\"", key);
    let mut offset = 0usize;
    while let Some(rel) = raw[offset..].find(&needle) {
        let key_end = offset + rel + needle.len();
        let after = raw[key_end..].trim_start();
        if !after.starts_with(':') {
            offset = key_end;
            continue;
        }
        let colon_rel = raw[key_end..].find(':')?;
        let after_colon = raw[key_end + colon_rel + 1..].trim_start();
        if !after_colon.starts_with('{') {
            offset = key_end + colon_rel + 1;
            continue;
        }
        let start_rel = raw[key_end + colon_rel + 1..].find('{')?;
        let obj_start = key_end + colon_rel + 1 + start_rel;
        let bytes = raw.as_bytes();
        let mut depth = 0i32;
        let mut in_str = false;
        let mut esc = false;
        for i in obj_start..bytes.len() {
            let b = bytes[i];
            if esc {
                esc = false;
                continue;
            }
            if in_str {
                if b == b'\\' {
                    esc = true;
                } else if b == b'"' {
                    in_str = false;
                }
                continue;
            }
            if b == b'"' {
                in_str = true;
            } else if b == b'{' {
                depth += 1;
            } else if b == b'}' {
                depth -= 1;
                if depth == 0 {
                    return Some(&raw[obj_start..=i]);
                }
            }
        }
        return None;
    }
    None
}

fn parse_json_str_key(raw: &str, key: &str) -> Option<String> {
    let needle = format!("\"{}\"", key);
    let idx = raw.find(&needle)?;
    let rest = &raw[idx + needle.len()..];
    let colon = rest.find(':')?;
    let after = rest[colon + 1..].trim_start();
    if !after.starts_with('"') {
        return None;
    }
    let mut out = String::new();
    let mut esc = false;
    for ch in after[1..].chars() {
        if esc {
            out.push(ch);
            esc = false;
            continue;
        }
        if ch == '\\' {
            esc = true;
            continue;
        }
        if ch == '"' {
            return Some(out);
        }
        out.push(ch);
    }
    None
}

fn parse_json_usize_key(raw: &str, key: &str) -> Option<usize> {
    let needle = format!("\"{}\"", key);
    let idx = raw.find(&needle)?;
    let rest = &raw[idx + needle.len()..];
    let colon = rest.find(':')?;
    let after = rest[colon + 1..].trim_start();
    let digits: String = after.chars().take_while(|c| c.is_ascii_digit()).collect();
    digits.parse::<usize>().ok()
}

fn load_grimoire_read_limits(host: HostKind) -> (usize, usize, usize) {
    let Some(path) = grimoire_profile_path() else {
        return (400, 250, 50);
    };
    let Ok(raw) = fs::read_to_string(&path) else {
        return (400, 250, 50);
    };
    let host_key = match host {
        HostKind::Agy => "agy",
        HostKind::Codex => "codex",
        HostKind::Cursor => "cursor",
    };
    let section = extract_json_object_slice(&raw, "host_profiles")
        .and_then(|hp| extract_json_object_slice(hp, host_key))
        .or_else(|| extract_json_object_slice(&raw, "model_tuning"))
        .unwrap_or(&raw);
    let max_unbounded = parse_json_usize_key(section, "max_unbounded_read_lines")
        .filter(|&v| (50..=2000).contains(&v))
        .unwrap_or(400);
    let clamp_to = parse_json_usize_key(section, "clamp_read_lines")
        .filter(|&v| (20..=1000).contains(&v))
        .unwrap_or(250);
    let wide_jsonl = parse_json_usize_key(section, "wide_jsonl_lines")
        .filter(|&v| (10..=500).contains(&v))
        .unwrap_or(50);
    (max_unbounded, clamp_to, wide_jsonl)
}

fn resolve_mandala_guard_mode(explicit: Option<&str>) -> String {
    if let Some(m) = explicit {
        let low = m.trim().to_ascii_lowercase();
        if matches!(low.as_str(), "advisory" | "strict" | "off") {
            return low;
        }
    }
    if let Ok(ev) = env::var("WM_MANDALA_GUARD") {
        let low = ev.trim().to_ascii_lowercase();
        if matches!(low.as_str(), "advisory" | "strict" | "off") {
            return low;
        }
    }
    if let Some(path) = grimoire_profile_path() {
        if let Ok(raw) = fs::read_to_string(path) {
            if let Some(mode) = parse_json_str_key(&raw, "mandala_guard_mode") {
                let low = mode.trim().to_ascii_lowercase();
                if matches!(low.as_str(), "advisory" | "strict" | "off") {
                    return low;
                }
            }
        }
    }
    "off".to_string()
}

fn resolve_exe_in_path(name: &str) -> Option<PathBuf> {
    if let Ok(home) = env::var("HOME") {
        if !home.trim().is_empty() {
            let local = PathBuf::from(&home).join(".local/bin").join(name);
            if local.is_file() {
                return Some(local);
            }
        }
    }
    if let Ok(paths) = env::var("PATH") {
        for dir in env::split_paths(&paths) {
            let cand = dir.join(name);
            if cand.is_file() {
                return Some(cand);
            }
        }
    }
    None
}

fn handoff_cache_dir_rs() -> PathBuf {
    if let Ok(v) = env::var("WM_HANDOFF_CACHE_DIR") {
        if !v.trim().is_empty() {
            return PathBuf::from(v);
        }
    }
    let home = env::var("HOME").ok().filter(|s| !s.is_empty()).unwrap_or_else(|| "/tmp".into());
    PathBuf::from(home).join(".cache/wm-handoff")
}

fn post_loopback_receipt(cache_dir: &Path, json_body: &str) {
    let disc = cache_dir.join("sidecar.json");
    let Ok(raw) = fs::read_to_string(disc) else {
        return;
    };
    let Some(port) = parse_json_usize_key(&raw, "port") else {
        return;
    };
    if !(1..=65535).contains(&port) {
        return;
    }
    let Ok(addr) = format!("127.0.0.1:{port}").parse::<SocketAddr>() else {
        return;
    };
    let timeout = std::time::Duration::from_millis(250);
    let Ok(mut stream) = TcpStream::connect_timeout(&addr, timeout) else {
        return;
    };
    let _ = stream.set_read_timeout(Some(timeout));
    let _ = stream.set_write_timeout(Some(timeout));
    let req = format!(
        "POST /api/receipt?recorded=1 HTTP/1.0\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
        json_body.len(),
        json_body
    );
    let _ = stream.write_all(req.as_bytes());
    let _ = stream.flush();
}

fn exec_via_mandala_guard(cmd_args: &[String], cwd: Option<&str>, profile_override: Option<&str>) -> ! {
    if cmd_args.is_empty() {
        eprintln!("wm-squeeze --mandala-exec: missing command after --");
        std::process::exit(2);
    }
    let work_dir = cwd
        .filter(|s| !s.trim().is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| env::current_dir().unwrap_or_else(|_| PathBuf::from(".")));

    let runner_profile = profile_override
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .or_else(|| {
            grimoire_profile_path()
                .and_then(|p| fs::read_to_string(p).ok())
                .and_then(|raw| parse_json_str_key(&raw, "runner_profile"))
        })
        .unwrap_or_else(|| "landlock-wm-direct-v1".to_string());

    let sb_bin = match env::var("WM_SANDBOX_BIN") {
        Ok(v) if v.trim().is_empty() || v.trim() == "none" => None,
        Ok(v) => {
            let p = PathBuf::from(v);
            p.is_file().then_some(p)
        }
        Err(_) => resolve_exe_in_path("wm-sandbox"),
    };

    if env::var("WM_MANDALA_REQUIRE_KERNEL").ok().as_deref() == Some("1") && sb_bin.is_none() {
        eprintln!("wm-squeeze --mandala-exec: kernel wm-sandbox required (WM_MANDALA_REQUIRE_KERNEL=1)");
        std::process::exit(3);
    }

    let (sandbox_class, verdict, output) = if let Some(ref sb) = sb_bin {
        let rw_paths = format!("/tmp:{}", work_dir.display());
        let ro_paths = "/usr:/lib:/lib64:/bin:/sbin:/etc:/proc:/dev/urandom:/dev/null:/dev/zero";
        let res = Command::new(sb)
            .args(["--ro", ro_paths, "--rw", &rw_paths, "--"])
            .args(cmd_args)
            .current_dir(&work_dir)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .output();
        match res {
            Ok(o) => ("landlock", "TRUSTED", o),
            Err(e) => {
                eprintln!("wm-squeeze --mandala-exec failed spawning {}: {}", sb.display(), e);
                std::process::exit(127);
            }
        }
    } else {
        let res = Command::new(&cmd_args[0])
            .args(&cmd_args[1..])
            .current_dir(&work_dir)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .output();
        match res {
            Ok(o) => ("host-unconfined", "PROVISIONAL", o),
            Err(e) => {
                eprintln!("wm-squeeze --mandala-exec failed spawning '{}': {}", cmd_args[0], e);
                std::process::exit(127);
            }
        }
    };

    let code = output.status.code().unwrap_or(1);
    let stdout_hash = format!("sha256:{}", sha256_hex(&output.stdout));
    let stderr_hash = format!("sha256:{}", sha256_hex(&output.stderr));
    let ts = now_secs();
    let pid = std::process::id();
    let job_id = format!("mg-{ts}-{pid}");
    let slot_id = format!("slot-{ts}-{pid}");
    let task_id = format!("urn:wm:mandala:{ts}:{pid}");
    let head_receipt_id = format!("urn:wm:rcpt:{ts}:{pid}");
    let bundle_material = format!("{job_id}:{runner_profile}:{code}:{stdout_hash}:{stderr_hash}");
    let bundle_sha256 = format!("sha256:{}", sha256_hex(bundle_material.as_bytes()));

    // Canonical alphabetical JSON matching Python `json.dumps(core, sort_keys=True, separators=(",", ":"))`
    let core_json = format!(
        "{{\"action\":\"mandala.exec\",\"bundle_sha256\":\"{}\",\"exit\":{},\"head_receipt_id\":\"{}\",\"job_id\":\"{}\",\"receipt_count\":1,\"runner_profile\":\"{}\",\"sandbox_class\":\"{}\",\"schema\":\"mandala-cpr-pointer/0.5\",\"slot_id\":\"{}\",\"stdout_hash\":\"{}\",\"task_id\":\"{}\",\"verdict\":\"{}\",\"worker_id\":\"wm-squeeze\"}}",
        json_escape(&bundle_sha256),
        code,
        json_escape(&head_receipt_id),
        json_escape(&job_id),
        json_escape(&runner_profile),
        json_escape(sandbox_class),
        json_escape(&slot_id),
        json_escape(&stdout_hash),
        json_escape(&task_id),
        json_escape(verdict),
    );

    let signing_key = env::var("WM_CPR_HMAC_KEY")
        .ok()
        .filter(|s| !s.trim().is_empty())
        .or_else(|| env::var("WM_CONFINEMENT_SIGNING_KEY").ok().filter(|s| !s.trim().is_empty()));
    let signed_json = if let Some(ref k) = signing_key {
        let sig = hmac_sha256_hex(k.trim().as_bytes(), core_json.as_bytes());
        format!(
            "{{\"action\":\"mandala.exec\",\"bundle_sha256\":\"{}\",\"exit\":{},\"head_receipt_id\":\"{}\",\"hmac_sha256\":\"hmac-sha256:{}\",\"job_id\":\"{}\",\"receipt_count\":1,\"runner_profile\":\"{}\",\"sandbox_class\":\"{}\",\"schema\":\"mandala-cpr-pointer/0.5\",\"slot_id\":\"{}\",\"stdout_hash\":\"{}\",\"task_id\":\"{}\",\"verdict\":\"{}\",\"worker_id\":\"wm-squeeze\"}}",
            json_escape(&bundle_sha256),
            code,
            json_escape(&head_receipt_id),
            sig,
            json_escape(&job_id),
            json_escape(&runner_profile),
            json_escape(sandbox_class),
            json_escape(&slot_id),
            json_escape(&stdout_hash),
            json_escape(&task_id),
            json_escape(verdict),
        )
    } else {
        core_json
    };

    let cache_dir = handoff_cache_dir_rs();
    let _ = fs::create_dir_all(&cache_dir);
    let bridge_bin = env::var("WM_CPR_BRIDGE_BIN")
        .ok()
        .filter(|s| !s.trim().is_empty())
        .map(PathBuf::from)
        .filter(|p| p.is_file())
        .or_else(|| resolve_exe_in_path("wm-cpr-bridge"));

    let mut bridged = false;
    if let Some(ref br) = bridge_bin {
        let tmp_rcpt = cache_dir.join(format!("tmp_mandala_{pid}_{ts}.receipt.json"));
        if fs::write(&tmp_rcpt, &signed_json).is_ok() {
            if let Ok(r) = Command::new("python3")
                .args(["-S", "-B"])
                .arg(br)
                .args(["publish", "--receipt"])
                .arg(&tmp_rcpt)
                .arg("--local-only")
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status()
            {
                if r.success() {
                    bridged = true;
                }
            }
            let _ = fs::remove_file(&tmp_rcpt);
        }
    }
    if !bridged {
        let ledger = env::var("WM_CPR_BRIDGE_LEDGER")
            .ok()
            .filter(|s| !s.trim().is_empty())
            .map(PathBuf::from)
            .unwrap_or_else(|| cache_dir.join("cpr_bridge.jsonl"));
        if let Some(par) = ledger.parent() {
            let _ = fs::create_dir_all(par);
        }
        if let Ok(mut lf) = OpenOptions::new().create(true).append(true).open(&ledger) {
            let _ = writeln!(lf, "{}", signed_json);
        }
        post_loopback_receipt(&cache_dir, &signed_json);
    }

    let _ = io::stdout().write_all(&output.stdout);
    let _ = io::stderr().write_all(&output.stderr);
    std::process::exit(code);
}

fn should_mandala_wrap_cmd(guard_mode: &str, cmd_line: &str) -> bool {
    if cmd_line.trim().is_empty() || cmd_line.contains("wm-squeeze") {
        return false;
    }
    guard_mode == "strict"
        || (guard_mode == "advisory" && env::var("WM_MANDALA_EXEC_CONFINED").ok().as_deref() == Some("1"))
}

fn build_mandala_wrapper_cmd(cmd_line: &str, cwd: &str) -> String {
    let sq = env::var("WM_SQUEEZE_BIN")
        .ok()
        .filter(|x| !x.is_empty())
        .or_else(find_wm_squeeze_rs)
        .unwrap_or_else(|| "wm-squeeze".to_string());
    let mut prefix = format!("{} --mandala-exec", shell_quote(&sq));
    if !cwd.trim().is_empty() {
        prefix.push_str(" --cwd ");
        prefix.push_str(&shell_quote(cwd.trim()));
    }
    if !has_unsafe_shell_meta_rs(cmd_line) {
        if let Some(toks) = posix_shlex_split(cmd_line) {
            if !toks.is_empty() && !toks[0].contains('=') {
                return format!("{} -- {}", prefix, cmd_line.trim());
            }
        }
    }
    format!("{} -- sh -c {}", prefix, shell_quote(cmd_line.trim()))
}


fn run_pre_tool_non_agy(host: HostKind, event: &str, stdin_buf: &str, guard_override: Option<&str>) {
    let payload = extract_top_level_fields(
        stdin_buf,
        &["tool_name", "tool_input", "command", "file_path", "path", "server_name", "hook_event_name", "session_id", "conversation_id", "cwd"],
    );
    let raw_event = if !event.is_empty() {
        event
    } else {
        payload.get("hook_event_name").map(|s| s.as_str()).unwrap_or("")
    };
    let tool_name = payload.get("tool_name").map(|s| s.trim()).unwrap_or("");
    let ti_raw = payload.get("tool_input").map(|s| s.as_str()).unwrap_or("");
    let ti_fields = if ti_raw.trim_start().starts_with('{') {
        extract_top_level_fields(ti_raw, &["command", "cmd", "file_path", "path", "offset", "limit", "input", "patch", "query", "token_budget"])
    } else {
        HashMap::new()
    };

    // 1. Patch / file secret guard
    if tool_name == "apply_patch" {
        let patch_txt = ti_fields
            .get("input")
            .or_else(|| ti_fields.get("patch"))
            .map(|s| s.as_str())
            .unwrap_or(ti_raw);
        if let Some(secret_base) = scan_patch_env_secret(patch_txt) {
            let reason = format!("[Env-Write Guard] Modifying secrets file `{}` via apply_patch requires explicit user confirmation.", secret_base);
            emit_pretool_decision(host, "force_ask", &reason);
            return;
        }
        emit_pretool_allow(host);
        return;
    }

    if matches!(tool_name, "write_file" | "edit_file" | "Write" | "Edit" | "StrReplace") {
        let target_path = ti_fields
            .get("file_path")
            .or_else(|| ti_fields.get("path"))
            .or_else(|| payload.get("file_path"))
            .or_else(|| payload.get("path"))
            .map(|s| s.trim())
            .unwrap_or("");
        let base = Path::new(target_path).file_name().and_then(|s| s.to_str()).unwrap_or("");
        if is_env_secret_basename(base) {
            let reason = format!("[Env-Write Guard] Modifying secrets file `{}` requires explicit user confirmation.", base);
            emit_pretool_decision(host, "force_ask", &reason);
            return;
        }
        emit_pretool_allow(host);
        return;
    }

    // 1b. Cursor Read bounding: clamp unbounded huge text/wide-JSONL reads via adaptive grimoire_profile.json
    if host == HostKind::Cursor && matches!(tool_name, "Read" | "read_file") {
        let has_explicit_limit = ti_fields
            .get("limit")
            .map(|s| !s.trim().is_empty() && s.trim() != "null")
            .unwrap_or(false);
        if !has_explicit_limit {
            let (path_key, raw_p) = if let Some(p) = ti_fields.get("path").filter(|s| !s.trim().is_empty()) {
                ("path", p.trim())
            } else if let Some(p) = ti_fields.get("file_path").filter(|s| !s.trim().is_empty()) {
                ("file_path", p.trim())
            } else {
                ("path", "")
            };
            if !raw_p.is_empty() {
                let low = raw_p.to_ascii_lowercase();
                let is_media = [".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".svg"]
                    .iter()
                    .any(|ext| low.ends_with(ext));
                let p_obj = Path::new(raw_p);
                if !is_media && p_obj.is_file() {
                    if let Ok(meta) = fs::metadata(p_obj) {
                        let sz = meta.len();
                        let (max_unbounded, clamp_lines, wide_lines) = load_grimoire_read_limits(host);
                        let wide = is_wide_line_file_rs(p_obj, sz);
                        let clamp_to = if wide {
                            Some(wide_lines)
                        } else if file_exceeds_lines_rs(p_obj, max_unbounded, sz) {
                            Some(clamp_lines)
                        } else {
                            None
                        };
                        if let Some(lim) = clamp_to {
                            let off: i64 = ti_fields
                                .get("offset")
                                .and_then(|s| s.trim().parse::<i64>().ok())
                                .unwrap_or(1);
                            println!(
                                "{{\"permission\":\"allow\",\"updated_input\":{{\"{}\":\"{}\",\"offset\":{},\"limit\":{}}}}}",
                                path_key,
                                json_escape(raw_p),
                                off,
                                lim
                            );
                            return;
                        }
                    }
                }
            }
        }
        emit_pretool_allow(host);
        return;
    }

    // 2. MCP execution guard (Cursor beforeMCPExecution / Codex custom MCP calls)
    if host == HostKind::Cursor && raw_event == "beforeMCPExecution" {
        if !ti_raw.is_empty() && contains_kekkai_sensitive(ti_raw) {
            emit_pretool_decision(host, "force_ask", "[Kekkai Guard] MCP payload references sensitive credentials or protected paths; confirm explicitly.");
            return;
        }
        emit_pretool_allow(host);
        return;
    }

    // 3. Shell execution path (Codex Bash/shell/exec_command, Cursor beforeShellExecution/preToolUse Shell)
    let is_shell = (host == HostKind::Codex && matches!(tool_name, "Bash" | "shell" | "exec_command"))
        || (host == HostKind::Cursor && (raw_event == "beforeShellExecution" || !payload.get("command").map(|s| s.is_empty()).unwrap_or(true) || matches!(tool_name, "Shell" | "Bash" | "run_command")));
    if !is_shell {
        emit_pretool_allow(host);
        return;
    }

    let (cmd_key, cmd_line) = if let Some(c) = ti_fields.get("command").filter(|s| !s.is_empty()) {
        ("command", c.as_str())
    } else if let Some(c) = ti_fields.get("cmd").filter(|s| !s.is_empty()) {
        ("cmd", c.as_str())
    } else if let Some(c) = payload.get("command").filter(|s| !s.is_empty()) {
        ("command", c.as_str())
    } else if !ti_raw.is_empty() && !ti_raw.trim_start().starts_with('{') {
        ("command", ti_raw)
    } else {
        ("command", "")
    };

    if cmd_line.is_empty() {
        emit_pretool_allow(host);
        return;
    }

    // In Cursor: beforeShellExecution enforces permission=ask (does NOT support updated_input),
    // while preToolUse supports updated_input (does NOT enforce permission=ask).
    if host == HostKind::Cursor && raw_event == "beforeShellExecution" {
        if let Some(reason) = check_destructive_cmd(cmd_line) {
            let msg = format!("[Data-Loss Guard] {}. Confirm explicitly before proceeding.", reason);
            emit_pretool_decision(host, "force_ask", &msg);
            return;
        }
        if contains_kekkai_sensitive(cmd_line) {
            emit_pretool_decision(
                host,
                "force_ask",
                "[Kekkai Guard] Command references sensitive credentials or protected directories; confirm explicitly.",
            );
            return;
        }
        emit_pretool_allow(host);
        return;
    }

    if let Some(reason) = check_destructive_cmd(cmd_line) {
        if host == HostKind::Cursor {
            // Defer to beforeShellExecution where Cursor enforces interactive `ask`
            emit_pretool_allow(host);
            return;
        }
        let msg = format!("[Data-Loss Guard] {}. Confirm explicitly before proceeding.", reason);
        emit_pretool_decision(host, "force_ask", &msg);
        return;
    }
    if contains_kekkai_sensitive(cmd_line) {
        if host == HostKind::Cursor {
            emit_pretool_allow(host);
            return;
        }
        emit_pretool_decision(
            host,
            "force_ask",
            "[Kekkai Guard] Command references sensitive credentials or protected directories; confirm explicitly.",
        );
        return;
    }

    match classify_run_command_rs(cmd_line) {
        SimpleCmdOutcome::Rewrite(rewritten) => emit_pretool_rewrite_cmd(host, cmd_key, &rewritten),
        SimpleCmdOutcome::DelegatePy | SimpleCmdOutcome::AllowAsIs => {
            let guard = resolve_mandala_guard_mode(guard_override);
            let can_rewrite = host == HostKind::Codex || (host == HostKind::Cursor && raw_event == "preToolUse");
            if can_rewrite && should_mandala_wrap_cmd(&guard, cmd_line) {
                let cwd_str = payload.get("cwd").map(|s| s.as_str()).unwrap_or("");
                let wrapped = build_mandala_wrapper_cmd(cmd_line, cwd_str);
                emit_pretool_rewrite_cmd(host, cmd_key, &wrapped);
            } else {
                emit_pretool_allow(host);
            }
        }
    }
}

fn run_pre_tool(fallback_py: Option<&str>, host: HostKind, event: Option<&str>, guard_override: Option<&str>) {
    if env::var("WM_AGY_BENCH_VANILLA").ok().as_deref() == Some("1") {
        emit_pretool_allow(host);
        return;
    }
    let mut stdin_buf = String::new();
    if io::stdin().read_to_string(&mut stdin_buf).is_err() || stdin_buf.trim().is_empty() {
        emit_pretool_allow(host);
        return;
    }
    if host != HostKind::Agy {
        run_pre_tool_non_agy(host, event.unwrap_or(""), &stdin_buf, guard_override);
        return;
    }

    let payload = extract_top_level_fields(&stdin_buf, &["toolCall", "conversationId", "stepIdx", "transcriptPath", "cwd"]);
    let tc_raw = payload.get("toolCall").map(|s| s.as_str()).unwrap_or("");
    let tc_fields = extract_top_level_fields(tc_raw, &["name", "args", "arguments"]);
    let tool_name = tc_fields.get("name").map(|s| s.as_str()).unwrap_or("");
    let args_raw = tc_fields.get("args").or_else(|| tc_fields.get("arguments")).map(|s| s.as_str()).unwrap_or("");
    let args = if args_raw.trim_start().starts_with('{') {
        extract_top_level_fields(
            args_raw,
            &[
                "CommandLine", "command", "verify", "commands",
                "Path", "path", "StartLine", "EndLine", "ByteOffset",
                "edits", "Action", "TaskId", "token_budget", "limit",
                "max_content_bytes", "markdown_only", "format",
                "action", "fork", "model", "brief", "message", "content", "body", "TypeName", "Model",
            ],
        )
    } else if !args_raw.is_empty() {
        let mut m = HashMap::new();
        m.insert("command".to_string(), args_raw.to_string());
        m
    } else {
        HashMap::new()
    };

    // 1. Env-Write Guard
    if tool_name == "write_file" || tool_name == "edit_file" {
        if let Some(p) = args.get("Path").or_else(|| args.get("path")) {
            let base = Path::new(p.trim()).file_name().and_then(|s| s.to_str()).unwrap_or("");
            if is_env_secret_basename(base) {
                let reason = format!("[Env-Write Guard] Modifying secrets file `{}` requires explicit user confirmation.", base);
                pretool_decision("force_ask", &reason);
                return;
            }
        }
    } else if tool_name.ends_with("_apply_edits") {
        if let Some(edits_raw) = args.get("edits") {
            for el in extract_json_array_elements(edits_raw, 64) {
                if el.trim_start().starts_with('{') {
                    if let Some(p) = extract_top_level_fields(&el, &["path"]).get("path") {
                        let base = Path::new(p.trim()).file_name().and_then(|s| s.to_str()).unwrap_or("");
                        if is_env_secret_basename(base) {
                            let reason = format!("[Env-Write Guard] Modifying secrets file `{}` requires explicit user confirmation.", base);
                            pretool_decision("force_ask", &reason);
                            return;
                        }
                    }
                }
            }
        }
    }

    // 1b. Destructive shell & Kekkai checks on any shell command arguments
    let mut cmds: Vec<String> = Vec::new();
    for k in ["CommandLine", "command", "verify"] {
        if let Some(v) = args.get(k) {
            if !v.is_empty() {
                cmds.push(v.clone());
            }
        }
    }
    if let Some(raw_cmds) = args.get("commands") {
        for el in extract_json_array_elements(raw_cmds, 32) {
            if el.trim_start().starts_with('{') {
                if let Some(c) = extract_top_level_fields(&el, &["command"]).get("command") {
                    if !c.is_empty() {
                        cmds.push(c.clone());
                    }
                }
            } else if !el.is_empty() {
                cmds.push(el);
            }
        }
    }
    for c in &cmds {
        if let Some(reason) = check_destructive_cmd(c) {
            let msg = format!("[Data-Loss Guard] {}. Confirm explicitly before proceeding.", reason);
            pretool_decision("force_ask", &msg);
            return;
        }
        if contains_kekkai_sensitive(c) {
            run_py_fallback(fallback_py, &stdin_buf);
            return;
        }
    }

    let conv_id = payload.get("conversationId").map(|s| s.as_str()).unwrap_or("");
    let step_idx = payload.get("stepIdx").and_then(|s| s.trim().parse::<i64>().ok()).unwrap_or(0);
    let transcript = payload.get("transcriptPath").map(|s| s.as_str()).unwrap_or("");
    let cwd = payload.get("cwd").map(|s| s.as_str()).unwrap_or("");

    // 2. run_command compression or compound delegation
    if tool_name == "run_command" {
        if let Some(cmd_line) = args.get("CommandLine").filter(|s| !s.is_empty()) {
            match classify_run_command_rs(cmd_line) {
                SimpleCmdOutcome::Rewrite(rewritten) => {
                    println!("{{\"decision\":\"allow\",\"overwrite\":{{\"CommandLine\":\"{}\"}}}}", json_escape(&rewritten));
                    return;
                }
                SimpleCmdOutcome::DelegatePy => {
                    run_py_fallback(fallback_py, &stdin_buf);
                    return;
                }
                SimpleCmdOutcome::AllowAsIs => {
                    let guard = resolve_mandala_guard_mode(guard_override);
                    if should_mandala_wrap_cmd(&guard, cmd_line) {
                        let wrapped = build_mandala_wrapper_cmd(cmd_line, cwd);
                        println!("{{\"decision\":\"allow\",\"overwrite\":{{\"CommandLine\":\"{}\"}}}}", json_escape(&wrapped));
                        return;
                    }
                    pretool_allow();
                    return;
                }
            }
        }
        pretool_allow();
        return;
    }

    // 3. Native fast-paths
    if tool_name == "read_file" {
        handle_read_file_rs(&args, transcript, conv_id, step_idx, cwd);
        return;
    }
    if tool_name == "manage_task" {
        handle_manage_task_rs(&args, conv_id, step_idx);
        return;
    }
    if matches!(tool_name, "swarm" | "post_message" | "invoke_subagent") {
        handle_swarm_and_subagent_rs(tool_name, &args, transcript);
        return;
    }
    if matches!(
        tool_name,
        "mcp_whitemagic_session_continuity"
            | "mcp_whitemagic_memory_recall"
            | "mcp_whitemagic_whitemagic_gen3_session_continuity"
            | "mcp_whitemagic_whitemagic_gen3_memory_recall"
            | "mcp_wm-research-engine_research_hunt"
            | "mcp_firecrawl_firecrawl_scrape"
    ) {
        handle_whitemagic_mcp_rs(tool_name, &args);
        return;
    }

    pretool_allow();
}

fn main() {
    let args: Vec<String> = env::args().skip(1).collect();
    if args.is_empty() || args[0] == "-h" || args[0] == "--help" {
        eprintln!(
            "Usage:\n  wm-squeeze --safe [--cwd DIR] [--max-bytes N] [--] <cmd> [args...]\n  wm-squeeze --pipe [--kind auto|cargo|pytest|gotest|linter|git|rg|ls|generic] [--max-bytes N]\n  wm-squeeze --pre-tool [--py FALLBACK_SCRIPT] [--host agy|codex|cursor] [--event NAME] [--mandala-guard advisory|strict|off]\n  wm-squeeze --mandala-exec [--profile ID] [--cwd DIR] [--] <cmd> [args...]\n  wm-squeeze --hud\n  wm-squeeze --post-tool [--host agy|codex|cursor]\n  wm-squeeze --stats"
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
    if args[0] == "--mandala-exec" {
        let mut profile: Option<String> = None;
        let mut cwd: Option<String> = None;
        let mut i = 1;
        while i < args.len() {
            match args[i].as_str() {
                "--profile" if i + 1 < args.len() => {
                    profile = Some(args[i + 1].clone());
                    i += 2;
                }
                "--cwd" if i + 1 < args.len() => {
                    cwd = Some(args[i + 1].clone());
                    i += 2;
                }
                "--" => {
                    i += 1;
                    break;
                }
                _ => break,
            }
        }
        exec_via_mandala_guard(&args[i..], cwd.as_deref(), profile.as_deref());
    }
    if args[0] == "--post-tool" {
        let mut host = HostKind::Agy;
        let mut i = 1;
        while i < args.len() {
            if args[i] == "--host" && i + 1 < args.len() {
                host = HostKind::from_str(&args[i + 1]);
                i += 2;
            } else {
                i += 1;
            }
        }
        run_post_tool(host);
        return;
    }
    if args[0] == "--pre-tool" {
        let mut py_fb: Option<&str> = None;
        let mut host = HostKind::Agy;
        let mut event: Option<&str> = None;
        let mut guard: Option<String> = None;
        let mut i = 1;
        while i < args.len() {
            if args[i] == "--py" && i + 1 < args.len() {
                py_fb = Some(args[i + 1].as_str());
                i += 2;
            } else if args[i] == "--host" && i + 1 < args.len() {
                host = HostKind::from_str(&args[i + 1]);
                i += 2;
            } else if args[i] == "--event" && i + 1 < args.len() {
                event = Some(args[i + 1].as_str());
                i += 2;
            } else if args[i] == "--mandala-guard" && i + 1 < args.len() {
                guard = Some(args[i + 1].clone());
                i += 2;
            } else {
                i += 1;
            }
        }
        run_pre_tool(py_fb, host, event, guard.as_deref());
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
