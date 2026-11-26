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
import sys

KEKKAI_SENSITIVE_PATTERN = (
    r"(?:\.ssh/|id_ed25519|id_rsa|\.aws/credentials|/etc/shadow|\bcurl\b[^;\n]*\|\s*(?:ba)?sh|\bwget\b[^;\n]*\|\s*(?:ba)?sh|\blandlock\b[^;\n]*\b(?:disable|bypass)\b|\bseccomp\b[^;\n]*\b(?:disable|bypass)\b)"
)
HUGINN_SOCK = os.path.join(os.environ.get("WM_TRICAMERAL_SOCK_DIR") or "/tmp", "wm_huginn.sock")

DESTRUCTIVE_PATTERN_SPECS = (
    (
        r"(?:^|[;&|]\s*)rm\s+(?:-(?:[^\s]*r[^\s]*f|[^\s]*f[^\s]*r)[^\s]*|-[^\s]*r[^\s]*\s+-[^\s]*f[^\s]*|-[^\s]*f[^\s]*\s+-[^\s]*r[^\s]*)\s+(?:/\s*$|/\*|\~|\$HOME(?:\s|$)|/(?:home|Users)/[^/\s]+(?:\s|$))",
        "Recursive force-delete targeting root or home directory",
    ),
    (r"\bdrop\s+(?:table|database|schema|view)\b", "SQL DROP statement detected"),
    (r"\btruncate\s+table\b", "SQL TRUNCATE TABLE detected"),
    (r"\bgcloud\s+projects\s+delete\b", "Destructive gcloud project deletion detected"),
    (r"\b(?:gsutil|gcloud\s+storage)\s+rm\b", "Cloud Storage deletion command detected"),
    (r"\bgit\s+push\s+(?:--force|-f)\b.*\b(?:main|master)\b", "Force-push to main/master branch detected"),
    (
        r"\bmkfs\.[a-z0-9]+\b|\bdd\s+if=.*\bof=/dev/|\bdiskutil\s+(?:eraseDisk|eraseVolume|partitionDisk)\b",
        "Raw disk/block device formatting or overwrite detected",
    ),
)

_DESTRUCTIVE_CACHE = None
_KEKKAI_RE_CACHE = None
_SHELL_RE_CACHE = None
_ENV_RE_CACHE = None


def _state_dir() -> str:
    return os.environ.get("WM_HOOK_STATE_DIR") or "/tmp/agy-hook-state"


def _san_conv_id(conv_id: str) -> str:
    return "".join(c for c in conv_id if c.isascii() and (c.isalnum() or c in "_-"))[:20]


def _get_destructive_patterns():
    global _DESTRUCTIVE_CACHE
    if _DESTRUCTIVE_CACHE is None:
        import re
        _DESTRUCTIVE_CACHE = [(re.compile(p, re.IGNORECASE), msg) for p, msg in DESTRUCTIVE_PATTERN_SPECS]
    return _DESTRUCTIVE_CACHE


def _get_kekkai_re():
    global _KEKKAI_RE_CACHE
    if _KEKKAI_RE_CACHE is None:
        import re
        _KEKKAI_RE_CACHE = re.compile(KEKKAI_SENSITIVE_PATTERN, re.IGNORECASE)
    return _KEKKAI_RE_CACHE


def _get_shell_res():
    global _SHELL_RE_CACHE
    if _SHELL_RE_CACHE is None:
        import re
        _SHELL_RE_CACHE = (
            re.compile(r"^[A-Za-z_][A-Za-z0-9_]*="),
            re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1"),
            re.compile(r"^-[A-Za-z]*i"),
        )
    return _SHELL_RE_CACHE


def _get_env_re():
    global _ENV_RE_CACHE
    if _ENV_RE_CACHE is None:
        import re
        _ENV_RE_CACHE = re.compile(r"^\.env(?:\.([A-Za-z0-9_.-]+))?$", re.IGNORECASE)
    return _ENV_RE_CACHE


def check_huginn_kekkai_socket(cmd):
    """Zero-spawn (<175ms) neural Kekkai check over resident wm_huginn.sock for sensitive commands."""
    if os.environ.get("WM_HUGINN_KEKKAI_OFF") == "1" or not os.path.exists(HUGINN_SOCK):
        return None
    if not _get_kekkai_re().search(cmd):
        return None
    import socket
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
            s.connect(HUGINN_SOCK)
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


def _write_json_atomic(path: str, payload_obj) -> None:
    d, base = os.path.split(path)
    tmp = os.path.join(d, f".{base}.tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload_obj, fh)
    os.replace(tmp, path)


_UNQUOTED_UNSAFE_CHARS = set("|><;`\n$*?[]{}()")
_DOUBLE_QUOTED_UNSAFE_CHARS = set("$`\n")
_PY_SAFE_FLAGS = {"-u", "-B", "-S", "-O", "-E", "-s", "-q"}
_COMPOUND_SAFE_CMDS = {
    "echo", "printf", "cd", "pwd", "ls", "ll", "find", "rg", "grep", "egrep", "fgrep",
    "cat", "head", "tail", "wc", "sed", "awk", "cut", "sort", "uniq", "tr", "nl",
    "stat", "file", "tree", "du", "df", "ps", "uptime", "uname", "which", "command",
    "type", "env", "printenv", "jq", "diff", "sha256sum", "shasum", "md5sum",
    "git", "cargo", "pytest", "go", "npm", "pnpm", "yarn", "ruff", "mypy", "eslint",
    "tsc", "python3", "python", "wm3", "wm-gen3", "wm-preflight-brief", "wm-sim",
    "wm-models", "wm-handoff", "wm-codegraph", "wm-vps", "wm-triage", "agy", "gh",
    "systemctl", "xargs", "true", "false",
}
_XARGS_READONLY_CMDS = {
    "echo", "printf", "ls", "ll", "wc", "head", "tail", "cat", "nl",
    "stat", "file", "du", "rg", "grep", "egrep", "fgrep", "sort", "uniq",
    "cut", "tr", "sha256sum", "shasum", "md5sum", "jq",
}
_XARGS_SAFE_NOARG_FLAGS = {"-0", "--null", "-r", "--no-run-if-empty", "-t", "--verbose", "-x", "--exit"}
_XARGS_SAFE_ARG_FLAGS = {"-n", "-P", "-L", "-I", "-d", "-s", "-E", "--max-args", "--max-procs", "--replace", "--delimiter", "--max-chars", "--eof"}
MEDIA_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".pdf")
BINARY_EXTS = (
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".pdf", ".zip", ".gz", ".tar",
    ".zst", ".xz", ".bz2", ".bin", ".exe", ".so", ".dylib", ".dll", ".o", ".a",
    ".wasm", ".sqlite", ".db", ".gguf", ".safetensors", ".pt", ".pth", ".mp3", ".mp4", ".wav",
)
IGNORED_HINT_NAMES = {".git", "node_modules", "__pycache__", ".DS_Store"}


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


