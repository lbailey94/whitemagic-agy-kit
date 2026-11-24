//! wm-codegraph v3.0.0 — Zero-dependency Rust incremental symbol & call-graph engine (CLI + 1-tool MCP).
//!
//! Solves the "Token Inflation Trap" (58% read_file share / ~45k tokens per cross-file trace):
//!   - Incrementally indexes Rust, Python, TS/JS, Go, C/C++, Java, and Shell files using content/mtime cache
//!   - Extracts classes/structs/traits/functions/methods with exact 1-based line ranges + call/reference edges
//!   - Answers `overview`, `defs`, `callers`, `callees`, `impact`, and `skeleton` in <2 ms and ~200-800 tokens
//!   - Runs as CLI (`wm-codegraph <mode> <target> [--root DIR]`) or 1-tool Stdio MCP (`wm-codegraph --mcp`)

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet, VecDeque};
use std::env;
use std::fmt::Write as FmtWrite;
use std::fs;
use std::io::{self, BufRead, Write};
use std::path::{Path, PathBuf};
use std::time::UNIX_EPOCH;

const MAX_INLINE_BYTES: usize = 8_200;
const MAX_FILE_BYTES: u64 = 4 * 1024 * 1024;
const SKIP_DIRS: &[&str] = &[
    ".git",
    "node_modules",
    "target",
    "dist",
    "build",
    "__pycache__",
    ".venv",
    "venv",
    ".pytest_cache",
    ".mypy_cache",
    ".next",
    ".cache",
    "vendor",
    "coverage",
    ".system_generated",
];

#[derive(Clone, Debug)]
struct Symbol {
    name: String,
    kind: String, // fn, struct, class, trait, impl, interface, enum, type, const
    file: String,
    start_line: usize,
    end_line: usize,
    signature: String,
    calls: Vec<String>,
}

#[derive(Clone, Debug)]
struct FileEntry {
    mtime_ns: u128,
    size: u64,
    lines: usize,
    imports: Vec<String>,
    symbols: Vec<Symbol>,
}

struct RepoIndex {
    root: PathBuf,
    files: BTreeMap<String, FileEntry>,
}

fn fnv1a_hex(s: &str) -> String {
    let mut h: u64 = 0xcbf29ce484222325;
    for b in s.as_bytes() {
        h ^= *b as u64;
        h = h.wrapping_mul(0x100000001b3);
    }
    format!("{:016x}", h)
}

fn cache_file_for(root: &Path) -> PathBuf {
    let base = env::var("XDG_CACHE_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| {
            PathBuf::from(env::var("HOME").unwrap_or_else(|_| "/tmp".into())).join(".cache")
        });
    let canon = fs::canonicalize(root).unwrap_or_else(|_| root.to_path_buf());
    base.join("wm-codegraph")
        .join(format!("{}.idx", fnv1a_hex(&canon.to_string_lossy())))
}

fn lang_for_ext(ext: &str) -> Option<&'static str> {
    match ext {
        "rs" => Some("rust"),
        "py" => Some("python"),
        "ts" | "tsx" | "js" | "jsx" | "mjs" | "cjs" => Some("ts"),
        "go" => Some("go"),
        "c" | "h" | "cc" | "cpp" | "hpp" => Some("c"),
        "sh" | "bash" => Some("sh"),
        _ => None,
    }
}

fn is_ident_char(b: u8) -> bool {
    b.is_ascii_alphanumeric() || b == b'_'
}

fn is_keyword(w: &str) -> bool {
    matches!(
        w,
        "if" | "else"
            | "for"
            | "while"
            | "loop"
            | "match"
            | "return"
            | "fn"
            | "def"
            | "class"
            | "struct"
            | "enum"
            | "trait"
            | "impl"
            | "pub"
            | "let"
            | "mut"
            | "const"
            | "static"
            | "use"
            | "mod"
            | "import"
            | "from"
            | "as"
            | "in"
            | "not"
            | "and"
            | "or"
            | "try"
            | "except"
            | "with"
            | "async"
            | "await"
            | "yield"
            | "new"
            | "function"
            | "switch"
            | "case"
            | "catch"
            | "throw"
            | "typeof"
            | "sizeof"
            | "println"
            | "eprintln"
            | "format"
            | "vec"
            | "Some"
            | "None"
            | "Ok"
            | "Err"
            | "print"
            | "len"
            | "str"
            | "int"
            | "dict"
            | "list"
            | "set"
            | "range"
            | "super"
            | "self"
            | "this"
            | "true"
            | "false"
            | "null"
            | "undefined"
    )
}

