#!/usr/bin/env python3
"""
Portable (macOS + Linux) AGY PreToolUse Guard & Deterministic Token Optimizer (Wave 3):
  1. Data-Loss Guard (force_ask on destructive shell patterns across run_command & agy-fast)
  2. Safe CLI Output Compression (overwrite run_command -> `wm-squeeze --safe` on noisy allowlisted cmds)
  3. read_file Anti-Bloat & Cache-Cliff Guard:
     - Multimodal >=150k context cache guard (force_ask)
     - Unbounded >250-line text file auto-clamp (overwrite StartLine=1, EndLine=200)
     - Unchanged exact duplicate-slice dedup within 20 steps (auto-resets on mtime change or compaction)
  4. WhiteMagic MCP Auto-Budget (overwrite token_budget=1200 on session_continuity, limit<=5 on memory_recall)

100% fail-open to `{"decision": "allow"}` on any parse/IO error. Runs under `python3 -S -B` (<18ms).
"""

import json
import os
import re
import shlex
import shutil
import socket
import stat as stat_mod
import sys
from pathlib import Path

KEKKAI_SENSITIVE_RE = re.compile(
    r"(?:\.ssh/|id_ed25519|id_rsa|\.aws/credentials|/etc/shadow|\bcurl\b[^;\n]*\|\s*(?:ba)?sh|\bwget\b[^;\n]*\|\s*(?:ba)?sh|\blandlock\b[^;\n]*\b(?:disable|bypass)\b|\bseccomp\b[^;\n]*\b(?:disable|bypass)\b)",
    re.IGNORECASE,
)
HUGINN_SOCK = Path(os.environ.get("WM_TRICAMERAL_SOCK_DIR", "/tmp")) / "wm_huginn.sock"


def check_huginn_kekkai_socket(cmd):
    """Zero-spawn (<175ms) neural Kekkai check over resident wm_huginn.sock for sensitive commands."""
    if os.environ.get("WM_HUGINN_KEKKAI_OFF") == "1" or not HUGINN_SOCK.exists():
        return None
    if not KEKKAI_SENSITIVE_RE.search(cmd):
        return None
    try:
        req = {
            "id": "pretool_kekkai",
            "prompt": f"[VETO_NEG0] {cmd[:240]}\n[AST]",
            "max_new": 16,
            "r_loops": 2,
            "shells": 3,
            "rep_penalty": 1.0,
            "mac_6d": [0.95, 0.0, 0.90, 1.00, 0.90, -1.00],
        }
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.8)
            s.connect(str(HUGINN_SOCK))
            s.sendall((json.dumps(req) + "\n").encode("utf-8"))
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(8192)
                if not chunk:
                    break
                buf += chunk
        row = json.loads(buf.decode("utf-8"))
        txt = str(row.get("gen_text") or "")
        margin = float(row.get("pick_margin") or 0.0)
        if ("[VETO_NEG0]" in txt or "REFUSE" in txt.upper() or "REJECT" in txt.upper()) and margin >= 2.0:
            return (
                f"[Huginn-1 Kekkai Neural Guard | {row.get('elapsed_ms', 0):.0f}ms | margin={margin:.2f}] "
                f"Sensitive command flagged ({txt.replace('[EOS]', '').strip()[:96]}). Confirm explicitly before proceeding."
            )
    except Exception:
        return None
    return None

