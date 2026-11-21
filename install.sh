#!/usr/bin/env bash
# WhiteMagic AGY Turbo Kit — Idempotent Cross-Platform Installer (macOS + Linux)
# Installs:
#   1. Native Antigravity plugin `whitemagic-agy-kit` (rules, fast hooks, 4 portable skills)
#   2. Hardened `INK-Q/agy-fast` plugin (--auto-update off) if Node >= 20 is available
#   3. Disables 6 unused heavy official plugins (-10.4k tokens/session) if present
#   4. Portable CLI tools (`wm-sim`, `agy-bench`) -> ~/.local/bin and `statusline.py`
#   5. Non-destructive `~/.gemini/antigravity-cli/settings.json` tuning (with timestamped backup)
#   6. Selective `"eager": true` MCP promotion for locally detected `wm3` / `firecrawl-local-mcp`
set -euo pipefail

KIT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PLUGINS_DIR="${HOME}/.gemini/config/plugins"
LOCAL_BIN="${HOME}/.local/bin"
AGY_CLI_DIR="${HOME}/.gemini/antigravity-cli"
SKIP_AGY_FAST=0
KEEP_HEAVY_PLUGINS=0

for arg in "$@"; do
    case "$arg" in
        --skip-agy-fast) SKIP_AGY_FAST=1 ;;
        --keep-heavy-plugins) KEEP_HEAVY_PLUGINS=1 ;;
        -h|--help)
            echo "Usage: ./install.sh [--skip-agy-fast] [--keep-heavy-plugins]"
            exit 0
            ;;
    esac
done

mkdir -p "$PLUGINS_DIR" "$LOCAL_BIN" "$AGY_CLI_DIR" "${HOME}/.gemini/config"

echo "=== 1. Installing native plugin: whitemagic-agy-kit ==="
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

echo "=== 2. Installing hardened agy-fast (auto-update off) ==="
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
        node "${AF_DIR}/scripts/install.mjs" doctor || true
    else
        echo "[warn] Node.js >= 20 not found; skipping agy-fast. On macOS run: brew install node ripgrep && ./install.sh"
    fi
fi

if ! command -v rg >/dev/null 2>&1; then
    echo "[tip] Install ripgrep for 2ms local search: brew install ripgrep (macOS) or apt install ripgrep (Linux)"
fi

echo "=== 3. Trimming unused heavy official plugins (reversible via 'agy plugin enable') ==="
if [ "$KEEP_HEAVY_PLUGINS" -eq 0 ] && command -v agy >/dev/null 2>&1; then
    for p in science data-agent-kit-plugin flutter firebase google_maps_platform android-cli-plugin; do
        agy plugin disable "$p" >/dev/null 2>&1 || true
    done
fi

echo "=== 4. Installing portable CLI tools (wm-sim, agy-bench, statusline.py) ==="
cp "${KIT_DIR}/bin/wm-sim" "${LOCAL_BIN}/wm-sim"
cp "${KIT_DIR}/bin/agy-bench" "${LOCAL_BIN}/agy-bench"
cp "${KIT_DIR}/bin/statusline.py" "${AGY_CLI_DIR}/statusline.py"
chmod +x "${LOCAL_BIN}/wm-sim" "${LOCAL_BIN}/agy-bench" "${AGY_CLI_DIR}/statusline.py"

echo "=== 5. Updating ~/.gemini/antigravity-cli/settings.json (non-destructive) ==="
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
    "stack_with_default": False,
}

base_allow = [
    "command(git status)",
    "command(git diff)",
    "command(git log)",
    "command(git show)",
    "command(git tag)",
    "command(git branch)",
    "command(ls)",
    "command(wc)",
    "command(head)",
    "command(tail)",
    "command(grep)",
    "command(rg)",
    "command(jq)",
    "command(which)",
    "command(uname)",
    "command(ps)",
    "command(df)",
    "command(du)",
    "command(cargo check)",
    "command(cargo test)",
    "command(cargo clippy)",
    "command(npm run typecheck)",
    "command(npm run lint)",
    "command(npm run build)",
    "command(wm3)",
    "command(wm-sim)",
    "command(agy-bench)",
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
merged_allow = list(dict.fromkeys(base_allow + [r for r in existing_allow if r not in base_allow]))
existing_deny = perms.get("deny") or []
if "write_file(.env)" not in existing_deny:
    existing_deny.append("write_file(.env)")
perms["allow"] = merged_allow
perms["deny"] = existing_deny
cur["permissions"] = perms

p.write_text(json.dumps(cur, indent=2) + "\n", encoding="utf-8")
p.chmod(0o600)
print(f"Updated {p} ({len(merged_allow)} allow, {len(existing_deny)} deny, statusLine enabled)")
PY

echo "=== 6. Auto-wiring selective eager MCP tools for locally present servers ==="
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
    print(f"Updated selective eager MCP entries in {mcp_path}: {list(servers.keys())}")
else:
    print("No local wm3/firecrawl-local-mcp binaries detected; left mcp_config.json unchanged.")
PY

echo ""
echo "=== 7. Verification Audit ==="
export PATH="${LOCAL_BIN}:${PATH}"
wm-sim audit || true
echo ""
echo "Done! Ensure ~/.local/bin is in your PATH, then start a fresh 'agy' session."
echo "After 5-10 sessions, capture and compare A/B performance with:"
echo "  agy-bench --recent 30 --save --label baseline"