def latest_model_and_ctx(transcript_path):
    """O(1) mtime/size-cached tail seek (last 24 KB) of transcript.jsonl for (model, ctx_tokens)."""
    if not transcript_path:
        return "", 0
    try:
        st = os.stat(transcript_path)
        if (st.st_mode & 0o170000) != 0o100000:
            return "", 0
        sz = st.st_size
        mtime_ns = st.st_mtime_ns
        state_dir = _state_dir()
        cf = os.path.join(state_dir, "ctx_stat_cache.json")
        try:
            if os.path.isfile(cf):
                with open(cf, "r", encoding="utf-8") as f:
                    cached = json.loads(f.read())
                if (
                    cached.get("path") == str(transcript_path)
                    and cached.get("sz") == sz
                    and cached.get("mtime_ns") == mtime_ns
                    and "model" in cached
                ):
                    return str(cached.get("model") or ""), int(cached.get("ctx") or 0)
        except Exception:
            pass
        with open(transcript_path, "rb") as f:
            if sz > 24576:
                f.seek(sz - 24576)
            tail = f.read().decode("utf-8", errors="replace")
        ctx_found = 0
        model_found = ""
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
                    model_found = str(o.get("model") or "")
                    break
        try:
            os.makedirs(state_dir, exist_ok=True)
            _write_json_atomic(
                cf,
                {
                    "path": str(transcript_path),
                    "sz": sz,
                    "mtime_ns": mtime_ns,
                    "ctx": ctx_found,
                    "model": model_found,
                },
            )
        except Exception:
            pass
        return model_found, ctx_found
    except Exception:
        pass
    return "", 0


def latest_ctx_tokens(transcript_path):
    return latest_model_and_ctx(transcript_path)[1]


def find_wm_squeeze():
    import shutil
    if shutil.which("wm-squeeze"):
        return "wm-squeeze"
    cand = os.path.join(os.path.expanduser("~"), ".local", "bin", "wm-squeeze")
    if os.path.isfile(cand) and os.access(cand, os.X_OK):
        return cand
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


def _strip_unquoted_line_continuations(s: str) -> str:
    """Collapse shell backslash-newline continuations outside single quotes (matches bash outside/double quotes)."""
    if "\\\n" not in s and "\\\r\n" not in s:
        return s
    out = []
    in_single = False
    in_double = False
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if in_single:
            out.append(ch)
            if ch == "'":
                in_single = False
            i += 1
            continue
        if ch == "\\":
            if i + 1 < n and s[i + 1] == "\n":
                out.append(" ")
                i += 2
                continue
            if i + 2 < n and s[i + 1] == "\r" and s[i + 2] == "\n":
                out.append(" ")
                i += 3
                continue
            if i + 1 < n:
                out.append(ch)
                out.append(s[i + 1])
                i += 2
                continue
        if ch == "'":
            if not in_double:
                in_single = True
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            in_double = not in_double
            out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_inline_comment(seg: str) -> str:
    """Strip shell `# comment` outside single/double quotes and escapes."""
    in_single = False
    in_double = False
    escaped = False
    for i, ch in enumerate(seg):
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
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == "'":
            in_single = True
            continue
        if ch == '"':
            in_double = True
            continue
        if ch == "#" and (i == 0 or seg[i - 1].isspace()):
            return seg[:i].strip()
    return seg.strip()


def _is_xargs_safe(cmd_args: list[str]) -> bool:
    """Allow xargs only with harmless flags and a strictly read-only subcommand."""
    i = 0
    n = len(cmd_args)
    while i < n:
        a = cmd_args[i]
        if a == "--":
            i += 1
            break
        if not a.startswith("-"):
            break
        if a in _XARGS_SAFE_NOARG_FLAGS:
            i += 1
            continue
        if a in _XARGS_SAFE_ARG_FLAGS:
            i += 2
            continue
        if any(a.startswith(f + "=") for f in _XARGS_SAFE_ARG_FLAGS if f.startswith("--")):
            i += 1
            continue
        if len(a) >= 2 and a[:2] in ("-n", "-P", "-L", "-I", "-d", "-s", "-E"):
            i += 1
            continue
        if a.startswith("-") and not a.startswith("--") and all(("-" + c) in _XARGS_SAFE_NOARG_FLAGS for c in a[1:]):
            i += 1
            continue
        return False

    sub_toks = cmd_args[i:]
    if not sub_toks:
        return True
    first_sub = sub_toks[0]
    if "{" in first_sub or "}" in first_sub or "$" in first_sub:
        return False
    sub_base = os.path.basename(first_sub)
    sub_args = sub_toks[1:]
    if sub_base not in _XARGS_READONLY_CMDS:
        return False
    if sub_base == "rg":
        return not any(x == "--pre" or x.startswith("--pre=") for x in sub_args)
    return True


def _is_safe_py_test_or_bench(arg: str) -> bool:
    """Allow local test_*.py / *_test.py / bench_*.py scripts while leaving arbitrary python3 scripts untouched."""
    if not arg or arg.startswith("-"):
        return False
    if any(ch in arg for ch in "$*?{}[]~;|&><`\n\r"):
        return False
    base = os.path.basename(arg)
    return base.endswith(".py") and (base.startswith("test_") or base.startswith("bench_") or base.endswith("_test.py"))