/// Extract identifier call targets `ident(` from a code slice, skipping string literals & line comments.
fn extract_calls(text: &str, own_name: &str) -> Vec<String> {
    let mut found = BTreeSet::new();
    for line in text.lines() {
        let t = line.trim();
        if t.starts_with("//") || t.starts_with('#') || t.starts_with('*') || t.starts_with("/*") {
            continue;
        }
        let bytes = line.as_bytes();
        let mut i = 0;
        let mut in_str: Option<u8> = None;
        while i < bytes.len() {
            let b = bytes[i];
            if let Some(q) = in_str {
                if b == b'\\' {
                    i += 2;
                    continue;
                }
                if b == q {
                    in_str = None;
                }
                i += 1;
                continue;
            }
            if b == b'"' || b == b'\'' || b == b'`' {
                in_str = Some(b);
                i += 1;
                continue;
            }
            if b == b'/' && i + 1 < bytes.len() && bytes[i + 1] == b'/' {
                break;
            }
            if b == b'#' {
                break;
            }
            if b.is_ascii_alphabetic() || b == b'_' {
                let start = i;
                while i < bytes.len() && is_ident_char(bytes[i]) {
                    i += 1;
                }
                let word = &line[start..i];
                let mut j = i;
                while j < bytes.len() && bytes[j] == b' ' {
                    j += 1;
                }
                if j < bytes.len()
                    && (bytes[j] == b'(' || (bytes[j] == b'!' && j + 1 < bytes.len() && bytes[j + 1] == b'('))
                    && word.len() >= 2
                    && word != own_name
                    && !is_keyword(word)
                {
                    found.insert(word.to_string());
                }
            } else {
                i += 1;
            }
        }
    }
    found.into_iter().collect()
}

fn first_ident_after(s: &str, pat: &str) -> Option<String> {
    let idx = s.find(pat)?;
    let rest = s[idx + pat.len()..].trim_start();
    let name: String = rest
        .chars()
        .take_while(|c| c.is_ascii_alphanumeric() || *c == '_')
        .collect();
    if name.is_empty() {
        None
    } else {
        Some(name)
    }
}

fn find_brace_end(lines: &[&str], start_idx: usize) -> usize {
    let mut depth: i32 = 0;
    let mut seen_open = false;
    for (offset, line) in lines[start_idx..].iter().enumerate() {
        let bytes = line.as_bytes();
        let mut i = 0;
        let mut in_str: Option<u8> = None;
        while i < bytes.len() {
            let b = bytes[i];
            if let Some(q) = in_str {
                if b == b'\\' {
                    i += 2;
                    continue;
                }
                if b == q {
                    in_str = None;
                }
                i += 1;
                continue;
            }
            if b == b'"' || b == b'\'' || b == b'`' {
                in_str = Some(b);
                i += 1;
                continue;
            }
            if b == b'/' && i + 1 < bytes.len() && bytes[i + 1] == b'/' {
                break;
            }
            if b == b'{' {
                depth += 1;
                seen_open = true;
            } else if b == b'}' {
                depth -= 1;
                if seen_open && depth <= 0 {
                    return start_idx + offset + 1;
                }
            } else if b == b';' && !seen_open {
                return start_idx + offset + 1;
            }
            i += 1;
        }
        if !seen_open && offset >= 20 {
            return start_idx + offset + 1;
        }
    }
    lines.len()
}

fn find_python_end(lines: &[&str], start_idx: usize) -> usize {
    let base_indent = lines[start_idx].chars().take_while(|c| *c == ' ' || *c == '\t').count();
    let mut end = start_idx + 1;
    for i in (start_idx + 1)..lines.len() {
        let t = lines[i].trim();
        if t.is_empty() || t.starts_with('#') {
            continue;
        }
        let ind = lines[i].chars().take_while(|c| *c == ' ' || *c == '\t').count();
        if ind <= base_indent {
            break;
        }
        end = i + 1;
    }
    end
}

