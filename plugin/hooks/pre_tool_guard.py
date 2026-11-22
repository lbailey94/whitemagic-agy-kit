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
]

SHELL_META_RE = re.compile(r"[|><;`\n$*?\[\]{}]|(?<!&)&(?!&)|(?:^|\s)~")
MEDIA_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".pdf")
BINARY_EXTS = (
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".pdf", ".zip", ".gz", ".tar",
    ".zst", ".xz", ".bz2", ".bin", ".exe", ".so", ".dylib", ".dll", ".o", ".a",
    ".wasm", ".sqlite", ".db", ".gguf", ".safetensors", ".pt", ".pth", ".mp3", ".mp4", ".wav",
)
STATE_DIR = Path("/tmp/agy-hook-state")


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
    """O(1) tail seek (last 24 KB) of transcript.jsonl to read current context tokens."""
    if not transcript_path:
        return 0
    try:
        p = Path(transcript_path)
        if not p.is_file():
            return 0
        sz = p.stat().st_size
        with open(p, "rb") as f:
            if sz > 24576:
                f.seek(sz - 24576)
            tail = f.read().decode("utf-8", errors="replace")
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
                    return ctx
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


def maybe_squeeze_command(cmd_line):
    if os.environ.get("WM_SQUEEZE_OFF") == "1":
        return None
    s = (cmd_line or "").strip()
    if not s or "wm-squeeze" in s:
        return None

    cwd_arg = None
    # Support safe `cd <dir> && <simple_cmd>` pattern
    if "&&" in s:
        parts = s.split("&&")
        if len(parts) != 2:
            return None
        left, right = parts[0].strip(), parts[1].strip()
        if SHELL_META_RE.search(left) or SHELL_META_RE.search(right):
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
    else:
        if SHELL_META_RE.search(s) or "&&" in s:
            return None
        try:
            toks = shlex.split(s)
        except Exception:
            return None
        if not toks:
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
        sf = STATE_DIR / f"{re.sub(r'[^a-zA-Z0-9_-]', '', conv_id)[:20]}.json"
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
        sf.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass
    return None


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

    # C2: Auto-clamp unbounded large text file reads (>250 lines)
    if s_line is None and e_line is None and b_off is None and os.environ.get("WM_READ_CLAMP_OFF") != "1":
        if file_exceeds_lines(p, threshold=250):
            s_line = 1
            e_line = 200
            overwrite = {"StartLine": 1, "EndLine": 200}

    # C3: Exact unchanged duplicate-slice dedup
    conv_id = str(payload.get("conversationId") or "")
    step_idx = int(payload.get("stepIdx") or 0)
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
    return {"decision": "allow"}


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
    for cmd in extract_commands(args):
        for pattern, reason in DESTRUCTIVE_PATTERNS:
            if re.search(pattern, cmd, flags=re.IGNORECASE):
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

    # 4. WhiteMagic MCP auto-budget enforcement
    if tool_name in (
        "mcp_whitemagic_session_continuity",
        "mcp_whitemagic_memory_recall",
        "mcp_whitemagic_whitemagic_gen3_session_continuity",
        "mcp_whitemagic_whitemagic_gen3_memory_recall",
    ):
        emit(handle_whitemagic_mcp(tool_name, args))
        return

    emit({"decision": "allow"})


if __name__ == "__main__":
    main()