def _is_compound_segment_safe(seg: str) -> tuple[bool, str, list[str]]:
    """Validate one statement/pipe segment for read-only/diagnostic safety."""
    import re
    import shlex
    assign_re, heredoc_op_re, sed_inplace_re = _get_shell_res()

    cleaned = _strip_inline_comment(seg)
    if not cleaned:
        return True, "", []
    cleaned = re.sub(r"(?:^|\s)(?:2>&1|2>/dev/null|>/dev/null|>&[12])(?=\s|$)", " ", cleaned).strip()
    cleaned = heredoc_op_re.sub("", cleaned).strip()
    if not cleaned:
        return True, "", []
    try:
        toks = shlex.split(cleaned)
    except Exception:
        return False, "", []
    while toks and assign_re.match(toks[0]):
        toks.pop(0)
    if not toks:
        return True, "", []
    if toks[0] in ("if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done", "case", "esac", "function"):
        return False, "", []

    cmd_base = os.path.basename(toks[0])
    cmd_args = toks[1:]
    if cmd_base not in _COMPOUND_SAFE_CMDS:
        return False, cmd_base, cmd_args

    if cmd_base in ("cargo", "pytest", "go", "npm", "pnpm", "yarn", "rg", "grep", "find", "wm3", "wm-gen3", "gh"):
        return is_safe_squeezable(cmd_base, cmd_args), cmd_base, cmd_args
    if cmd_base == "xargs":
        return _is_xargs_safe(cmd_args), cmd_base, cmd_args
    if cmd_base == "git":
        if is_safe_squeezable("git", cmd_args):
            return True, cmd_base, cmd_args
        i = 0
        while i < len(cmd_args):
            a = cmd_args[i]
            if a in ("-c", "--paginate") or a.startswith(("-c", "--exec-path", "--config-env", "--upload-pack")):
                return False, cmd_base, cmd_args
            if a == "-C":
                i += 2
                continue
            if a.startswith("-"):
                i += 1
                continue
            nxt = cmd_args[i + 1] if i + 1 < len(cmd_args) else ""
            ok = a in ("rev-parse", "describe") or (a == "remote" and nxt in ("", "-v"))
            return ok, cmd_base, cmd_args
        return False, cmd_base, cmd_args
    if cmd_base in ("python3", "python"):
        rest = [a for a in cmd_args if a not in _PY_SAFE_FLAGS]
        if not rest:
            return False, cmd_base, cmd_args
        if rest[0] in ("-c", "-", "--version"):
            return True, cmd_base, cmd_args
        if len(rest) >= 2 and rest[0] == "-m" and rest[1] in ("pytest", "unittest", "mypy", "ruff", "json.tool"):
            return True, cmd_base, cmd_args
        if _is_safe_py_test_or_bench(rest[0]):
            return True, cmd_base, cmd_args
        return False, cmd_base, cmd_args
    if cmd_base == "sed":
        for a in cmd_args:
            if a == "--in-place" or a.startswith("--in-place=") or sed_inplace_re.match(a):
                return False, cmd_base, cmd_args
        return True, cmd_base, cmd_args
    if cmd_base == "awk":
        joined = " ".join(cmd_args)
        if "system(" in joined or ">" in joined:
            return False, cmd_base, cmd_args
        return True, cmd_base, cmd_args
    if cmd_base == "systemctl":
        sub = next((a for a in cmd_args if not a.startswith("-")), "")
        return sub in ("status", "cat", "is-active", "is-enabled", "list-units", "list-timers", "show"), cmd_base, cmd_args
    if cmd_base == "agy":
        pos = [a for a in cmd_args if not a.startswith("-")]
        if "--version" in cmd_args or "--help" in cmd_args:
            return True, cmd_base, cmd_args
        return (pos[:1] in (["models"], ["changelog"])) or (pos[:2] in (["plugin", "list"], ["mcp", "list"])), cmd_base, cmd_args
    return True, cmd_base, cmd_args


def _maybe_wrap_safe_compound_readonly(s: str):
    """Wrap safe compound/multiline/piped/heredoc read-only inspection blocks via `wm-squeeze --pipe`."""
    if os.environ.get("WM_SQUEEZE_COMPOUND_OFF") == "1":
        return None
    if not s or "wm-squeeze" in s:
        return None
    import shlex
    _, heredoc_op_re, _ = _get_shell_res()

    segments = []  # list of (sep_before, seg_text)
    cur = []
    sep_before = ""
    has_compound_sep = False
    has_pipe = False
    in_single = False
    in_double = False
    escaped = False
    pending_heredoc = None
    in_heredoc = False
    i = 0
    n = len(s)

    while i < n:
        if in_heredoc:
            nl = s.find("\n", i)
            line_end = n if nl == -1 else nl
            if s[i:line_end].strip() == pending_heredoc:
                in_heredoc = False
                pending_heredoc = None
            i = line_end + 1 if nl != -1 else n
            continue

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
            if ch == "\\":
                if i + 1 < n and s[i + 1] == "\n":
                    cur.append(" ")
                    i += 2
                    continue
                if i + 2 < n and s[i + 1] == "\r" and s[i + 2] == "\n":
                    cur.append(" ")
                    i += 3
                    continue
                cur.append(ch)
                escaped = True
                i += 1
                continue
            if ch in ("`",) or (ch == "$" and i + 1 < n and s[i + 1] == "("):
                return None
            cur.append(ch)
            if ch == '"':
                in_double = False
            i += 1
            continue

        # Outside quotes
        if ch == "\\":
            if i + 1 < n and s[i + 1] == "\n":
                cur.append(" ")
                i += 2
                continue
            if i + 2 < n and s[i + 1] == "\r" and s[i + 2] == "\n":
                cur.append(" ")
                i += 3
                continue
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
        if ch == "`" or (ch == "$" and i + 1 < n and s[i + 1] == "("):
            return None
        if ch in ("(", ")", "{", "}"):
            return None
        if ch == "<" and i + 1 < n and s[i + 1] == "(":
            return None
        if ch == "<" and i + 1 < n and s[i + 1] == "<":
            m_hd = heredoc_op_re.match(s, i)
            if m_hd:
                pending_heredoc = m_hd.group(2)
                cur.append(m_hd.group(0))
                i = m_hd.end()
                continue
        if ch == ">":
            if i + 1 < n and s[i + 1] == "(":
                return None
            prev_ch = s[i - 1] if i > 0 else ""
            if s.startswith(">&1", i) or s.startswith(">&2", i):
                cur.append(s[i : i + 3])
                i += 3
                continue
            if s.startswith(">/dev/null", i) and (i + 10 == n or s[i + 10].isspace() or s[i + 10] in ";|&\n"):
                if prev_ch in ("", " ", "\t", "2", "1"):
                    cur.append(">/dev/null")
                    i += 10
                    continue
            return None
        if ch == "&":
            if i + 1 < n and s[i + 1] == "&":
                if i + 2 < n and s[i + 2] == "&":
                    return None
                segments.append((sep_before, "".join(cur).strip()))
                cur = []
                sep_before = "&&"
                has_compound_sep = True
                i += 2
                continue
            return None
        if ch == "|":
            if i + 1 < n and s[i + 1] == "|":
                segments.append((sep_before, "".join(cur).strip()))
                cur = []
                sep_before = "||"
                has_compound_sep = True
                i += 2
                continue
            segments.append((sep_before, "".join(cur).strip()))
            cur = []
            sep_before = "|"
            has_pipe = True
            i += 1
            continue
        if ch == ";" or ch == "\n":
            segments.append((sep_before, "".join(cur).strip()))
            cur = []
            sep_before = ch
            if ch == ";" or not pending_heredoc:
                has_compound_sep = True
            if ch == "\n" and pending_heredoc:
                in_heredoc = True
            i += 1
            continue
        cur.append(ch)
        i += 1

    if in_single or in_double or escaped or in_heredoc:
        return None
    tail_seg = "".join(cur).strip()
    if tail_seg:
        segments.append((sep_before, tail_seg))

    if not has_compound_sep and not has_pipe:
        return None

    non_empty = []
    for sep, seg_txt in segments:
        ok, base, cargs = _is_compound_segment_safe(seg_txt)
        if not ok:
            return None
        if base:
            non_empty.append((sep, base, cargs))
    if not non_empty:
        return None

    # For single-line pure pipelines (no \n, ;, &&, ||), don't wrap if already bounded by head/tail/wc/grep -c
    if not has_compound_sep and has_pipe:
        _, last_base, last_args = non_empty[-1]
        if last_base in ("head", "tail", "wc"):
            return None
        if last_base in ("grep", "egrep", "fgrep", "rg") and any(a in ("-c", "--count") for a in last_args):
            return None

    sq_bin = find_wm_squeeze()
    if not sq_bin:
        return None
    return f"set -o pipefail; (set +o pipefail\n{s}\n) 2>&1 | {shlex.quote(sq_bin)} --pipe --kind generic"