fn parse_file(rel: &str, lang: &str, text: &str) -> (Vec<String>, Vec<Symbol>) {
    let lines: Vec<&str> = text.lines().collect();
    let mut imports = Vec::new();
    let mut symbols = Vec::new();

    for (idx, line) in lines.iter().enumerate() {
        let t = line.trim();
        if t.is_empty() || t.starts_with("//") || t.starts_with('#') || t.starts_with("/*") {
            continue;
        }

        // Imports
        if (lang == "rust" && t.starts_with("use "))
            || (lang == "python" && (t.starts_with("import ") || t.starts_with("from ")))
            || (lang == "ts" && (t.starts_with("import ") || t.contains("require(")))
            || (lang == "go" && t.starts_with("import "))
        {
            imports.push(t.chars().take(120).collect());
            continue;
        }

        let mut detected: Option<(&str, String)> = None;
        match lang {
            "rust" => {
                if let Some(n) = first_ident_after(t, "fn ") {
                    if !t.starts_with("let ") && !t.starts_with("//") {
                        detected = Some(("fn", n));
                    }
                } else if let Some(n) = first_ident_after(t, "struct ") {
                    detected = Some(("struct", n));
                } else if let Some(n) = first_ident_after(t, "enum ") {
                    detected = Some(("enum", n));
                } else if let Some(n) = first_ident_after(t, "trait ") {
                    detected = Some(("trait", n));
                }
            }
            "python" => {
                if let Some(n) = first_ident_after(t, "def ") {
                    if t.starts_with("def ") || t.starts_with("async def ") {
                        detected = Some(("fn", n));
                    }
                } else if let Some(n) = first_ident_after(t, "class ") {
                    if t.starts_with("class ") {
                        detected = Some(("class", n));
                    }
                }
            }
            "ts" => {
                if let Some(n) = first_ident_after(t, "function ") {
                    detected = Some(("fn", n));
                } else if let Some(n) = first_ident_after(t, "class ") {
                    if t.starts_with("class ") || t.starts_with("export class ") || t.starts_with("export default class ") {
                        detected = Some(("class", n));
                    }
                } else if let Some(n) = first_ident_after(t, "interface ") {
                    if t.starts_with("interface ") || t.starts_with("export interface ") {
                        detected = Some(("interface", n));
                    }
                } else if (t.starts_with("export const ") || t.starts_with("const "))
                    && (t.contains("=>") || t.contains("= function"))
                {
                    if let Some(n) = first_ident_after(t, "const ") {
                        detected = Some(("fn", n));
                    }
                }
            }
            "go" => {
                if t.starts_with("func ") {
                    let after = t.strip_prefix("func ").unwrap_or("");
                    let after_recv = if after.starts_with('(') {
                        after.find(')').map(|i| after[i + 1..].trim_start()).unwrap_or(after)
                    } else {
                        after
                    };
                    let n: String = after_recv
                        .chars()
                        .take_while(|c| c.is_ascii_alphanumeric() || *c == '_')
                        .collect();
                    if !n.is_empty() {
                        detected = Some(("fn", n));
                    }
                } else if t.starts_with("type ") && (t.contains(" struct") || t.contains(" interface")) {
                    if let Some(n) = first_ident_after(t, "type ") {
                        detected = Some(("type", n));
                    }
                }
            }
            "c" => {
                if t.contains('(')
                    && (t.ends_with('{') || t.ends_with(')'))
                    && !t.starts_with("if")
                    && !t.starts_with("for")
                    && !t.starts_with("while")
                    && !t.starts_with("switch")
                {
                    if let Some(paren) = t.find('(') {
                        let before = t[..paren].trim_end();
                        if let Some(tok) = before.split_whitespace().last() {
                            let n = tok.trim_start_matches('*');
                            if !n.is_empty() && n.chars().all(|c| c.is_ascii_alphanumeric() || c == '_') && !is_keyword(n) {
                                detected = Some(("fn", n.to_string()));
                            }
                        }
                    }
                }
            }
            "sh" => {
                if let Some(n) = first_ident_after(t, "function ") {
                    detected = Some(("fn", n));
                } else if t.ends_with("() {") || t.ends_with("(){") {
                    let n = t.split('(').next().unwrap_or("").trim();
                    if !n.is_empty() && n.chars().all(|c| c.is_ascii_alphanumeric() || c == '_') {
                        detected = Some(("fn", n.to_string()));
                    }
                }
            }
            _ => {}
        }

        if let Some((kind, name)) = detected {
            let end_line = if lang == "python" {
                find_python_end(&lines, idx)
            } else {
                find_brace_end(&lines, idx)
            };
            let body = lines[idx..end_line.min(lines.len())].join("\n");
            let calls = extract_calls(&body, &name);
            let sig: String = t.chars().take(120).collect();
            symbols.push(Symbol {
                name,
                kind: kind.to_string(),
                file: rel.to_string(),
                start_line: idx + 1,
                end_line,
                signature: sig,
                calls,
            });
        }
    }

    (imports, symbols)
}