DESTRUCTIVE_PATTERNS = [
    (
        re.compile(
            r"(?:^|[;&|]\s*)rm\s+(?:-(?:[^\s]*r[^\s]*f|[^\s]*f[^\s]*r)[^\s]*|-[^\s]*r[^\s]*\s+-[^\s]*f[^\s]*|-[^\s]*f[^\s]*\s+-[^\s]*r[^\s]*)\s+(?:/\s*$|/\*|\~|\$HOME(?:\s|$)|/(?:home|Users)/[^/\s]+(?:\s|$))",
            re.IGNORECASE,
        ),
        "Recursive force-delete targeting root or home directory",
    ),
    (re.compile(r"\bdrop\s+(?:table|database|schema|view)\b", re.IGNORECASE), "SQL DROP statement detected"),
    (re.compile(r"\btruncate\s+table\b", re.IGNORECASE), "SQL TRUNCATE TABLE detected"),
    (re.compile(r"\bgcloud\s+projects\s+delete\b", re.IGNORECASE), "Destructive gcloud project deletion detected"),
    (re.compile(r"\b(?:gsutil|gcloud\s+storage)\s+rm\b", re.IGNORECASE), "Cloud Storage deletion command detected"),
    (re.compile(r"\bgit\s+push\s+(?:--force|-f)\b.*\b(?:main|master)\b", re.IGNORECASE), "Force-push to main/master branch detected"),
    (
        re.compile(
            r"\bmkfs\.[a-z0-9]+\b|\bdd\s+if=.*\bof=/dev/|\bdiskutil\s+(?:eraseDisk|eraseVolume|partitionDisk)\b",
            re.IGNORECASE,
        ),
        "Raw disk/block device formatting or overwrite detected",
    ),
]
_CONV_ID_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9_-]")


def _write_json_atomic(path: Path, payload_obj) -> None:
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload_obj, fh)
    os.replace(tmp, path)

SHELL_META_RE = re.compile(r"[|><;`\n$*?\[\]{}]|(?<!&)&(?!&)|(?:^|\s)~")
_UNQUOTED_UNSAFE_CHARS = set("|><;`\n$*?[]{}()")
_DOUBLE_QUOTED_UNSAFE_CHARS = set("$`\n")
_PY_SAFE_FLAGS = {"-u", "-B", "-S", "-O", "-E", "-s", "-q"}
MEDIA_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".pdf")
BINARY_EXTS = (
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".pdf", ".zip", ".gz", ".tar",
    ".zst", ".xz", ".bz2", ".bin", ".exe", ".so", ".dylib", ".dll", ".o", ".a",
    ".wasm", ".sqlite", ".db", ".gguf", ".safetensors", ".pt", ".pth", ".mp3", ".mp4", ".wav",
)
STATE_DIR = Path(os.environ.get("WM_HOOK_STATE_DIR", "/tmp/agy-hook-state"))


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.flush()


def extract_commands(args):
    cmds = []
    for key in ("CommandLine", "command", "verify"):
        val = args.get(key)
        if val:
            cmds.append(str(val))
    raw_cmds = args.get("commands")
    if isinstance(raw_cmds, list):
        for item in raw_cmds:
            if isinstance(item, dict) and item.get("command"):
                cmds.append(str(item["command"]))
            elif isinstance(item, str):
                cmds.append(item)
    return cmds


def latest_ctx_tokens(transcript_path):
    """O(1) mtime/size-cached tail seek (last 24 KB) of transcript.jsonl for context tokens."""
    if not transcript_path:
        return 0
    try:
        p = Path(transcript_path)
        st = p.stat()
        if not stat_mod.S_ISREG(st.st_mode):
            return 0
        sz = st.st_size
        mtime_ns = st.st_mtime_ns
        # Fast O(1) stat-cache hit across parallel tool bursts within the same turn
        cf = STATE_DIR / "ctx_stat_cache.json"
        try:
            if cf.is_file():
                cached = json.loads(cf.read_text(encoding="utf-8"))
                if (
                    cached.get("path") == str(p)
                    and cached.get("sz") == sz
                    and cached.get("mtime_ns") == mtime_ns
                ):
                    return int(cached.get("ctx") or 0)
        except Exception:
            pass
        with open(p, "rb") as f:
            if sz > 24576:
                f.seek(sz - 24576)
            tail = f.read().decode("utf-8", errors="replace")
        ctx_found = 0
        for line in reversed(tail.splitlines()):
            if '"PLANNER_RESPONSE"' not in line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("type") == "PLANNER_RESPONSE":
                ctx = int(o.get("input_tokens") or 0) + int(o.get("cache_read_tokens") or 0)
                if ctx > 0:
                    ctx_found = ctx
                    break
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            _write_json_atomic(
                cf, {"path": str(p), "sz": sz, "mtime_ns": mtime_ns, "ctx": ctx_found}
            )
        except Exception:
            pass
        return ctx_found
    except Exception:
        pass
    return 0