def _maybe_wrap_python_dash_c(s: str):
    """Wrap standalone `python3 -c "..."` (single or multiline) through `wm-squeeze --pipe`."""
    import re
    import shlex
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


def _maybe_wrap_safe_py_test_or_bench(s: str):
    """Wrap standalone `[cd <dir> && ] [ENV=VAL ...] python3 test_*.py|bench_*.py` via `wm-squeeze --pipe`."""
    import re
    import shlex
    cleaned = re.sub(r"(?:\s+(?:2>&1|2>/dev/null))+\s*$", "", s).strip()
    if not cleaned:
        return None
    s_norm = _strip_unquoted_line_continuations(cleaned)
    parts = _split_top_level_and(s_norm)
    if not parts:
        return None
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
        toks = rtoks
    elif len(parts) == 1 and not _has_unsafe_shell_meta(s_norm):
        try:
            toks = shlex.split(s_norm)
        except Exception:
            return None
    else:
        return None

    safe_env_keys = {"PYTHONPATH", "RUST_BACKTRACE", "RUST_LOG", "NO_COLOR", "CARGO_TERM_COLOR"}
    while toks and "=" in toks[0]:
        k, _, _ = toks[0].partition("=")
        if k not in safe_env_keys:
            return None
        toks.pop(0)
    if not toks or os.path.basename(toks[0]) not in ("python3", "python"):
        return None
    rest = [a for a in toks[1:] if a not in _PY_SAFE_FLAGS]
    if not rest or not _is_safe_py_test_or_bench(rest[0]):
        return None
    sq_bin = find_wm_squeeze()
    if not sq_bin:
        return None
    return f"set -o pipefail; {cleaned} 2>&1 | {shlex.quote(sq_bin)} --pipe --kind generic"


def _maybe_wrap_safe_readonly_for_loop(s: str):
    """Wrap single-level read-only `for <var> in ...; do ...; done` blocks via `wm-squeeze --pipe`."""
    if os.environ.get("WM_SQUEEZE_COMPOUND_OFF") == "1":
        return None
    import re
    import shlex
    cleaned = re.sub(r"(?:\s+(?:2>&1|2>/dev/null))+\s*$", "", s).strip()
    if not cleaned:
        return None
    m_hdr = re.match(r"^for\s+[A-Za-z_][A-Za-z0-9_]*\s+in\b", cleaned)
    if not m_hdr:
        return None
    m_done = re.search(r"(?:^|[;\n])\s*done\s*$", cleaned)
    if not m_done:
        return None

    prefix = cleaned[: m_done.start()]
    # Find `do` separator cleanly outside quotes
    in_s = in_d = esc = False
    do_split = None
    i = m_hdr.end()
    np = len(prefix)
    while i < np:
        ch = prefix[i]
        if esc:
            esc = False
            i += 1
            continue
        if in_s:
            if ch == "'":
                in_s = False
            i += 1
            continue
        if in_d:
            if ch == "\\":
                esc = True
            elif ch == '"' :
                in_d = False
            elif ch == "`" or (ch == "$" and i + 1 < np and prefix[i + 1] == "("):
                return None
            i += 1
            continue
        if ch == "\\":
            esc = True
            i += 1
            continue
        if ch == "'":
            in_s = True
            i += 1
            continue
        if ch == '"':
            in_d = True
            i += 1
            continue
        if ch in ("`", "(", ")", "{", "}", "<", ">") or (ch == "$" and i + 1 < np and prefix[i + 1] == "("):
            return None
        if ch in (";", "\n"):
            m_do = re.match(r"^[;\n\s]*do\b\s*", prefix[i:])
            if m_do:
                hdr_items = prefix[:i]
                body = prefix[i + m_do.end() :].strip()
                do_split = (hdr_items, body)
                break
        i += 1

    if not do_split or in_s or in_d or esc:
        return None
    _, body = do_split
    if not body:
        return None

    # Validate loop body through read-only compound statement scanner
    # Reuse _maybe_wrap_safe_compound_readonly by verifying each statement/pipe in body
    wrapped_body = _maybe_wrap_safe_compound_readonly(body + "\ntrue")
    if not wrapped_body:
        return None

    sq_bin = find_wm_squeeze()
    if not sq_bin:
        return None
    return f"set -o pipefail; (set +o pipefail\n{s}\n) 2>&1 | {shlex.quote(sq_bin)} --pipe --kind generic"


