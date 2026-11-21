#!/usr/bin/env bash
# WhiteMagic AGY Turbo Kit v2.0.0 — Idempotent Cross-Platform Installer (macOS + Linux)
# Installs:
#   1. Zero-dependency Rust CLI output compressor `wm-squeeze` -> ~/.local/bin/wm-squeeze
#   2. Native Antigravity plugin `whitemagic-agy-kit` (or `--sync-global` to update ~/.gemini/config without plugin duplication)
#   3. Hardened `INK-Q/agy-fast` plugin (--auto-update off) if Node >= 20 is available
#   4. Disables 6 unused heavy official plugins (-10.4k tokens/session) if present
#   5. Portable CLI tools (`wm-sim`, `agy-bench`, `agy-mode`, `wm-local-digest`, `wm-dream`, `statusline.py`)
#   6. Non-destructive `~/.gemini/antigravity-cli/settings.json` safe allowlist + statusLine tuning (timestamped backup; leaves model untouched)
#   7. Selective `"eager": true` MCP promotion for locally detected `wm3` / `firecrawl-local-mcp`
set -euo pipefail

KIT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PLUGINS_DIR="${HOME}/.gemini/config/plugins"
LOCAL_BIN="${HOME}/.local/bin"
AGY_CLI_DIR="${HOME}/.gemini/antigravity-cli"
SKIP_AGY_FAST=0
KEEP_HEAVY_PLUGINS=0
SYNC_GLOBAL=0

for arg in "$@"; do
    case "$arg" in
        --skip-agy-fast) SKIP_AGY_FAST=1 ;;
        --keep-heavy-plugins) KEEP_HEAVY_PLUGINS=1 ;;
        --sync-global) SYNC_GLOBAL=1 ;;
        -h|--help)
            echo "Usage: ./install.sh [--skip-agy-fast] [--keep-heavy-plugins] [--sync-global]"
            exit 0
            ;;
    esac
done

mkdir -p "$PLUGINS_DIR" "$LOCAL_BIN" "$AGY_CLI_DIR" "${HOME}/.gemini/config/hooks"

echo "=== 1. Building zero-dependency Rust CLI compressor: wm-squeeze ==="
if command -v cargo >/dev/null 2>&1; then
    cargo build --release --manifest-path "${KIT_DIR}/tools/wm-squeeze/Cargo.toml"
    cp "${KIT_DIR}/tools/wm-squeeze/target/release/wm-squeeze" "${LOCAL_BIN}/wm-squeeze"
    chmod +x "${LOCAL_BIN}/wm-squeeze"
    echo "Installed ${LOCAL_BIN}/wm-squeeze"
elif command -v rustc >/dev/null 2>&1; then
    rustc -O "${KIT_DIR}/tools/wm-squeeze/src/main.rs" -o "${LOCAL_BIN}/wm-squeeze"
    chmod +x "${LOCAL_BIN}/wm-squeeze"
    echo "Compiled ${LOCAL_BIN}/wm-squeeze via rustc"
else
    echo "[warn] Rust toolchain not found; skipping wm-squeeze (hooks fail-open cleanly without it)."
fi

if [ "$SYNC_GLOBAL" -eq 1 ]; then
    echo "=== 2. Syncing hooks & skills into global ~/.gemini/config (zero plugin duplication) ==="
    cp "${KIT_DIR}/plugin/hooks/session_start_brief.py" "${HOME}/.gemini/config/hooks/session_start_brief.py"
    cp "${KIT_DIR}/plugin/hooks/pre_tool_guard.py" "${HOME}/.gemini/config/hooks/pre_tool_guard.py"
    cp "${KIT_DIR}/plugin/hooks/post_tool_sim.py" "${HOME}/.gemini/config/hooks/post_tool_sim.py"
    cp "${KIT_DIR}/plugin/hooks/stop_telemetry.py" "${HOME}/.gemini/config/hooks/stop_telemetry.py"
    chmod +x "${HOME}/.gemini/config/hooks/"*.py
    python3 - <<'PY'
import json
from pathlib import Path