def find_wm_squeeze():
    if shutil.which("wm-squeeze"):
        return "wm-squeeze"
    cand = Path.home() / ".local" / "bin" / "wm-squeeze"
    if cand.is_file() and os.access(cand, os.X_OK):
        return str(cand)
    return None


def is_safe_squeezable(cmd_base, cmd_args):
    """Mirror wm-squeeze --safe's strict allowlist for high-spill noisy commands."""
    if cmd_base == "git":
        i = 0
        while i < len(cmd_args):
            a = cmd_args[i]
            if a in ("-c", "--paginate") or a.startswith(("-c", "--exec-path", "--config-env", "--upload-pack")):
                return False
            if a == "-C":
                i += 2
                continue
            if a.startswith("-"):
                i += 1
                continue
            sub = a
            nxt = cmd_args[i + 1] if i + 1 < len(cmd_args) else ""
            if sub in ("status", "diff", "log", "show", "ls-files", "blame", "shortlog"):
                return True
            if sub == "branch" and not any(x in ("-d", "-D", "-m", "-M", "--delete") for x in cmd_args[i + 1 :]):
                return True
            if sub == "stash" and nxt == "list":
                return True
            if sub == "worktree" and nxt == "list":
                return True
            return False
        return False

    if cmd_base == "cargo":
        if any(a == "--config" or a.startswith("--config=") for a in cmd_args):
            return False
        sub = next((a for a in cmd_args if not a.startswith(("-", "+"))), "")
        return sub in ("check", "test", "clippy", "build", "tree")

    if cmd_base == "pytest":
        return True

    if cmd_base == "go":
        return (cmd_args[0] if cmd_args else "") in ("test", "vet", "list", "build")

    if cmd_base in ("python3", "python"):
        return len(cmd_args) >= 2 and cmd_args[0] == "-m" and cmd_args[1] in ("pytest", "unittest", "mypy", "ruff")

    if cmd_base in ("npm", "pnpm", "yarn"):
        s0 = cmd_args[0] if cmd_args else ""
        s1 = cmd_args[1] if len(cmd_args) > 1 else ""
        return s0 in ("test", "ls", "outdated") or (s0 == "run" and s1 in ("typecheck", "lint", "build", "test", "check"))

    if cmd_base == "rg":
        return not any(a == "--pre" or a.startswith("--pre=") for a in cmd_args)

    if cmd_base == "grep":
        return True

    if cmd_base == "find":
        bad = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fls", "-fprint", "-fprint0", "-fprintf"}
        return not any(a in bad for a in cmd_args)

    if cmd_base in ("ls", "tree", "du", "ruff", "mypy", "eslint", "tsc"):
        return True

    if cmd_base in ("wm3", "wm-gen3"):
        sub = ""
        i = 0
        while i < len(cmd_args):
            a = cmd_args[i]
            if a in ("--store", "--galaxy"):
                i += 2
                continue
            if a.startswith("-"):
                i += 1
                continue
            sub = a
            break
        return sub in ("recall", "evidence-recall", "stats", "status", "session", "list", "build-info", "inspect") or "--version" in cmd_args

    if cmd_base == "gh":
        s0 = cmd_args[0] if cmd_args else ""
        s1 = cmd_args[1] if len(cmd_args) > 1 else ""
        return s0 in ("pr", "issue", "repo", "run", "workflow", "release") and s1 in ("list", "view", "status", "diff", "checks")

    return False


def _has_unsafe_shell_meta(s: str, allow_quoted_newline: bool = False) -> bool:
    """Quote-aware shell metacharacter check so quoted rg/find/git patterns still squeeze safely."""
    in_single = False
    in_double = False
    escaped = False
    n = len(s)
    for i, ch in enumerate(s):
        if escaped:
            escaped = False
            continue
        if in_single:
            if ch == "'":
                in_single = False
            continue
        if in_double:
            if ch == "\\":
                escaped = True
            elif ch == '"':
                in_double = False
            elif ch == "\n":
                if not allow_quoted_newline:
                    return True
            elif ch in _DOUBLE_QUOTED_UNSAFE_CHARS:
                return True
            continue
        # Outside quotes
        if ch == "\\":
            escaped = True
            continue
        if ch == "'":
            in_single = True
            continue
        if ch == '"':
            in_double = True
            continue
        if ch in _UNQUOTED_UNSAFE_CHARS:
            return True
        if ch == "&":
            prev_amp = i > 0 and s[i - 1] == "&"
            next_amp = i + 1 < n and s[i + 1] == "&"
            if not (prev_amp ^ next_amp):
                return True
        if ch == "~" and (i == 0 or s[i - 1].isspace()):
            return True
    return in_single or in_double or escaped