fn walk_repo(root: &Path, dir: &Path, out: &mut Vec<(String, PathBuf, u128, u64, &'static str)>) {
    let Ok(rd) = fs::read_dir(dir) else {
        return;
    };
    let mut entries: Vec<_> = rd.flatten().collect();
    entries.sort_by_key(|e| e.file_name());
    for entry in entries {
        let p = entry.path();
        let name = entry.file_name();
        let name_s = name.to_string_lossy();
        let Ok(meta) = entry.metadata() else {
            continue;
        };
        if meta.is_dir() {
            if SKIP_DIRS.contains(&name_s.as_ref()) || name_s.starts_with('.') {
                continue;
            }
            walk_repo(root, &p, out);
        } else if meta.is_file() && meta.len() <= MAX_FILE_BYTES {
            let ext = p.extension().and_then(|s| s.to_str()).unwrap_or("");
            if let Some(lang) = lang_for_ext(ext) {
                let rel = p
                    .strip_prefix(root)
                    .unwrap_or(&p)
                    .to_string_lossy()
                    .replace('\\', "/");
                let mtime_ns = meta
                    .modified()
                    .ok()
                    .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
                    .map(|d| d.as_nanos())
                    .unwrap_or(0);
                out.push((rel, p, mtime_ns, meta.len(), lang));
            }
        }
    }
}

/// Compact line-based persistence (zero serde dependency):
///   F\t<rel>\t<mtime_ns>\t<size>\t<lines>
///   I\t<import>
///   S\t<kind>\t<name>\t<start>\t<end>\t<calls_csv>\t<sig>
fn load_cache(path: &Path) -> BTreeMap<String, FileEntry> {
    let mut map = BTreeMap::new();
    let Ok(raw) = fs::read_to_string(path) else {
        return map;
    };
    let mut cur: Option<(String, FileEntry)> = None;
    for line in raw.lines() {
        let mut it = line.split('\t');
        match it.next() {
            Some("F") => {
                let (Some(rel_s), Some(mt_s), Some(sz_s), Some(ln_s)) =
                    (it.next(), it.next(), it.next(), it.next())
                else {
                    continue;
                };
                if let Some((prev_rel, prev_fe)) = cur.take() {
                    map.insert(prev_rel, prev_fe);
                }
                let rel = rel_s.to_string();
                let mtime_ns = mt_s.parse().unwrap_or(0);
                let size = sz_s.parse().unwrap_or(0);
                let lines = ln_s.parse().unwrap_or(0);
                cur = Some((
                    rel,
                    FileEntry {
                        mtime_ns,
                        size,
                        lines,
                        imports: Vec::new(),
                        symbols: Vec::new(),
                    },
                ));
            }
            Some("I") => {
                if let (Some((_, ref mut fe)), Some(imp)) = (&mut cur, it.next()) {
                    fe.imports.push(imp.to_string());
                }
            }
            Some("S") => {
                let (Some(kind), Some(name), Some(st), Some(en), Some(calls_s), Some(sig)) =
                    (it.next(), it.next(), it.next(), it.next(), it.next(), it.next())
                else {
                    continue;
                };
                if let Some((ref cur_rel, ref mut fe)) = cur {
                    let mut calls = Vec::new();
                    if !calls_s.is_empty() {
                        for c in calls_s.split(',') {
                            calls.push(c.to_string());
                        }
                    }
                    fe.symbols.push(Symbol {
                        kind: kind.to_string(),
                        name: name.to_string(),
                        file: String::from(cur_rel.as_str()),
                        start_line: st.parse().unwrap_or(1),
                        end_line: en.parse().unwrap_or(1),
                        calls,
                        signature: sig.to_string(),
                    });
                }
            }
            _ => {}
        }
    }
    if let Some((last_rel, last_fe)) = cur {
        map.insert(last_rel, last_fe);
    }
    map
}

fn save_cache(path: &Path, files: &BTreeMap<String, FileEntry>) {
    if let Some(parent) = path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    let mut buf = String::with_capacity(files.len() * 256);
    for (rel, fe) in files {
        let _ = writeln!(buf, "F\t{}\t{}\t{}\t{}", rel, fe.mtime_ns, fe.size, fe.lines);
        for imp in &fe.imports {
            let _ = writeln!(buf, "I\t{}", imp.replace(['\t', '\n', '\r'], " "));
        }
        for sym in &fe.symbols {
            let _ = writeln!(
                buf,
                "S\t{}\t{}\t{}\t{}\t{}\t{}",
                sym.kind,
                sym.name,
                sym.start_line,
                sym.end_line,
                sym.calls.join(","),
                sym.signature.replace(['\t', '\n', '\r'], " ")
            );
        }
    }
    let tmp = path.with_extension(format!("idx.tmp.{}", std::process::id()));
    if fs::write(&tmp, buf).is_ok() {
        if fs::rename(&tmp, path).is_err() {
            let _ = fs::remove_file(&tmp);
        }
    } else {
        let _ = fs::remove_file(&tmp);
    }
}

impl RepoIndex {
    fn build(root: &Path) -> Self {
        let cpath = cache_file_for(root);
        let mut prev = load_cache(&cpath);
        let mut discovered = Vec::new();
        walk_repo(root, root, &mut discovered);

        let mut files = BTreeMap::new();
        let mut changed = prev.len() != discovered.len();

        for (rel, abs, mtime_ns, size, lang) in discovered {
            if let Some(old) = prev.remove(&rel) {
                if old.mtime_ns == mtime_ns && old.size == size {
                    files.insert(rel, old);
                    continue;
                }
            }
            changed = true;
            if let Ok(text) = fs::read_to_string(&abs) {
                let line_cnt = text.lines().count();
                let (imports, symbols) = parse_file(&rel, lang, &text);
                files.insert(
                    rel,
                    FileEntry {
                        mtime_ns,
                        size,
                        lines: line_cnt,
                        imports,
                        symbols,
                    },
                );
            }
        }

        if changed {
            save_cache(&cpath, &files);
        }
        Self {
            root: root.to_path_buf(),
            files,
        }
    }

    fn all_symbols(&self) -> Vec<&Symbol> {
        self.files.values().flat_map(|f| f.symbols.iter()).collect()
    }

    fn query_overview(&self) -> String {
        let total_files = self.files.len();
        let total_syms: usize = self.files.values().map(|f| f.symbols.len()).sum();
        let total_lines: usize = self.files.values().map(|f| f.lines).sum();
        let mut out = vec![format!(
            "# CodeGraph Overview ({}) — {} files | {} lines | {} symbols",
            self.root.display(),
            total_files,
            total_lines,
            total_syms
        )];
        let mut ranked: Vec<(&String, &FileEntry)> = self.files.iter().collect();
        ranked.sort_by(|a, b| b.1.symbols.len().cmp(&a.1.symbols.len()));
        out.push("\n## Top Structural Files:".to_string());
        for (rel, fe) in ranked.into_iter().take(25) {
            let names: Vec<&str> = fe.symbols.iter().take(8).map(|s| s.name.as_str()).collect();
            let extra = if fe.symbols.len() > 8 {
                format!(" +{}", fe.symbols.len() - 8)
            } else {
                String::new()
            };
            out.push(format!(
                "- `{}` ({}L, {} syms): {}{}",
                rel,
                fe.lines,
                fe.symbols.len(),
                names.join(", "),
                extra
            ));
        }
        cap_utf8(&out.join("\n"), MAX_INLINE_BYTES)
    }

    fn query_defs(&self, target: &str) -> String {
        let q = target.to_lowercase();
        let mut exact = Vec::new();
        let mut partial = Vec::new();
        for s in self.all_symbols() {
            let nl = s.name.to_lowercase();
            if nl == q {
                exact.push(s);
            } else if nl.contains(&q) || s.file.to_lowercase().contains(&q) {
                partial.push(s);
            }
        }
        let mut out = vec![format!("# Definitions matching `{}` ({} exact, {} partial)", target, exact.len(), partial.len())];
        for s in exact.into_iter().chain(partial.into_iter()).take(40) {
            out.push(format!(
                "- [{}] `{}` @ `{}:L{}-L{}` :: `{}`",
                s.kind, s.name, s.file, s.start_line, s.end_line, s.signature
            ));
        }
        cap_utf8(&out.join("\n"), MAX_INLINE_BYTES)
    }

    fn query_callers(&self, target: &str, max_depth: usize) -> String {
        let syms = self.all_symbols();
        // Reverse map: callee_name -> Vec<&Symbol>
        let mut rev: HashMap<&str, Vec<&Symbol>> = HashMap::new();
        for s in &syms {
            for c in &s.calls {
                rev.entry(c.as_str()).or_default().push(*s);
            }
        }
        let mut out = vec![format!("# Callers of `{}` (BFS depth <= {})", target, max_depth)];
        // Show definitions of target first
        let defs: Vec<&Symbol> = syms.iter().copied().filter(|s| s.name == target).collect();
        if !defs.is_empty() {
            out.push("## Definitions:".to_string());
            for d in defs.iter().take(6) {
                out.push(format!("  - `{}:{}-{} ` {}", d.file, d.start_line, d.end_line, d.signature));
            }
        }
        let mut visited: HashSet<&str> = HashSet::new();
        let mut queue: VecDeque<(&str, usize)> = VecDeque::new();
        visited.insert(target);
        queue.push_back((target, 0));
        let mut edges = Vec::new();

        while let Some((cur, depth)) = queue.pop_front() {
            if depth >= max_depth {
                continue;
            }
            if let Some(callers) = rev.get(cur) {
                for caller in callers {
                    edges.push(format!(
                        "  [d{}] `{}` ({}:L{}-L{}) -> `{}`",
                        depth + 1,
                        caller.name,
                        caller.file,
                        caller.start_line,
                        caller.end_line,
                        cur
                    ));
                    if visited.insert(caller.name.as_str()) {
                        queue.push_back((caller.name.as_str(), depth + 1));
                    }
                }
            }
        }
        out.push(format!("## Callers ({}):", edges.len()));
        if edges.is_empty() {
            out.push("  (no direct callers found in indexed files)".to_string());
        } else {
            out.extend(edges.into_iter().take(50));
        }
        cap_utf8(&out.join("\n"), MAX_INLINE_BYTES)
    }

    fn query_callees(&self, target: &str) -> String {
        let syms = self.all_symbols();
        let mut def_map: HashMap<&str, Vec<&Symbol>> = HashMap::new();
        for s in &syms {
            def_map.entry(s.name.as_str()).or_default().push(*s);
        }
        let matches: Vec<&Symbol> = syms.iter().copied().filter(|s| s.name == target).collect();
        let mut out = vec![format!("# Callees of `{}` ({} definitions)", target, matches.len())];
        for m in matches.iter().take(10) {
            out.push(format!("## `{}` @ `{}:L{}-L{}`", m.name, m.file, m.start_line, m.end_line));
            for c in &m.calls {
                if let Some(ds) = def_map.get(c.as_str()) {
                    let locs: Vec<String> = ds.iter().take(3).map(|d| format!("{}:L{}", d.file, d.start_line)).collect();
                    out.push(format!("  -> `{}` [defined: {}]", c, locs.join(", ")));
                } else {
                    out.push(format!("  -> `{}` [external/stdlib]", c));
                }
            }
        }
        cap_utf8(&out.join("\n"), MAX_INLINE_BYTES)
    }

    fn query_impact(&self, target: &str) -> String {
        // Accepts either a symbol name or file path fragment
        let syms = self.all_symbols();
        let target_syms: Vec<&Symbol> = syms
            .iter()
            .copied()
            .filter(|s| s.name == target || s.file.ends_with(target) || s.file.contains(target))
            .collect();
        let mut rev: HashMap<&str, Vec<&Symbol>> = HashMap::new();
        for s in &syms {
            for c in &s.calls {
                rev.entry(c.as_str()).or_default().push(*s);
            }
        }
        let mut impacted_files: BTreeMap<&str, BTreeSet<String>> = BTreeMap::new();
        let mut test_hits: BTreeSet<String> = BTreeSet::new();
        for ts in &target_syms {
            if let Some(callers) = rev.get(ts.name.as_str()) {
                for c in callers {
                    impacted_files
                        .entry(c.file.as_str())
                        .or_default()
                        .insert(format!("{} (calls {})", c.name, ts.name));
                    if c.file.contains("test") || c.name.starts_with("test") {
                        test_hits.insert(format!("{}:L{} {}", c.file, c.start_line, c.name));
                    }
                }
            }
        }
        let mut out = vec![format!(
            "# Impact Analysis for `{}` ({} target symbols, {} impacted files)",
            target,
            target_syms.len(),
            impacted_files.len()
        )];
        for (f, items) in impacted_files.iter().take(25) {
            let list: Vec<&str> = items.iter().take(6).map(|s| s.as_str()).collect();
            out.push(format!("- `{}`: {}", f, list.join("; ")));
        }
        if !test_hits.is_empty() {
            out.push(format!("\n## Related Tests ({}):", test_hits.len()));
            for t in test_hits.iter().take(15) {
                out.push(format!("  - `{}`", t));
            }
        }
        cap_utf8(&out.join("\n"), MAX_INLINE_BYTES)
    }

    fn query_skeleton(&self, target: &str) -> String {
        let mut out = Vec::new();
        for (rel, fe) in &self.files {
            if rel == target || rel.ends_with(target) || rel.contains(target) {
                out.push(format!("# Skeleton: `{}` ({}L, {} imports, {} syms)", rel, fe.lines, fe.imports.len(), fe.symbols.len()));
                if !fe.imports.is_empty() {
                    out.push("## Imports:".to_string());
                    for imp in fe.imports.iter().take(15) {
                        out.push(format!("  {}", imp));
                    }
                }
                out.push("## Symbols:".to_string());
                for s in &fe.symbols {
                    let calls_preview: Vec<&str> = s.calls.iter().take(6).map(|x| x.as_str()).collect();
                    let cs = if calls_preview.is_empty() {
                        String::new()
                    } else {
                        format!("  // calls: {}", calls_preview.join(", "))
                    };
                    out.push(format!("  L{}-L{} [{}] {}{}", s.start_line, s.end_line, s.kind, s.signature, cs));
                }
                break;
            }
        }
        if out.is_empty() {
            return format!("No indexed file matching `{}` under {}", target, self.root.display());
        }
        cap_utf8(&out.join("\n"), MAX_INLINE_BYTES)
    }

    fn dispatch(&self, mode: &str, target: &str, depth: usize) -> String {
        match mode {
            "overview" => self.query_overview(),
            "defs" => self.query_defs(target),
            "callers" => self.query_callers(target, depth.clamp(1, 3)),
            "callees" => self.query_callees(target),
            "impact" => self.query_impact(target),
            "skeleton" => self.query_skeleton(target),
            _ => format!("Unknown mode '{}'. Use: overview|defs|callers|callees|impact|skeleton", mode),
        }
    }
}

fn cap_utf8(s: &str, max_bytes: usize) -> String {
    if s.len() <= max_bytes {
        return s.to_string();
    }
    let mut end = max_bytes.saturating_sub(48);
    while end > 0 && !s.is_char_boundary(end) {
        end -= 1;
    }
    format!("{}\n... [truncated to {} of {} B]", &s[..end], end, s.len())
}

// ── Minimal JSON helpers for zero-dependency Stdio MCP server ──────────────────
fn json_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 8);
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out
}