def maybe_squeeze_command(cmd_line):
    if os.environ.get("WM_SQUEEZE_OFF") == "1":
        return None
    s = (cmd_line or "").strip()
    if not s or "wm-squeeze" in s:
        return None
    import re
    import shlex

    # Safe inline Python heredoc output cap (`[cd <dir|quoted dir> && ] python3 [-u|-B] - <<'PY' ...`)
    if "\n" in s and os.environ.get("WM_SQUEEZE_HEREDOC_OFF") != "1":
        lines = s.splitlines()
        first = lines[0].strip()
        m = re.match(
            r"""^(?:cd\s+(?:"[^"\n]+"|'[^'\n]+'|\S+)\s+&&\s+)?(python3?(?:\s+-[uBSO]+)*)\s+-\s+<<(['"]?)([A-Za-z_][A-Za-z0-9_]*)\2(?:\s+(?:2>&1|2>/dev/null))*\s*$""",
            first,
        )
        if m and not any(ch in first for ch in ("|", ";", "$", "`")) and not re.search(r"(?<!&)&\s*$", first):
            tag = m.group(3)
            last_non_empty = next((ln.strip() for ln in reversed(lines[1:]) if ln.strip()), "")
            if last_non_empty == tag:
                sq_bin = find_wm_squeeze()
                if sq_bin:
                    clean_first = re.sub(r"(?:\s+(?:2>&1|2>/dev/null))+\s*$", "", first).strip()
                    new_first = (
                        f"set -o pipefail; {clean_first} 2>&1 | {shlex.quote(sq_bin)} --pipe --kind generic"
                    )
                    return "\n".join([new_first] + lines[1:])

    # Safe standalone `python3 -c "..."` output cap (single or multiline)
    if os.environ.get("WM_SQUEEZE_HEREDOC_OFF") != "1":
        wrapped_py = _maybe_wrap_python_dash_c(s)
        if wrapped_py:
            return wrapped_py

    # Safe standalone local test_*.py / *_test.py / bench_*.py runners
    wrapped_tb = _maybe_wrap_safe_py_test_or_bench(s)
    if wrapped_tb:
        return wrapped_tb

    # Strip harmless trailing 2>&1 / 2>/dev/null and unquoted backslash-newline line continuations
    s_single = re.sub(r"(?:\s+(?:2>&1|2>/dev/null))+\s*$", "", s).strip()
    if not s_single:
        return None
    s_norm = _strip_unquoted_line_continuations(s_single)

    parts = _split_top_level_and(s_norm)
    if parts:
        cwd_arg = None
        toks = None
        # Support safe `cd <dir> && <simple_cmd>` pattern (quote-aware)
        if len(parts) == 2:
            left, right = parts[0], parts[1]
            if left and right and not _has_unsafe_shell_meta(left) and not _has_unsafe_shell_meta(right):
                try:
                    ltoks = shlex.split(left)
                    rtoks = shlex.split(right)
                    if len(ltoks) == 2 and ltoks[0] == "cd" and rtoks:
                        cwd_arg = ltoks[1]
                        toks = rtoks
                except Exception:
                    toks = None
        elif len(parts) == 1 and not _has_unsafe_shell_meta(s_norm):
            try:
                toks = shlex.split(s_norm) or None
            except Exception:
                toks = None

        if toks:
            safe_env_keys = {"PYTHONPATH", "RUST_BACKTRACE", "RUST_LOG", "NO_COLOR", "CARGO_TERM_COLOR"}
            env_args = []
            while toks and "=" in toks[0]:
                k, _, _ = toks[0].partition("=")
                if k not in safe_env_keys:
                    toks = []
                    break
                env_args.append(toks.pop(0))
            if toks:
                cmd_base = os.path.basename(toks[0])
                if is_safe_squeezable(cmd_base, toks[1:]):
                    sq_bin = find_wm_squeeze()
                    if sq_bin:
                        out = [sq_bin, "--safe"]
                        if cwd_arg:
                            out.extend(["--cwd", cwd_arg])
                        for ev in env_args:
                            out.extend(["--env", ev])
                        out.append("--")
                        out.extend(toks)
                        return " ".join(shlex.quote(x) for x in out)

    # Safe compound / multiline / uncapped pipeline / complex heredoc fallback, then safe read-only for-loop
    wrapped_comp = _maybe_wrap_safe_compound_readonly(s)
    if wrapped_comp:
        return wrapped_comp
    return _maybe_wrap_safe_readonly_for_loop(s)


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


def file_exceeds_lines(path_str, threshold=250, sz=None):
    """Fast streaming check (<1ms): returns True if file has >threshold lines or >24KB text."""
    try:
        if sz is None:
            sz = os.stat(path_str).st_size
        if sz <= 4096:
            return False
        if sz > 32768:
            return True
        with open(path_str, "rb") as f:
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
        state_dir = _state_dir()
        os.makedirs(state_dir, exist_ok=True)
        sf = os.path.join(state_dir, f"{_san_conv_id(conv_id)}.json")
        state = {}
        if os.path.isfile(sf):
            try:
                with open(sf, "r", encoding="utf-8") as fh:
                    state = json.loads(fh.read())
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
        state_dir = _state_dir()
        os.makedirs(state_dir, exist_ok=True)
        bf = os.path.join(state_dir, f"burst_{_san_conv_id(conv_id)}.json")
        entries = []
        if os.path.isfile(bf):
            try:
                with open(bf, "r", encoding="utf-8") as fh:
                    raw = json.loads(fh.read())
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


def _to_int(val):
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    try:
        return int(val)
    except Exception:
        return None


def _split_name_tokens(stem: str):
    cur = []
    out = []
    for ch in stem.lower():
        if ch.isalnum():
            cur.append(ch)
        elif cur:
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return out