def _split_top_level_and(s: str):
    """Split on unquoted `&&` only; returns None on unmatched quotes or escapes."""
    parts = []
    cur = []
    in_single = False
    in_double = False
    escaped = False
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if escaped:
            cur.append(ch)
            escaped = False
            i += 1
            continue
        if in_single:
            cur.append(ch)
            if ch == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            cur.append(ch)
            if ch == "\\":
                escaped = True
            elif ch == '"':
                in_double = False
            i += 1
            continue
        if ch == "\\":
            cur.append(ch)
            escaped = True
            i += 1
            continue
        if ch == "'":
            in_single = True
            cur.append(ch)
            i += 1
            continue
        if ch == '"':
            in_double = True
            cur.append(ch)
            i += 1
            continue
        if ch == "&" and i + 1 < n and s[i + 1] == "&":
            # Reject `&&&`
            if i + 2 < n and s[i + 2] == "&":
                return None
            parts.append("".join(cur).strip())
            cur = []
            i += 2
            continue
        cur.append(ch)
        i += 1
    if in_single or in_double or escaped:
        return None
    parts.append("".join(cur).strip())
    return parts


def _maybe_wrap_python_dash_c(s: str):
    """Wrap standalone `python3 -c "..."` (single or multiline) through `wm-squeeze --pipe`."""
    cleaned = re.sub(r"(?:\s+(?:2>&1|2>/dev/null))+\s*$", "", s).strip()
    if not cleaned:
        return None
    parts = _split_top_level_and(cleaned)
    if not parts or len(parts) != 1:
        return None
    if _has_unsafe_shell_meta(cleaned, allow_quoted_newline=True):
        return None
    try:
        toks = shlex.split(cleaned)
    except Exception:
        return None
    if len(toks) < 3 or os.path.basename(toks[0]) not in ("python3", "python"):
        return None
    try:
        c_idx = toks.index("-c", 1)
    except ValueError:
        return None
    if c_idx >= len(toks) - 1 or not all(t in _PY_SAFE_FLAGS for t in toks[1:c_idx]):
        return None
    sq_bin = find_wm_squeeze()
    if not sq_bin:
        return None
    return f"set -o pipefail; {cleaned} 2>&1 | {shlex.quote(sq_bin)} --pipe --kind generic"