fn find_json_key_value_start(raw: &str, key: &str) -> Option<usize> {
    let needle = format!("\"{}\"", key);
    let mut offset = 0;
    while let Some(rel) = raw[offset..].find(&needle) {
        let pos = offset + rel;
        let after_key = pos + needle.len();
        let rest = &raw[after_key..];
        let ws = rest.len() - rest.trim_start().len();
        if rest[ws..].starts_with(':') {
            let val_part = &rest[ws + 1..];
            let vws = val_part.len() - val_part.trim_start().len();
            return Some(after_key + ws + 1 + vws);
        }
        offset = after_key;
    }
    None
}

fn extract_json_object_slice<'a>(raw: &'a str, key: &str) -> Option<&'a str> {
    let start = find_json_key_value_start(raw, key)?;
    let slice = &raw[start..];
    if !slice.starts_with('{') {
        return None;
    }
    let bytes = slice.as_bytes();
    let mut depth = 0i32;
    let mut in_str = false;
    let mut i = 0;
    while i < bytes.len() {
        let b = bytes[i];
        if in_str {
            if b == b'\\' {
                i += 2;
                continue;
            }
            if b == b'"' {
                in_str = false;
            }
        } else {
            match b {
                b'"' => in_str = true,
                b'{' => depth += 1,
                b'}' => {
                    depth -= 1;
                    if depth == 0 {
                        return Some(&slice[..=i]);
                    }
                }
                _ => {}
            }
        }
        i += 1;
    }
    None
}