def _score_filename(cand_name: str, target_name: str) -> float:
    cl = cand_name.lower()
    tl = target_name.lower()
    if cl == tl:
        return 100.0
    c_stem, c_ext = os.path.splitext(cl)
    t_stem, t_ext = os.path.splitext(tl)
    score = 0.0
    if t_ext and c_ext == t_ext:
        score += 10.0
    if t_stem and c_stem:
        if t_stem in c_stem or c_stem in t_stem:
            score += 22.0
        pref = 0
        for a, b in zip(c_stem, t_stem):
            if a == b:
                pref += 1
            else:
                break
        if pref >= 2:
            score += min(pref, 12) * 3.0
        t_toks = {t for t in _split_name_tokens(t_stem) if len(t) >= 2}
        if t_toks:
            c_toks = set(_split_name_tokens(c_stem))
            score += 8.0 * len(t_toks & c_toks)
        c_set, t_set = set(c_stem), set(t_stem)
        union = len(c_set | t_set)
        if union:
            score += 8.0 * len(c_set & t_set) / union
    return score


def _missing_path_hint(p_str: str, raw_path: str) -> str:
    """Bounded O(1) scan of parent/grandparent directory for missing read_file targets."""
    try:
        if os.path.isdir(p_str):
            items = []
            with os.scandir(p_str) as it:
                for idx, entry in enumerate(it):
                    if idx >= 80:
                        break
                    if entry.name in IGNORED_HINT_NAMES:
                        continue
                    try:
                        items.append(entry.name + ("/" if entry.is_dir() else ""))
                    except Exception:
                        items.append(entry.name)
            items.sort()
            shown = ", ".join(items[:10])
            suffix = f" (+{len(items) - 10} more)" if len(items) > 10 else ""
            base = os.path.basename(p_str) or p_str
            return (
                f"[Path Hint] `{base}` is a directory, not a file. "
                + (f"Entries: {shown}{suffix}." if shown else "Directory is empty.")
            )

        parent = os.path.dirname(p_str)
        target_name = os.path.basename(p_str)
        gp_hit = ""
        gp = os.path.dirname(parent) if parent else ""
        if gp and gp != parent and target_name and os.path.isdir(gp):
            cand_gp = os.path.join(gp, target_name)
            if os.path.isfile(cand_gp):
                gp_hit = cand_gp

        if parent and os.path.isdir(parent):
            scored = []
            with os.scandir(parent) as it:
                for idx, entry in enumerate(it):
                    if idx >= 100:
                        break
                    if entry.name in IGNORED_HINT_NAMES:
                        continue
                    try:
                        disp = entry.name + ("/" if entry.is_dir() else "")
                    except Exception:
                        disp = entry.name
                    scored.append((-_score_filename(entry.name, target_name), disp.lower(), disp))
            scored.sort()
            best = [item[2] for item in scored[:4]]
            p_disp = os.path.basename(parent) or parent
            msg = f"[Path Hint] File does not exist: `{raw_path}`."
            if best:
                msg += f" Closest in `{p_disp}/`: {', '.join(best)}."
            else:
                msg += f" `{p_disp}/` has no matching entries."
            if gp_hit:
                msg += f" Found `{target_name}` in parent dir: `{gp_hit}`."
            return msg

        if gp and os.path.isdir(gp):
            gp_entries = []
            with os.scandir(gp) as it:
                for idx, entry in enumerate(it):
                    if idx >= 60:
                        break
                    if entry.name in IGNORED_HINT_NAMES:
                        continue
                    try:
                        disp = entry.name + ("/" if entry.is_dir() else "")
                    except Exception:
                        disp = entry.name
                    gp_entries.append((-_score_filename(entry.name, os.path.basename(parent)), disp.lower(), disp))
            gp_entries.sort()
            gp_best = [item[2] for item in gp_entries[:4]]
            msg = f"[Path Hint] Parent directory does not exist: `{parent}`."
            if gp_hit:
                msg += f" Found `{target_name}` at `{gp_hit}`."
            elif gp_best:
                msg += f" Closest in `{gp}/`: {', '.join(gp_best)}."
            return msg

        return f"[Path Hint] File and parent directory do not exist: `{raw_path}`."
    except Exception:
        return f"[Path Hint] File does not exist: `{raw_path}`."