hp = Path.home() / ".gemini" / "config" / "hooks.json"
hooks = {
    "whitemagic-session-preflight": {
        "enabled": True,
        "SessionStart": [
            {"type": "command", "command": "python3 -S -B ~/.gemini/config/hooks/session_start_brief.py", "timeout": 5}
        ],
    },
    "data-loss-and-token-guard": {
        "enabled": True,
        "PreToolUse": [
            {
                "matcher": "run_command|read_file|mcp_agy-fast_fast_run_checks|mcp_agy-fast_fast_apply_edits|mcp_whitemagic_session_continuity|mcp_whitemagic_memory_recall",
                "hooks": [
                    {"type": "command", "command": "python3 -S -B ~/.gemini/config/hooks/pre_tool_guard.py", "timeout": 5}
                ],
            }
        ],
    },
    "sim-failure-capture": {
        "enabled": True,
        "PostToolUse": [
            {
                "matcher": "run_command|call_mcp_tool|mcp_agy-fast_fast_run_checks|mcp_agy-fast_fast_apply_edits",
                "hooks": [
                    {"type": "command", "command": "python3 -S -B ~/.gemini/config/hooks/post_tool_sim.py", "timeout": 5}
                ],
            }
        ],
    },
    "agy-stop-telemetry": {
        "enabled": True,
        "Stop": [
            {"type": "command", "command": "python3 -S -B ~/.gemini/config/hooks/stop_telemetry.py", "timeout": 5}
        ],
    },
}
hp.write_text(json.dumps(hooks, indent=2) + "\n", encoding="utf-8")
print(f"Updated global {hp}")
PY
else
    echo "=== 2. Installing native plugin: whitemagic-agy-kit ==="
    TARGET_PLUGIN="${PLUGINS_DIR}/whitemagic-agy-kit"
    rm -rf "$TARGET_PLUGIN"
    cp -R "${KIT_DIR}/plugin" "$TARGET_PLUGIN"
    chmod +x "${TARGET_PLUGIN}/hooks/"*.py

    if command -v agy >/dev/null 2>&1; then
        agy plugin validate --strict "$TARGET_PLUGIN"
        agy plugin enable whitemagic-agy-kit
    else
        echo "[warn] 'agy' CLI not found on PATH; plugin copied to $TARGET_PLUGIN"
    fi
fi

echo "=== 3. Checking hardened agy-fast (auto-update off) ==="
if [ "$SKIP_AGY_FAST" -eq 0 ]; then
    if command -v node >/dev/null 2>&1 && [ "$(node -p "process.versions.node.split('.')[0]")" -ge 20 ]; then
        AF_DIR="${PLUGINS_DIR}/agy-fast"
        if [ -d "$AF_DIR/.git" ]; then
            echo "agy-fast already cloned at $AF_DIR (keeping pinned revision)"
        else
            rm -rf "$AF_DIR"
            git clone --depth 1 https://github.com/INK-Q/agy-fast.git "$AF_DIR"
        fi
        node "${AF_DIR}/scripts/install.mjs" config --auto-update off
        if command -v agy >/dev/null 2>&1; then
            agy plugin validate --strict "$AF_DIR"
            agy plugin enable agy-fast
        fi
    else
        echo "[warn] Node.js >= 20 not found; skipping agy-fast."
    fi
fi

echo "=== 4. Trimming unused heavy official plugins (reversible via 'agy plugin enable') ==="
if [ "$KEEP_HEAVY_PLUGINS" -eq 0 ] && command -v agy >/dev/null 2>&1; then
    for p in science data-agent-kit-plugin flutter firebase google_maps_platform android-cli-plugin; do
        agy plugin disable "$p" >/dev/null 2>&1 || true
    done
fi

echo "=== 5. Installing portable CLI tools & cache-warmth statusline ==="
for tool in wm-sim agy-bench agy-mode wm-local-digest wm-dream; do
    cp "${KIT_DIR}/bin/${tool}" "${LOCAL_BIN}/${tool}"
    chmod +x "${LOCAL_BIN}/${tool}"
done
cp "${KIT_DIR}/bin/statusline.py" "${AGY_CLI_DIR}/statusline.py"
chmod +x "${AGY_CLI_DIR}/statusline.py"

echo "=== 6. Updating ~/.gemini/antigravity-cli/settings.json (non-destructive; model untouched) ==="
python3 - <<'PY'
import json, shutil
from datetime import datetime
from pathlib import Path

p = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
if p.exists():
    bak = p.with_name(f"settings.json.bak-{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(p, bak)
    cur = json.loads(p.read_text(encoding="utf-8"))
else:
    cur = {}

cur.setdefault("notifications", True)
cur["statusLine"] = {
    "type": "command",
    "command": "python3 -S -B ~/.gemini/antigravity-cli/statusline.py",
    "enabled": True,
}

base_allow = [
    "command(git status)",
    "command(git diff)",
    "command(git log)",
    "command(git show)",
    "command(git tag)",
    "command(git branch)",
    "command(git ls-files)",
    "command(git rev-parse)",
    "command(git blame)",
    "command(git stash list)",
    "command(git worktree list)",
    "command(ls)",
    "command(cat)",
    "command(wc)",
    "command(head)",
    "command(tail)",
    "command(grep)",
    "command(rg)",
    "command(find)",
    "command(stat)",
    "command(file)",
    "command(tree)",
    "command(jq)",
    "command(which)",
    "command(uname)",
    "command(ps)",
    "command(df)",
    "command(du)",
    "command(sha256sum)",
    "command(cargo check)",
    "command(cargo test)",
    "command(cargo clippy)",
    "command(cargo build)",
    "command(pytest)",
    "command(ruff)",
    "command(mypy)",
    "command(tsc)",
    "command(npm run typecheck)",
    "command(npm run lint)",
    "command(npm run build)",
    "command(wm3)",
    "command(wm-sim)",
    "command(wm-squeeze --safe)",
    "command(wm-local-digest)",
    "command(wm-dream)",
    "command(wm-preflight-brief)",
    "command(agy-bench)",
    "command(agy-mode)",
    "command(agy --version)",
    "command(agy changelog)",
    "command(agy plugin list)",
    "command(agy mcp list)",
    "command(gh)",
    "read_url(whitemagic.dev)",
    "read_url(antigravity.google)",
    "read_url(github.com)",
    "read_url(arxiv.org)",
]
perms = cur.get("permissions") or {}
existing_allow = perms.get("allow") or []
merged_allow = list(dict.fromkeys(existing_allow + [r for r in base_allow if r not in existing_allow]))
existing_deny = perms.get("deny") or []
if "write_file(.env)" not in existing_deny:
    existing_deny.append("write_file(.env)")
perms["allow"] = merged_allow
perms["deny"] = existing_deny
cur["permissions"] = perms

p.write_text(json.dumps(cur, indent=2) + "\n", encoding="utf-8")
p.chmod(0o600)
print(f"Updated {p} ({len(merged_allow)} allow, {len(existing_deny)} deny, statusLine enabled, model preserved={cur.get('model')!r})")
PY

echo "=== 7. Auto-wiring selective eager MCP tools for locally present servers ==="
python3 - <<'PY'
import json, shutil
from pathlib import Path

home = Path.home()
mcp_path = home / ".gemini" / "config" / "mcp_config.json"
if mcp_path.exists():
    data = json.loads(mcp_path.read_text(encoding="utf-8"))
else:
    data = {"mcpServers": {}}

servers = data.setdefault("mcpServers", {})
changed = False

wm3 = shutil.which("wm3") or (str(home / ".local" / "bin" / "wm3") if (home / ".local" / "bin" / "wm3").exists() else None)
if wm3:
    entry = servers.setdefault("whitemagic", {"command": wm3, "args": ["serve", "--store", str(home / ".local" / "share" / "whitemagic" / "gen3")]})
    tools = entry.setdefault("tools", {})
    for t in ("memory_recall", "session_continuity", "session_checkpoint"):
        tools.setdefault(t, {"eager": True})
    changed = True

fc = shutil.which("firecrawl-local-mcp") or (str(home / ".local" / "bin" / "firecrawl-local-mcp") if (home / ".local" / "bin" / "firecrawl-local-mcp").exists() else None)
if fc:
    entry = servers.setdefault("firecrawl", {"command": fc, "disabled": False, "env": {"FIRECRAWL_CLOUD": "off"}})
    entry.setdefault("tools", {}).setdefault("firecrawl_scrape", {"eager": True})
    changed = True

if changed:
    mcp_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Verified selective eager MCP entries in {mcp_path}: {list(servers.keys())}")
PY

echo ""
echo "=== 8. Verification Audit ==="
export PATH="${LOCAL_BIN}:${PATH}"
wm-sim audit || true
echo ""
echo "Done! Launch with 'agy' (or 'agy-mode longevity' / 'agy-mode orchestrator')."