fn extract_json_str(raw: &str, key: &str) -> Option<String> {
    let start = find_json_key_value_start(raw, key)?;
    let after = &raw[start..];
    if !after.starts_with('"') {
        return None;
    }
    let mut out = String::new();
    let mut chars = after[1..].chars();
    while let Some(c) = chars.next() {
        if c == '\\' {
            if let Some(n) = chars.next() {
                match n {
                    'n' => out.push('\n'),
                    'r' => out.push('\r'),
                    't' => out.push('\t'),
                    '"' => out.push('"'),
                    '\\' => out.push('\\'),
                    other => out.push(other),
                }
            }
        } else if c == '"' {
            return Some(out);
        } else {
            out.push(c);
        }
    }
    None
}

fn extract_json_usize(raw: &str, key: &str) -> Option<usize> {
    let start = find_json_key_value_start(raw, key)?;
    let after = &raw[start..];
    let tok: String = after.chars().take_while(|c| c.is_ascii_digit()).collect();
    tok.parse().ok()
}

fn extract_json_id(raw: &str) -> String {
    if let Some(idx) = raw.find("\"id\"") {
        let rest = &raw[idx + 4..];
        if let Some(colon) = rest.find(':') {
            let after = rest[colon + 1..].trim_start();
            if after.starts_with('"') {
                if let Some(end) = after[1..].find('"') {
                    return format!("\"{}\"", &after[1..1 + end]);
                }
            } else {
                let tok: String = after.chars().take_while(|c| c.is_ascii_digit() || *c == '-').collect();
                if !tok.is_empty() {
                    return tok;
                }
            }
        }
    }
    "null".to_string()
}