def handle_read_file(args, payload):
    raw_path = args.get("Path") or args.get("path") or ""
    if not raw_path:
        return {"decision": "allow"}
    low = str(raw_path).lower()
    transcript = payload.get("transcriptPath") or ""
    cur_ctx = 0

    # C1: Multimodal >=150k cache-cliff guard (must precede disk existence checks)
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

    p_raw = os.path.expanduser(str(raw_path).strip())
    if not os.path.isabs(p_raw):
        cwd = str(payload.get("cwd") or "").strip()
        if cwd and os.path.isdir(cwd):
            p_str = os.path.normpath(os.path.join(cwd, p_raw))
        elif os.path.exists(p_raw):
            p_str = os.path.abspath(p_raw)
        else:
            return {"decision": "allow"}
    else:
        p_str = os.path.normpath(p_raw)

    if not os.path.isfile(p_str):
        if os.environ.get("WM_READ_HINT_OFF") == "1":
            return {"decision": "allow"}
        return {"decision": "deny", "reason": _missing_path_hint(p_str, str(raw_path))}

    try:
        st = os.stat(p_str)
        abs_p = os.path.realpath(p_str)
    except Exception:
        return {"decision": "allow"}

    s_line = args.get("StartLine")
    e_line = args.get("EndLine")
    b_off = args.get("ByteOffset")
    overwrite = {}
    conv_id = str(payload.get("conversationId") or "")
    step_idx = int(payload.get("stepIdx") or 0)
    s_int = _to_int(s_line)
    e_int = _to_int(e_line)

    # C2: Self-heal inverted/invalid line ranges + auto-clamp unbounded/wide-span/wide-line reads
    if b_off is None and os.environ.get("WM_READ_CLAMP_OFF") != "1":
        if s_int is not None and s_int < 1:
            s_int = 1
            s_line = 1
            overwrite["StartLine"] = 1
        inverted = s_int is not None and e_int is not None and e_int < s_int
        span = (e_int - s_int + 1) if (s_int is not None and e_int is not None and not inverted) else None
        wide_lines = (inverted or span is None or span > 50) and is_wide_line_file(p_str, st.st_size)

        already_compact = (not inverted) and (span is not None) and span <= (50 if wide_lines else 150)
        if not already_compact:
            cur_ctx = latest_ctx_tokens(transcript)
            if cur_ctx >= 160_000:
                max_span, clamp_trigger = 100, 150
            elif cur_ctx >= 120_000:
                max_span, clamp_trigger = 150, 200
            else:
                max_span, clamp_trigger = 200, 250
            if wide_lines:
                max_span = min(max_span, 50)
                clamp_trigger = min(clamp_trigger, 60)
            is_large_file = wide_lines or file_exceeds_lines(p_str, threshold=clamp_trigger, sz=st.st_size)
            if is_large_file and conv_id and step_idx > 0:
                if track_and_check_burst(conv_id, abs_p, step_idx):
                    max_span = min(max_span, 120 if not wide_lines else 50)

            if inverted:
                if 1 <= e_int <= 250:
                    # EndLine passed as line count -> compute inclusive EndLine
                    e_int = s_int + min(e_int, max_span) - 1
                    e_line = e_int
                    overwrite["EndLine"] = e_int
                elif e_int > 250:
                    # Swapped (StartLine, EndLine) -> flip and bound to max_span
                    new_s = e_int
                    new_e = min(s_int, new_s + max_span - 1)
                    s_int, e_int = new_s, new_e
                    s_line, e_line = new_s, new_e
                    overwrite["StartLine"] = new_s
                    overwrite["EndLine"] = new_e
                else:
                    e_int = s_int + max_span - 1
                    e_line = e_int
                    overwrite["EndLine"] = e_int
            elif s_int is None and e_int is None:
                if is_large_file:
                    s_line = 1
                    e_line = max_span
                    overwrite["StartLine"] = 1
                    overwrite["EndLine"] = max_span
            elif s_int is not None and e_int is None:
                if is_large_file:
                    e_line = s_int + max_span - 1
                    overwrite["EndLine"] = e_line
            elif s_int is not None and e_int is not None:
                if (e_int - s_int + 1) > clamp_trigger:
                    e_line = s_int + max_span - 1
                    overwrite["EndLine"] = e_line
            elif s_int is None and e_int is not None:
                if e_int < 1:
                    e_int = max_span
                    e_line = e_int
                    overwrite["EndLine"] = e_int
                elif is_large_file and e_int > clamp_trigger:
                    s_line = 1
                    e_line = max_span
                    overwrite["StartLine"] = 1
                    overwrite["EndLine"] = max_span

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
        state_dir = _state_dir()
        os.makedirs(state_dir, exist_ok=True)
        pf = os.path.join(state_dir, f"poll_{_san_conv_id(conv_id)}.json")
        state = {}
        if os.path.isfile(pf):
            try:
                with open(pf, "r", encoding="utf-8") as fh:
                    state = json.loads(fh.read())
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


SAFE_ENV_SUFFIXES = {"example", "sample", "template", "dist", "schema"}


def check_env_write_guard(tool_name, args):
    """Require explicit confirmation when writing/editing .env secret files (portable replacement for relative deny rules)."""
    if tool_name not in ("write_file", "edit_file") and not tool_name.endswith("_apply_edits"):
        return None
    paths = []
    if tool_name in ("write_file", "edit_file"):
        p = args.get("Path") or args.get("path") or ""
        if p:
            paths.append(str(p))
    else:
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
    if not paths:
        return None
    env_re = _get_env_re()
    for raw_p in paths:
        base = os.path.basename(raw_p.strip())
        m = env_re.match(base)
        if m and (m.group(1) or "").lower() not in SAFE_ENV_SUFFIXES:
            return {
                "decision": "force_ask",
                "reason": (
                    f"[Env-Write Guard] Modifying secrets file `{base}` requires explicit user confirmation."
                ),
            }
    return None


def handle_swarm_and_subagent(tool_name, args, payload):
    """Enforce Orchestrator & /swarm cost/bloat guardrails."""
    if os.environ.get("WM_SWARM_GUARD_OFF") == "1":
        return {"decision": "allow"}
    transcript = payload.get("transcriptPath") or ""

    if tool_name == "swarm":
        action = str(args.get("action") or "").strip().lower()
        if action == "add":
            fork_arg = str(args.get("fork") or "").strip()
            model_arg = str(args.get("model") or "").strip()
            brief_txt = str(args.get("brief") or "")
            if fork_arg:
                _, cur_ctx = latest_model_and_ctx(transcript)
                if cur_ctx >= 80_000:
                    return {
                        "decision": "force_ask",
                        "reason": (
                            f"[Swarm Fork Guard] Coordinator context is ~{cur_ctx // 1000}k tokens. "
                            "Forking copies this context onto every worker. Omit `fork` and pass a compact fresh brief + shared codemap (`wm-handoff --swarm-map`), or confirm to fork anyway."
                        ),
                    }
            if not model_arg:
                cur_model, _ = latest_model_and_ctx(transcript)
                if "neon" in cur_model.lower():
                    return {
                        "decision": "force_ask",
                        "reason": (
                            "[Swarm Model Guard] Coordinator is on Neon and `model` is omitted — spawned worker would inherit Neon (~3.1x burn). "
                            "Pass `model: \"gemini-4-hydrogen-high\"` (or `xhigh` for complex debugging), or confirm to spawn on Neon."
                        ),
                    }
            if len(brief_txt) > 6000:
                return {
                    "decision": "force_ask",
                    "reason": (
                        f"[Swarm Brief Guard] Brief is {len(brief_txt) / 1024:.1f} KB (>6 KB), bloating the shared board for every member. "
                        "Write details to a file and pass a <=150-token pointer brief, or confirm to post inline."
                    ),
                }
        return {"decision": "allow"}

    if tool_name == "post_message":
        msg_txt = str(args.get("message") or args.get("content") or args.get("body") or "")
        if len(msg_txt) > 4000:
            return {
                "decision": "force_ask",
                "reason": (
                    f"[Board Bloat Guard] Board message is {len(msg_txt) / 1024:.1f} KB (>4 KB). "
                    "Write full logs/tables to a file and post a <=150-token CPR pointer receipt, or confirm to post inline."
                ),
            }
        return {"decision": "allow"}

    if tool_name == "invoke_subagent":
        tname = str(args.get("TypeName") or "").strip().lower()
        m_arg = str(args.get("Model") or "").strip().lower()
        if tname == "research" and m_arg in ("", "inherit"):
            cur_model, _ = latest_model_and_ctx(transcript)
            if "neon" in cur_model.lower():
                if os.environ.get("WM_SUBAGENT_AUTO_FLASH") == "1":
                    return {"decision": "allow", "overwrite": {"Model": "flash"}}
                return {
                    "decision": "force_ask",
                    "reason": (
                        "[Subagent Model Guard] Parent is on Neon and `research` subagent uses `Model=\"inherit\"` (would run read-only research on Neon). "
                        "Pass `Model=\"flash\"` (or set `WM_SUBAGENT_AUTO_FLASH=1`), or confirm to run on Neon."
                    ),
                }
        return {"decision": "allow"}

    return {"decision": "allow"}