def maybe_squeeze_command(cmd_line):
    if os.environ.get("WM_SQUEEZE_OFF") == "1":
        return None
    s = (cmd_line or "").strip()
    if not s or "wm-squeeze" in s:
        return None

    # Safe inline Python heredoc output cap (`python3 - <<'PY' ...`)
    if "\n" in s and os.environ.get("WM_SQUEEZE_HEREDOC_OFF") != "1":
        lines = s.splitlines()
        first = lines[0].strip()
        m = re.match(
            r"^(python3?(?:\s+-u)?)\s+-\s+<<(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2(?:\s+2>&1)?\s*$",
            first,
        )
        if m and not any(ch in first for ch in ("|", ";", "&", "$", "`")):
            tag = m.group(3)
            last_non_empty = next((ln.strip() for ln in reversed(lines[1:]) if ln.strip()), "")
            if last_non_empty == tag:
                sq_bin = find_wm_squeeze()
                if sq_bin:
                    clean_first = re.sub(r"\s+2>&1\s*$", "", first).strip()
                    new_first = (
                        f"set -o pipefail; {clean_first} 2>&1 | {shlex.quote(sq_bin)} --pipe --kind generic"
                    )
                    return "\n".join([new_first] + lines[1:])

    # Safe standalone `python3 -c "..."` output cap (single or multiline)
    if os.environ.get("WM_SQUEEZE_HEREDOC_OFF") != "1":
        wrapped_py = _maybe_wrap_python_dash_c(s)
        if wrapped_py:
            return wrapped_py

    # Strip harmless trailing 2>&1 / 2>/dev/null (wm-squeeze captures stdout + stderr)
    s = re.sub(r"(?:\s+(?:2>&1|2>/dev/null))+\s*$", "", s).strip()
    if not s:
        return None

    parts = _split_top_level_and(s)
    if not parts:
        return None

    cwd_arg = None
    # Support safe `cd <dir> && <simple_cmd>` pattern (quote-aware)
    if len(parts) == 2:
        left, right = parts[0], parts[1]
        if not left or not right or _has_unsafe_shell_meta(left) or _has_unsafe_shell_meta(right):
            return None
        try:
            ltoks = shlex.split(left)
            rtoks = shlex.split(right)
        except Exception:
            return None
        if len(ltoks) != 2 or ltoks[0] != "cd" or not rtoks:
            return None
        cwd_arg = ltoks[1]
        toks = rtoks
    elif len(parts) == 1:
        if _has_unsafe_shell_meta(s):
            return None
        try:
            toks = shlex.split(s)
        except Exception:
            return None
        if not toks:
            return None
    else:
        return None

    # Allow safe read-only env var prefixes (e.g., PYTHONPATH=. pytest) while rejecting arbitrary env mutations
    safe_env_keys = {"PYTHONPATH", "RUST_BACKTRACE", "RUST_LOG", "NO_COLOR", "CARGO_TERM_COLOR"}
    env_args = []
    while toks and "=" in toks[0]:
        k, _, _ = toks[0].partition("=")
        if k not in safe_env_keys:
            return None
        env_args.append(toks.pop(0))
    if not toks:
        return None

    cmd_base = os.path.basename(toks[0])
    if not is_safe_squeezable(cmd_base, toks[1:]):
        return None

    sq_bin = find_wm_squeeze()
    if not sq_bin:
        return None

    out = [sq_bin, "--safe"]
    if cwd_arg:
        out.extend(["--cwd", cwd_arg])
    for ev in env_args:
        out.extend(["--env", ev])
    out.append("--")
    out.extend(toks)
    return " ".join(shlex.quote(x) for x in out)


def is_wide_line_file(path_obj, size: int) -> bool:
    """Detect minified JSON / wide JSONL files (>24 KB, avg >400 B/line) to avoid 46 KB output spills."""
    if size <= 24576:
        return False
    try:
        with open(path_obj, "rb") as f:
            chunk = f.read(8192)
        if not chunk or b"\x00" in chunk[:1024]:
            return False
        newlines = chunk.count(b"\n")
        return newlines == 0 or (len(chunk) / max(newlines, 1)) > 400.0
    except Exception:
        return False


def file_exceeds_lines(path_obj, threshold=250):
    """Fast streaming check (<1ms): returns True if file has >threshold lines or >24KB text."""
    try:
        sz = path_obj.stat().st_size
        if sz <= 4096:
            return False
        if sz > 32768:
            return True
        with open(path_obj, "rb") as f:
            chunk = f.read(32768)
        if b"\x00" in chunk[:1024]:
            return False  # binary
        return chunk.count(b"\n") > threshold
    except Exception:
        return False


def check_and_record_read_dedup(conv_id, abs_path, start_line, end_line, step_idx, cur_ctx, mtime_ns, size):
    if not conv_id or os.environ.get("WM_READ_DEDUP_OFF") == "1":
        return None
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        sf = STATE_DIR / f"{_CONV_ID_SANITIZE_RE.sub('', conv_id)[:20]}.json"
        state = {}
        if sf.is_file():
            try:
                state = json.loads(sf.read_text(encoding="utf-8"))
            except Exception:
                state = {}
        key = f"{abs_path}|{start_line}|{end_line}"
        prev = state.get(key)
        if isinstance(prev, dict):
            prev_step = int(prev.get("step", -999))
            prev_ctx = int(prev.get("ctx", 0))
            unchanged = prev.get("mtime_ns") == mtime_ns and prev.get("size") == size
            not_compacted = (prev_ctx == 0) or (cur_ctx == 0) or (cur_ctx >= int(prev_ctx * 0.85))
            recent = (step_idx - prev_step) <= 20 and step_idx > prev_step
            if unchanged and not_compacted and recent:
                return (
                    f"[Read-Dedup Guard] `{os.path.basename(abs_path)}` (L{start_line or 1}-{end_line or 'EOF'}) "
                    f"was already read unchanged at step {prev_step}. Reuse from existing context or pass a different StartLine/EndLine."
                )
        state[key] = {"mtime_ns": mtime_ns, "size": size, "step": step_idx, "ctx": cur_ctx}
        if len(state) > 128:
            # Drop oldest half by step
            items = sorted(state.items(), key=lambda kv: kv[1].get("step", 0))[-96:]
            state = dict(items)
        _write_json_atomic(sf, state)
    except Exception:
        pass
    return None