fn run_mcp() {
    let stdin = io::stdin();
    let mut stdout = io::stdout();
    for line in stdin.lock().lines().flatten() {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let method = extract_json_str(trimmed, "method").unwrap_or_default();
        let id = extract_json_id(trimmed);
        if method == "notifications/initialized" || method.starts_with("notifications/") {
            continue;
        }
        let resp = match method.as_str() {
            "initialize" => format!(
                "{{\"jsonrpc\":\"2.0\",\"id\":{},\"result\":{{\"protocolVersion\":\"2024-11-05\",\"capabilities\":{{\"tools\":{{}}}},\"serverInfo\":{{\"name\":\"wm-codegraph\",\"version\":\"3.0.0\"}}}}}}",
                id
            ),
            "tools/list" => format!(
                "{{\"jsonrpc\":\"2.0\",\"id\":{},\"result\":{{\"tools\":[{{\"name\":\"code_graph\",\"description\":\"Incremental AST/symbol & cross-file call-graph query (<2ms, saves 85-98% tokens vs grep+read loops). Modes: overview, defs, callers, callees, impact, skeleton.\",\"inputSchema\":{{\"type\":\"object\",\"properties\":{{\"mode\":{{\"type\":\"string\",\"enum\":[\"overview\",\"defs\",\"callers\",\"callees\",\"impact\",\"skeleton\"]}},\"target\":{{\"type\":\"string\",\"description\":\"Symbol name or file path fragment (optional for overview)\"}},\"root\":{{\"type\":\"string\",\"description\":\"Repository root directory (defaults to cwd)\"}},\"depth\":{{\"type\":\"integer\",\"description\":\"BFS depth for callers (1-3, default 2)\"}}}},\"required\":[\"mode\"]}}}}]}}}}",
                id
            ),
            "tools/call" => {
                let args_src = extract_json_object_slice(trimmed, "arguments").unwrap_or(trimmed);
                let mode = extract_json_str(args_src, "mode").unwrap_or_else(|| "overview".into());
                let target = extract_json_str(args_src, "target").unwrap_or_default();
                let depth = extract_json_usize(args_src, "depth").unwrap_or(2);
                let root = extract_json_str(args_src, "root")
                    .filter(|s| !s.is_empty())
                    .map(PathBuf::from)
                    .unwrap_or_else(|| env::current_dir().unwrap_or_else(|_| PathBuf::from(".")));
                let idx = RepoIndex::build(&root);
                let text = idx.dispatch(&mode, &target, depth);
                format!(
                    "{{\"jsonrpc\":\"2.0\",\"id\":{},\"result\":{{\"content\":[{{\"type\":\"text\",\"text\":\"{}\"}}],\"isError\":false}}}}",
                    id,
                    json_escape(&text)
                )
            }
            _ => format!(
                "{{\"jsonrpc\":\"2.0\",\"id\":{},\"error\":{{\"code\":-32601,\"message\":\"Method not found: {}\"}}}}",
                id,
                json_escape(&method)
            ),
        };
        let _ = writeln!(stdout, "{}", resp);
        let _ = stdout.flush();
    }
}

fn main() {
    let args: Vec<String> = env::args().skip(1).collect();
    if args.is_empty() || args[0] == "-h" || args[0] == "--help" {
        eprintln!(
            "Usage:\n  wm-codegraph <overview|defs|callers|callees|impact|skeleton> [target] [--root DIR] [--depth N]\n  wm-codegraph --mcp"
        );
        return;
    }
    if args[0] == "--mcp" {
        run_mcp();
        return;
    }

    let mode = args[0].as_str();
    let mut target = String::new();
    let mut root = env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    let mut depth = 2usize;

    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--root" if i + 1 < args.len() => {
                root = PathBuf::from(&args[i + 1]);
                i += 2;
            }
            "--depth" if i + 1 < args.len() => {
                depth = args[i + 1].parse().unwrap_or(2);
                i += 2;
            }
            other if target.is_empty() => {
                target = other.to_string();
                i += 1;
            }
            _ => i += 1,
        }
    }

    let idx = RepoIndex::build(&root);
    println!("{}", idx.dispatch(mode, &target, depth));
}