def _emit_host_allow(host: str):
    if host == "cursor":
        emit({"permission": "allow"})
    elif host == "codex":
        emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}})
    else:
        emit({"decision": "allow"})


def _emit_host_ask(host: str, reason: str):
    if host == "cursor":
        emit({"permission": "ask", "user_message": reason, "agent_message": reason})
    elif host == "codex":
        emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask", "permissionDecisionReason": reason}})
    else:
        emit({"decision": "force_ask", "reason": reason})


def handle_non_agy_pretool(host: str, event: str, payload: dict):
    ev = event or str(payload.get("hook_event_name") or "")
    tool_name = str(payload.get("tool_name") or "")
    ti_raw = payload.get("tool_input")
    ti_dict = None
    if isinstance(ti_raw, dict):
        ti_dict = ti_raw
    elif isinstance(ti_raw, str) and ti_raw.lstrip().startswith("{"):
        try:
            parsed = json.loads(ti_raw)
            if isinstance(parsed, dict):
                ti_dict = parsed
        except Exception:
            ti_dict = None

    if host == "cursor" and ev == "beforeShellExecution":
        cmd = str(payload.get("command") or (ti_dict or {}).get("command") or "")
        if cmd:
            for pattern, reason in _get_destructive_patterns():
                if pattern.search(cmd):
                    _emit_host_ask(host, f"[Data-Loss Guard] {reason}. Confirm explicitly before proceeding.")
                    return
            kekkai_reason = check_huginn_kekkai_socket(cmd)
            if kekkai_reason:
                _emit_host_ask(host, kekkai_reason)
                return
        _emit_host_allow(host)
        return

    if host == "cursor" and ev == "beforeMCPExecution":
        args_mcp = ti_dict or {}
        eg = check_env_write_guard(
            "mcp_whitemagic-agy-kit_fast_apply_edits" if tool_name == "apply_edits" else tool_name,
            args_mcp,
        )
        if eg:
            _emit_host_ask(host, str(eg.get("reason") or "[Env-Write Guard] Secret file modification requires confirmation."))
            return
        for cmd in extract_commands(args_mcp):
            for pattern, reason in _get_destructive_patterns():
                if pattern.search(cmd):
                    _emit_host_ask(host, f"[Data-Loss Guard] {reason}. Confirm explicitly before proceeding.")
                    return
            kekkai_reason = check_huginn_kekkai_socket(cmd)
            if kekkai_reason:
                _emit_host_ask(host, kekkai_reason)
                return
        _emit_host_allow(host)
        return

    if tool_name in ("Shell", "Bash", "shell", "exec_command", "run_command"):
        cmd_key = "cmd" if (ti_dict and "cmd" in ti_dict and "command" not in ti_dict) else "command"
        cmd_line = str((ti_dict or {}).get(cmd_key) or payload.get("command") or "")
        if cmd_line:
            if host != "cursor":
                for pattern, reason in _get_destructive_patterns():
                    if pattern.search(cmd_line):
                        _emit_host_ask(host, f"[Data-Loss Guard] {reason}. Confirm explicitly before proceeding.")
                        return
                kekkai_reason = check_huginn_kekkai_socket(cmd_line)
                if kekkai_reason:
                    _emit_host_ask(host, kekkai_reason)
                    return
            rewritten = maybe_squeeze_command(cmd_line)
            if rewritten:
                updated = dict(ti_dict) if ti_dict is not None else {}
                updated[cmd_key] = rewritten
                if host == "cursor":
                    emit({"permission": "allow", "updated_input": updated})
                else:
                    emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow", "updatedInput": updated}})
                return
        _emit_host_allow(host)
        return

    _emit_host_allow(host)


def main():
    host = "agy"
    event = ""
    cli_args = sys.argv[1:]
    for idx, a in enumerate(cli_args):
        if a == "--host" and idx + 1 < len(cli_args):
            host = cli_args[idx + 1].strip().lower()
        elif a.startswith("--host="):
            host = a.split("=", 1)[1].strip().lower()
        elif a == "--event" and idx + 1 < len(cli_args):
            event = cli_args[idx + 1].strip()
        elif a.startswith("--event="):
            event = a.split("=", 1)[1].strip()

    if os.environ.get("WM_AGY_BENCH_VANILLA") == "1":
        _emit_host_allow(host)
        return
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        _emit_host_allow(host)
        return

    if host != "agy":
        handle_non_agy_pretool(host, event, payload)
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

    # 1. Data-loss guard + Huginn-1 Kekkai neural firewall (only compile patterns when shell cmds exist)
    env_guard = check_env_write_guard(tool_name, args)
    if env_guard:
        emit(env_guard)
        return
    cmds = extract_commands(args)
    if cmds:
        destructive_patterns = _get_destructive_patterns()
        for cmd in cmds:
            for pattern, reason in destructive_patterns:
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

    # 3. read_file self-heal, missing-path hints, anti-bloat & cache-cliff guard
    if tool_name == "read_file":
        emit(handle_read_file(args, payload))
        return

    # 3b. manage_task(status) anti-poll loop breaker
    if tool_name == "manage_task":
        emit(handle_manage_task(args, payload))
        return

    # 3c. Orchestrator & /swarm guardrails
    if tool_name in ("swarm", "post_message", "invoke_subagent"):
        emit(handle_swarm_and_subagent(tool_name, args, payload))
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