def track_and_check_burst(conv_id, abs_path, step_idx):
    """Return True when >=4 distinct large files (>200 lines) were read within 10 steps."""
    if not conv_id or step_idx <= 0:
        return False
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        bf = STATE_DIR / f"burst_{_CONV_ID_SANITIZE_RE.sub('', conv_id)[:20]}.json"
        entries = []
        if bf.is_file():
            try:
                raw = json.loads(bf.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    entries = raw
            except Exception:
                entries = []
        kept = [
            e for e in entries
            if isinstance(e, list) and len(e) == 2 and isinstance(e[1], int)
            and 0 <= (step_idx - e[1]) <= 10
        ]
        distinct_paths = {e[0] for e in kept}
        burst_triggered = len(distinct_paths) >= 4
        kept.append([abs_path, step_idx])
        _write_json_atomic(bf, kept[-32:])
        return burst_triggered
    except Exception:
        return False


def handle_read_file(args, payload):
    raw_path = args.get("Path") or args.get("path") or ""
    if not raw_path:
        return {"decision": "allow"}
    low = str(raw_path).lower()
    transcript = payload.get("transcriptPath") or ""
    cur_ctx = 0

    # C1: Multimodal >=150k cache-cliff guard
    if low.endswith(MEDIA_EXTS):
        cur_ctx = latest_ctx_tokens(transcript)
        if cur_ctx >= 150_000:
            return {
                "decision": "force_ask",
                "reason": (
                    f"[Multimodal Cache Guard] Session context is ~{cur_ctx // 1000}k tokens. "
                    "Loading media inline invalidates Gemini 4 prompt cache (3-7x TTFT spike). "
                    "Prefer delegating visual inspection to a read-only subagent, or approve to load inline."
                ),
            }
        return {"decision": "allow"}

    # Skip internal spill outputs and binary files
    if "/.system_generated/" in str(raw_path) or low.endswith(BINARY_EXTS):
        return {"decision": "allow"}

    p = Path(raw_path)
    if not p.is_file():
        return {"decision": "allow"}

    try:
        st = p.stat()
        abs_p = str(p.resolve())
    except Exception:
        return {"decision": "allow"}

    s_line = args.get("StartLine")
    e_line = args.get("EndLine")
    b_off = args.get("ByteOffset")
    overwrite = {}
    conv_id = str(payload.get("conversationId") or "")
    step_idx = int(payload.get("stepIdx") or 0)

    # C2: Auto-clamp unbounded, unbounded-tail, wide-span (>250L / >200L@120k), or wide-line files (>50L)
    if b_off is None and os.environ.get("WM_READ_CLAMP_OFF") != "1":
        span = None
        if s_line is not None and e_line is not None:
            try:
                span = int(e_line) - int(s_line) + 1
            except Exception:
                span = None
        wide_lines = (span is None or span > 50) and is_wide_line_file(p, st.st_size)
        # Fast-path: explicitly bounded <=150L (or <=50L on wide-line files) cannot trigger clamping
        already_compact = span is not None and span <= (50 if wide_lines else 150)
        if not already_compact:
            cur_ctx = latest_ctx_tokens(transcript)
            max_span = 150 if cur_ctx >= 120_000 else 200
            clamp_trigger = 200 if cur_ctx >= 120_000 else 250
            if wide_lines:
                max_span = min(max_span, 50)
                clamp_trigger = min(clamp_trigger, 60)
            is_large_file = wide_lines or file_exceeds_lines(p, threshold=clamp_trigger)
            if is_large_file and conv_id and step_idx > 0:
                if track_and_check_burst(conv_id, abs_p, step_idx):
                    max_span = min(max_span, 120 if not wide_lines else 50)

            if s_line is None and e_line is None:
                if is_large_file:
                    s_line = 1
                    e_line = max_span
                    overwrite = {"StartLine": 1, "EndLine": max_span}
            elif s_line is not None and e_line is None:
                if is_large_file:
                    s_int = int(s_line)
                    e_line = s_int + max_span - 1
                    overwrite = {"EndLine": e_line}
            elif s_line is not None and e_line is not None:
                s_int = int(s_line)
                e_int = int(e_line)
                if (e_int - s_int + 1) > clamp_trigger:
                    e_line = s_int + max_span - 1
                    overwrite = {"EndLine": e_line}

    # C3: Exact unchanged duplicate-slice dedup
    if conv_id and step_idx > 0:
        cur_ctx = cur_ctx or latest_ctx_tokens(transcript)
        dedup_reason = check_and_record_read_dedup(
            conv_id, abs_p, s_line, e_line, step_idx, cur_ctx, st.st_mtime_ns, st.st_size
        )
        if dedup_reason:
            return {"decision": "deny", "reason": dedup_reason}

    if overwrite:
        return {"decision": "allow", "overwrite": overwrite}
    return {"decision": "allow"}


def handle_whitemagic_mcp(tool_name, args):
    if tool_name in ("mcp_whitemagic_session_continuity", "mcp_whitemagic_whitemagic_gen3_session_continuity"):
        tb = args.get("token_budget")
        if tb is None or (isinstance(tb, (int, float)) and tb > 2000):
            return {"decision": "allow", "overwrite": {"token_budget": 1200}}
    elif tool_name == "mcp_whitemagic_memory_recall":
        ow = {}
        lim = args.get("limit")
        if lim is None or (isinstance(lim, (int, float)) and lim > 6):
            ow["limit"] = 5
        mcb = args.get("max_content_bytes")
        if mcb is None or (isinstance(mcb, (int, float)) and mcb > 1200):
            ow["max_content_bytes"] = 900
        if ow:
            return {"decision": "allow", "overwrite": ow}
    elif tool_name == "mcp_whitemagic_whitemagic_gen3_memory_recall":
        # Gen3 cyberbrain memory_recall schema accepts {query, limit, scope, historical} (no max_content_bytes)
        lim = args.get("limit")
        if lim is None or (isinstance(lim, (int, float)) and lim > 6):
            return {"decision": "allow", "overwrite": {"limit": 5}}
    elif tool_name == "mcp_wm-research-engine_research_hunt":
        lim = args.get("limit")
        if isinstance(lim, (int, float)) and lim > 8:
            return {"decision": "allow", "overwrite": {"limit": 8}}
    elif tool_name == "mcp_firecrawl_firecrawl_scrape":
        if args.get("markdown_only") is None and args.get("format") is None:
            return {"decision": "allow", "overwrite": {"markdown_only": True}}
    return {"decision": "allow"}


def handle_manage_task(args, payload):
    """Anti-poll guard: deny >=3 consecutive manage_task(status) polls on the same TaskId within 12 steps."""
    if os.environ.get("WM_POLL_GUARD_OFF") == "1":
        return {"decision": "allow"}
    action = str(args.get("Action") or "").strip().lower()
    task_id = str(args.get("TaskId") or "").strip()
    conv_id = str(payload.get("conversationId") or "")
    step_idx = int(payload.get("stepIdx") or 0)
    if action != "status" or not task_id or not conv_id:
        return {"decision": "allow"}
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        pf = STATE_DIR / f"poll_{_CONV_ID_SANITIZE_RE.sub('', conv_id)[:20]}.json"
        state = {}
        if pf.is_file():
            try:
                state = json.loads(pf.read_text(encoding="utf-8"))
            except Exception:
                state = {}
        entry = state.get(task_id) or {"count": 0, "last_step": -999}
        prev_step = int(entry.get("last_step", -999))
        cnt = int(entry.get("count", 0))
        if step_idx > 0 and (step_idx - prev_step) <= 12 and step_idx > prev_step:
            cnt += 1
        else:
            cnt = 1
        state[task_id] = {"count": cnt, "last_step": step_idx}
        if len(state) > 32:
            items = sorted(state.items(), key=lambda kv: kv[1].get("last_step", 0))[-24:]
            state = dict(items)
        _write_json_atomic(pf, state)
        if cnt >= 3:
            return {
                "decision": "deny",
                "reason": (
                    f"[Anti-Poll Guard] Checked status of {task_id} {cnt} times within 12 steps. "
                    "Stop polling in a loop — end your turn or do other work; background tasks notify you automatically on completion."
                ),
            }
    except Exception:
        pass
    return {"decision": "allow"}


ENV_SECRET_BASENAME_RE = re.compile(r"^\.env(?:\.([A-Za-z0-9_.-]+))?$", re.IGNORECASE)
SAFE_ENV_SUFFIXES = {"example", "sample", "template", "dist", "schema"}


def check_env_write_guard(tool_name, args):
    """Require explicit confirmation when writing/editing .env secret files (portable replacement for relative deny rules)."""
    paths = []
    if tool_name in ("write_file", "edit_file"):
        p = args.get("Path") or args.get("path") or ""
        if p:
            paths.append(str(p))
    elif tool_name.endswith("_apply_edits"):
        edits = args.get("edits")
        if isinstance(edits, str):
            try:
                edits = json.loads(edits)
            except Exception:
                edits = []
        if isinstance(edits, list):
            for item in edits:
                if isinstance(item, dict) and item.get("path"):
                    paths.append(str(item["path"]))
    for raw_p in paths:
        base = os.path.basename(raw_p.strip())
        m = ENV_SECRET_BASENAME_RE.match(base)
        if m and (m.group(1) or "").lower() not in SAFE_ENV_SUFFIXES:
            return {
                "decision": "force_ask",
                "reason": (
                    f"[Env-Write Guard] Modifying secrets file `{base}` requires explicit user confirmation."
                ),
            }
    return None


def main():
    if os.environ.get("WM_AGY_BENCH_VANILLA") == "1":
        emit({"decision": "allow"})
        return
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        emit({"decision": "allow"})
        return

    tool_call = payload.get("toolCall") or {}
    tool_name = str(tool_call.get("name") or "")
    args = tool_call.get("args") or tool_call.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            args = {"command": args}
    if not isinstance(args, dict):
        emit({"decision": "allow"})
        return

    # 1. Data-loss guard + Huginn-1 Kekkai neural firewall for shell/check/edit tools
    env_guard = check_env_write_guard(tool_name, args)
    if env_guard:
        emit(env_guard)
        return
    for cmd in extract_commands(args):
        for pattern, reason in DESTRUCTIVE_PATTERNS:
            if pattern.search(cmd):
                emit({
                    "decision": "force_ask",
                    "reason": f"[Data-Loss Guard] {reason}. Confirm explicitly before proceeding.",
                })
                return
        kekkai_reason = check_huginn_kekkai_socket(cmd)
        if kekkai_reason:
            emit({"decision": "force_ask", "reason": kekkai_reason})
            return

    # 2. Safe CLI output compression for run_command
    if tool_name == "run_command" and args.get("CommandLine"):
        rewritten = maybe_squeeze_command(str(args["CommandLine"]))
        if rewritten:
            emit({"decision": "allow", "overwrite": {"CommandLine": rewritten}})
            return

    # 3. read_file anti-bloat & cache-cliff guard
    if tool_name == "read_file":
        emit(handle_read_file(args, payload))
        return

    # 3b. manage_task(status) anti-poll loop breaker
    if tool_name == "manage_task":
        emit(handle_manage_task(args, payload))
        return

    # 4. WhiteMagic, Research Engine & Firecrawl MCP auto-budget enforcement
    if tool_name in (
        "mcp_whitemagic_session_continuity",
        "mcp_whitemagic_memory_recall",
        "mcp_whitemagic_whitemagic_gen3_session_continuity",
        "mcp_whitemagic_whitemagic_gen3_memory_recall",
        "mcp_wm-research-engine_research_hunt",
        "mcp_firecrawl_firecrawl_scrape",
    ):
        emit(handle_whitemagic_mcp(tool_name, args))
        return

    emit({"decision": "allow"})


if __name__ == "__main__":
    main()
