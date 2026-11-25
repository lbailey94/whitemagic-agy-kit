#!/usr/bin/env bash
# WhiteMagic AGY Turbo Kit v4.4.2 — Idempotent Cross-Platform Installer (macOS + Linux)
# Installs:
#   1. Zero-dependency Rust binaries `wm-squeeze`, `wm-codegraph`, & Safe-Rust `muninn-engine` -> ~/.local/bin/
#   2. Native Antigravity plugin `whitemagic-agy-kit` (+ eager `code_graph` & `tricameral_dispatch` MCPs; or `--sync-global`)
#   3. Hardened `INK-Q/agy-fast` plugin (--auto-update off) if Node >= 20 is available (preserves existing valid installs)
#   4. Disables 6 unused heavy official plugins (-10.4k tokens/session) if present
#   5. Portable CLI tools (`wm-sim`, `agy-bench`, `agy-mode`, `wm-local-digest`, `wm-dream`, `wm-handoff`, `wm-models`, `wm-huginn`, `wm-muninn`, `wm-tricameral`, `wm-tricameral-mcp`, `statusline.py`)
#   6. Non-destructive `~/.gemini/antigravity-cli/settings.json` safe allowlist + statusLine tuning (timestamped backup; leaves model untouched)
#   7. Selective `"eager": true` MCP promotion for locally detected `wm3` / `whitemagic_gen3` / `firecrawl-local-mcp`
#   8. Optional Tricameral spore pull (`--with-huginn` | `--with-ravens` | `--with-tricameral`) via `wm-models`
set -euo pipefail

KIT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PLUGINS_DIR="${HOME}/.gemini/config/plugins"
LOCAL_BIN="${HOME}/.local/bin"
AGY_CLI_DIR="${HOME}/.gemini/antigravity-cli"
MODELS_DIR="${WM_MODELS_DIR:-${HOME}/.local/share/whitemagic/models}"
SKIP_AGY_FAST=0
SKIP_TRICAMERAL_ENGINE=0
KEEP_HEAVY_PLUGINS=0
SYNC_GLOBAL=0
PULL_PROFILE=""

for arg in "$@"; do
    case "$arg" in
        --skip-agy-fast) SKIP_AGY_FAST=1 ;;
        --skip-tricameral-engine) SKIP_TRICAMERAL_ENGINE=1 ;;
        --keep-heavy-plugins) KEEP_HEAVY_PLUGINS=1 ;;
        --sync-global) SYNC_GLOBAL=1 ;;
        --with-huginn) PULL_PROFILE="huginn" ;;
        --with-ravens) PULL_PROFILE="ravens" ;;
        --with-tricameral) PULL_PROFILE="tricameral" ;;
        -h|--help)
            echo "Usage: ./install.sh [--skip-agy-fast] [--skip-tricameral-engine] [--keep-heavy-plugins] [--sync-global] [--with-huginn|--with-ravens|--with-tricameral]"
            exit 0
            ;;
    esac
done

mkdir -p "$PLUGINS_DIR" "$LOCAL_BIN" "$AGY_CLI_DIR" "${HOME}/.gemini/config/hooks" "$MODELS_DIR"

install_bin() {
    # Atomic temp-file + mv replaces the directory entry with a fresh inode,
    # avoiding Apple Silicon macOS AMFI SIGKILL (exit 137) and Linux ETXTBSY
    # when upgrading a running/mapped binary (e.g. wm-codegraph --mcp) in place.
    local src="$1" dst="$2"
    local tmp="${dst}.tmp.$$"
    cp "$src" "$tmp"
    chmod +x "$tmp"
    mv -f "$tmp" "$dst"
}

echo "=== 1. Building Rust binaries: wm-squeeze, wm-codegraph & Safe-Rust muninn-engine ==="
if command -v cargo >/dev/null 2>&1; then
    cargo build --release --manifest-path "${KIT_DIR}/tools/wm-squeeze/Cargo.toml"
    install_bin "${KIT_DIR}/tools/wm-squeeze/target/release/wm-squeeze" "${LOCAL_BIN}/wm-squeeze"
    cargo build --release --manifest-path "${KIT_DIR}/tools/wm-codegraph/Cargo.toml"
    install_bin "${KIT_DIR}/tools/wm-codegraph/target/release/wm-codegraph" "${LOCAL_BIN}/wm-codegraph"
    echo "Installed ${LOCAL_BIN}/wm-squeeze & ${LOCAL_BIN}/wm-codegraph"
    if [ "$SKIP_TRICAMERAL_ENGINE" -eq 0 ] && [ -f "${KIT_DIR}/tools/wm-tricameral-engine/Cargo.toml" ]; then
        if cargo build --release --manifest-path "${KIT_DIR}/tools/wm-tricameral-engine/Cargo.toml"; then
            install_bin "${KIT_DIR}/tools/wm-tricameral-engine/target/release/muninn-engine" "${LOCAL_BIN}/muninn-engine"
            echo "Installed ${LOCAL_BIN}/muninn-engine (Safe-Rust Tricameral Engine)"
        else
            echo "[warn] muninn-engine build failed (requires Rust >= 1.88 / edition 2024); continuing without Tricameral engine."
        fi
    fi
elif command -v rustc >/dev/null 2>&1; then
    rustc -O "${KIT_DIR}/tools/wm-squeeze/src/main.rs" -o "${LOCAL_BIN}/wm-squeeze.tmp.$$"
    chmod +x "${LOCAL_BIN}/wm-squeeze.tmp.$$"
    mv -f "${LOCAL_BIN}/wm-squeeze.tmp.$$" "${LOCAL_BIN}/wm-squeeze"
    rustc -O "${KIT_DIR}/tools/wm-codegraph/src/main.rs" -o "${LOCAL_BIN}/wm-codegraph.tmp.$$"
    chmod +x "${LOCAL_BIN}/wm-codegraph.tmp.$$"
    mv -f "${LOCAL_BIN}/wm-codegraph.tmp.$$" "${LOCAL_BIN}/wm-codegraph"
    echo "Compiled ${LOCAL_BIN}/wm-squeeze & ${LOCAL_BIN}/wm-codegraph via rustc"
else
    echo "[warn] Rust toolchain not found; skipping native binaries (hooks fail-open cleanly without them)."
fi

if [ "$SYNC_GLOBAL" -eq 1 ]; then
    echo "=== 2. Syncing hooks & skills into global ~/.gemini/config (zero plugin duplication) ==="
    cp "${KIT_DIR}/plugin/hooks/session_start_brief.py" "${HOME}/.gemini/config/hooks/session_start_brief.py"
    cp "${KIT_DIR}/plugin/hooks/pre_tool_guard.py" "${HOME}/.gemini/config/hooks/pre_tool_guard.py"
    cp "${KIT_DIR}/plugin/hooks/post_tool_sim.py" "${HOME}/.gemini/config/hooks/post_tool_sim.py"
    cp "${KIT_DIR}/plugin/hooks/longevity_hud.py" "${HOME}/.gemini/config/hooks/longevity_hud.py"
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
                "matcher": "run_command|read_file|write_file|edit_file|manage_task|mcp_whitemagic-agy-kit_fast_run_checks|mcp_whitemagic-agy-kit_fast_apply_edits|mcp_agy-fast_fast_run_checks|mcp_agy-fast_fast_apply_edits|mcp_whitemagic_session_continuity|mcp_whitemagic_memory_recall|mcp_whitemagic_whitemagic_gen3_session_continuity|mcp_whitemagic_whitemagic_gen3_memory_recall|mcp_wm-research-engine_research_hunt|mcp_firecrawl_firecrawl_scrape",
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
                "matcher": "run_command|read_file|write_file|edit_file|call_mcp_tool|mcp_whitemagic-agy-kit_fast_run_checks|mcp_whitemagic-agy-kit_fast_apply_edits|mcp_agy-fast_fast_run_checks|mcp_agy-fast_fast_apply_edits",
                "hooks": [
                    {"type": "command", "command": "if command -v wm-squeeze >/dev/null 2>&1; then exec wm-squeeze --post-tool; elif [ -x \"$HOME/.local/bin/wm-squeeze\" ]; then exec \"$HOME/.local/bin/wm-squeeze\" --post-tool; else exec python3 -S -B ~/.gemini/config/hooks/post_tool_sim.py; fi", "timeout": 5}
                ],
            }
        ],
    },
    "agy-longevity-hud": {
        "enabled": True,
        "PostInvocation": [
            {
                "type": "command",
                "command": "if command -v wm-squeeze >/dev/null 2>&1; then exec wm-squeeze --hud; elif [ -x \"$HOME/.local/bin/wm-squeeze\" ]; then exec \"$HOME/.local/bin/wm-squeeze\" --hud; else exec python3 -S -B ~/.gemini/config/hooks/longevity_hud.py; fi",
                "timeout": 3,
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
    echo "=== 2. Installing unified native plugin: whitemagic-agy-kit v4.4.2 ==="
    # Build fused TS MCP & benchmark bundle if needed (probe PATH, ~/.local/bin, nvm, homebrew, volta, fnm)
    TURBO_MCP_DIR="${KIT_DIR}/tools/agy-turbo-mcp"
    NODE_BIN=""
    for cand in \
        "$(command -v node 2>/dev/null || true)" \
        "${LOCAL_BIN}/node" \
        $(ls -1d "${HOME}"/.nvm/versions/node/*/bin/node 2>/dev/null | sort -V -r || true) \
        /opt/homebrew/bin/node \
        /usr/local/bin/node \
        "${HOME}/.volta/bin/node" \
        $(ls -1d "${HOME}"/.fnm/node-versions/*/installation/bin/node 2>/dev/null | sort -V -r || true); do
        if [ -n "$cand" ] && [ -x "$cand" ]; then
            NODE_BIN="$cand"
            break
        fi
    done
    if [ -n "$NODE_BIN" ] && [ -x "$NODE_BIN" ]; then
        if [ "$NODE_BIN" != "${LOCAL_BIN}/node" ]; then
            ln -sf "$NODE_BIN" "${LOCAL_BIN}/node"
        fi
        if [ -d "$TURBO_MCP_DIR" ] && [ ! -f "${KIT_DIR}/plugin/dist/mcp-server.mjs" -o "${TURBO_MCP_DIR}/src/mcp-server.ts" -nt "${KIT_DIR}/plugin/dist/mcp-server.mjs" ]; then
            if [ -d "${TURBO_MCP_DIR}/node_modules" ] && command -v npm >/dev/null 2>&1; then
                (cd "$TURBO_MCP_DIR" && npm run build)
            fi
        fi
    else
        echo "[WARN] Node.js (>=20) not found; fused 5-tool 'fast' MCP will fall back to 1-tool 'codegraph' MCP."
    fi

    TARGET_PLUGIN="${PLUGINS_DIR}/whitemagic-agy-kit"
    rm -rf "$TARGET_PLUGIN"
    cp -R "${KIT_DIR}/plugin" "$TARGET_PLUGIN"
    mkdir -p "${TARGET_PLUGIN}/models"
    cp "${KIT_DIR}/models/manifest.json" "${TARGET_PLUGIN}/models/manifest.json"
    chmod +x "${TARGET_PLUGIN}/hooks/"*.py

    python3 - "$LOCAL_BIN" "$TARGET_PLUGIN" "${HOME}/.gemini/config/mcp_config.json" <<'PY'
import glob, json, os, shutil, sys
from pathlib import Path

local_bin = Path(sys.argv[1])
target_plugin = Path(sys.argv[2])
global_mcp_path = Path(sys.argv[3])
servers = {}

def resolve_node() -> Path | None:
    cands = [local_bin / "node"]
    which_node = shutil.which("node")
    if which_node:
        cands.append(Path(which_node))
    home = Path.home()
    for pat in (
        str(home / ".nvm" / "versions" / "node" / "*" / "bin" / "node"),
        str(home / ".fnm" / "node-versions" / "*" / "installation" / "bin" / "node"),
    ):
        for p in sorted(glob.glob(pat), reverse=True):
            cands.append(Path(p))
    cands.extend([Path("/opt/homebrew/bin/node"), Path("/usr/local/bin/node"), home / ".volta" / "bin" / "node"])
    for c in cands:
        if c.is_file() and os.access(c, os.X_OK):
            return c
    return None

node_bin = resolve_node()
mcp_js = target_plugin / "dist" / "mcp-server.mjs"
if node_bin and mcp_js.is_file():
    servers["fast"] = {
        "command": str(node_bin),
        "args": [str(mcp_js)],
        "tools": {
            "batch_search": {"eager": True},
            "apply_edits": {"eager": True},
            "read_slices": {"eager": True},
            "run_checks": {"eager": True},
            "code_graph": {"eager": True},
        },
    }
else:
    print(f"[WARN] Fused 'fast' MCP unavailable (node={node_bin}, mcp_js={mcp_js.is_file()}); falling back to 'codegraph'-only MCP.", file=sys.stderr)
    cg = local_bin / "wm-codegraph"
    if cg.is_file():
        servers["codegraph"] = {
            "command": str(cg),
            "args": ["--mcp"],
            "tools": {"code_graph": {"eager": True}},
        }

global_servers = {}
if global_mcp_path.is_file():
    try:
        global_servers = json.loads(global_mcp_path.read_text(encoding="utf-8")).get("mcpServers") or {}
    except Exception:
        global_servers = {}

tri_mcp = local_bin / "wm-tricameral-mcp"
engine = local_bin / "muninn-engine"
if engine.is_file() and tri_mcp.is_file() and "tricameral" not in global_servers:
    servers["tricameral"] = {
        "command": str(tri_mcp),
        "args": [],
        "tools": {"tricameral_dispatch": {"eager": True}},
    }

if servers:
    out = target_plugin / "mcp_config.json"
    out.write_text(json.dumps({"mcpServers": servers}, indent=2) + "\n", encoding="utf-8")
    print(f"Wired eager plugin MCPs in {out}: {list(servers.keys())}")
PY

    # Migrate away from legacy global hooks.json to prevent double-firing with native plugin hooks
    GLOBAL_HOOKS="${HOME}/.gemini/config/hooks.json"
    if [ -f "$GLOBAL_HOOKS" ] && [ "$(wc -c < "$GLOBAL_HOOKS")" -gt 4 ]; then
        cp "$GLOBAL_HOOKS" "${GLOBAL_HOOKS}.bak-$(date +%Y%m%d_%H%M%S)"
        printf '{}\n' > "$GLOBAL_HOOKS"
        echo "Backed up and cleared legacy global ${GLOBAL_HOOKS} (now managed by whitemagic-agy-kit plugin)"
    fi

    if command -v agy >/dev/null 2>&1; then
        agy plugin validate --strict "$TARGET_PLUGIN"
        agy plugin enable whitemagic-agy-kit
    else
        echo "[warn] 'agy' CLI not found on PATH; plugin copied to $TARGET_PLUGIN"
    fi

    # Step 2b: Deduplicate user skills that are already provided by whitemagic-agy-kit
    USER_SKILLS_DIR="${HOME}/.gemini/config/skills"
    if [ -d "$USER_SKILLS_DIR" ] && [ -d "${TARGET_PLUGIN}/skills" ]; then
        DEDUP_BAK="${HOME}/.gemini/config/skills-dedup-backup-$(date +%Y%m%d_%H%M%S)"
        MOVED_CNT=0
        for pskill in "${TARGET_PLUGIN}/skills/"*; do
            [ -d "$pskill" ] || continue
            sname="$(basename "$pskill")"
            if [ -e "${USER_SKILLS_DIR}/${sname}" ]; then
                mkdir -p "$DEDUP_BAK"
                mv "${USER_SKILLS_DIR}/${sname}" "${DEDUP_BAK}/${sname}"
                MOVED_CNT=$((MOVED_CNT + 1))
                echo "Deduplicated user skill '${sname}' -> ${DEDUP_BAK}/${sname}"
            fi
        done
        if [ "$MOVED_CNT" -gt 0 ]; then
            echo "Moved ${MOVED_CNT} duplicate user skill(s) outside ~/.gemini/config/skills/"
        fi
    fi
fi

echo "=== 3. Retiring standalone agy-fast (now fused into whitemagic-agy-kit v4.0.0) ==="
if [ "$SKIP_AGY_FAST" -eq 0 ] && command -v agy >/dev/null 2>&1; then
    if agy plugin list 2>/dev/null | grep -qE '^\- agy-fast \(enabled'; then
        agy plugin disable agy-fast >/dev/null 2>&1 || true
        echo "Disabled legacy standalone agy-fast plugin (tools now provided by whitemagic-agy-kit_fast)"
    fi
fi

echo "=== 4. Trimming unused heavy official plugins (reversible via 'agy plugin enable') ==="
if [ "$KEEP_HEAVY_PLUGINS" -eq 0 ] && command -v agy >/dev/null 2>&1; then
    for p in science data-agent-kit-plugin flutter firebase google_maps_platform android-cli-plugin; do
        agy plugin disable "$p" >/dev/null 2>&1 || true
    done
fi

echo "=== 5. Installing portable CLI tools, model manifest & cache-warmth statusline ==="
for tool in wm-sim agy-bench agy-mode agy-token-audit wm-local-digest wm-dream wm-handoff wm-models wm-huginn wm-muninn wm-tricameral wm-tricameral-mcp wm-grok-swarm wm-grok-fleet wm-grok-mcp wm-vps wm-triage; do
    if [ -f "${KIT_DIR}/bin/${tool}" ]; then
        install_bin "${KIT_DIR}/bin/${tool}" "${LOCAL_BIN}/${tool}"
    fi
done
cp "${KIT_DIR}/models/manifest.json" "${MODELS_DIR}/manifest.json"
install_bin "${KIT_DIR}/bin/statusline.py" "${AGY_CLI_DIR}/statusline.py"

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
    "command(wm-gen3)",
    "command(wm-sim)",
    "command(wm-squeeze --safe)",
    "command(wm-codegraph)",
    "mcp(whitemagic-agy-kit_fast/batch_search)",
    "mcp(whitemagic-agy-kit_fast/read_slices)",
    "mcp(whitemagic-agy-kit_fast/code_graph)",
    "mcp(whitemagic-agy-kit_codegraph/*)",
    "mcp(whitemagic-agy-kit_tricameral/*)",
    "command(wm-models)",
    "command(wm-huginn)",
    "command(wm-muninn)",
    "command(wm-tricameral)",
    "command(wm-handoff)",
    "command(wm-local-digest)",
    "command(wm-dream)",
    "command(wm-preflight-brief)",
    "command(wm-grok-swarm)",
    "command(wm-vps)",
    "command(wm-triage)",
    "mcp(grok/grok_dispatch)",
    "command(agy-bench)",
    "command(agy-mode)",
    "command(agy-token-audit)",
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
existing_deny = [d for d in (perms.get("deny") or []) if d != "write_file(.env)"]
ssh_deny = f"write_file({Path.home() / '.ssh'})"
if ssh_deny not in existing_deny:
    existing_deny.append(ssh_deny)
perms["allow"] = merged_allow
perms["deny"] = existing_deny
cur["permissions"] = perms

p.write_text(json.dumps(cur, indent=2) + "\n", encoding="utf-8")
p.chmod(0o600)
print(f"Updated {p} ({len(merged_allow)} allow, {len(existing_deny)} deny, statusLine enabled, model preserved={cur.get('model')!r})")

# Also add safe read-only MCP grants to ~/.gemini/config/config.json for Antigravity 2.0 Desktop App parity
cfg_path = Path.home() / ".gemini" / "config" / "config.json"
if cfg_path.is_file():
    try:
        shutil.copy2(cfg_path, cfg_path.with_name(f"config.json.bak-{datetime.now().strftime('%Y%m%d_%H%M%S')}"))
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        us = cfg.setdefault("userSettings", {})
        gpg = us.setdefault("globalPermissionGrants", {})
        gallow = gpg.setdefault("allow", [])
        for pat in (
            "mcp(whitemagic-agy-kit_fast/batch_search)",
            "mcp(whitemagic-agy-kit_fast/read_slices)",
            "mcp(whitemagic-agy-kit_fast/code_graph)",
            "mcp(whitemagic-agy-kit_codegraph/*)",
            "mcp(tricameral/*)",
            "mcp(grok/grok_dispatch)",
            "mcp(firecrawl/firecrawl_scrape)",
            "mcp(wm-research-engine/research_hunt)",
            "mcp(wm-research-engine/research_daydream)",
        ):
            if pat not in gallow:
                gallow.append(pat)
        cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        print(f"Updated {cfg_path} globalPermissionGrants.allow")
    except Exception as e:
        print(f"[warn] could not update {cfg_path}: {e}")
PY

echo "=== 7. Auto-wiring selective eager MCP tools for locally present servers ==="
SYNC_GLOBAL_FLAG="${SYNC_GLOBAL}" python3 - <<'PY'
import json, os, shutil
from pathlib import Path

home = Path.home()
mcp_path = home / ".gemini" / "config" / "mcp_config.json"
if mcp_path.exists():
    data = json.loads(mcp_path.read_text(encoding="utf-8"))
else:
    data = {"mcpServers": {}}

servers = data.setdefault("mcpServers", {})
changed = False

if os.environ.get("SYNC_GLOBAL_FLAG") == "1":
    engine = home / ".local" / "bin" / "muninn-engine"
    tri_mcp = home / ".local" / "bin" / "wm-tricameral-mcp"
    if engine.is_file() and tri_mcp.is_file():
        servers["tricameral"] = {
            "command": str(tri_mcp),
            "args": [],
            "tools": {"tricameral_dispatch": {"eager": True}},
        }
        changed = True

wm3 = shutil.which("wm3") or (str(home / ".local" / "bin" / "wm3") if (home / ".local" / "bin" / "wm3").exists() else None)
if wm3:
    entry = servers.setdefault("whitemagic", {"command": wm3, "args": ["serve", "--store", str(home / ".local" / "share" / "whitemagic" / "gen3")]})
    tools = entry.setdefault("tools", {})
    for t in ("memory_recall", "memory_get", "session_continuity", "session_checkpoint"):
        tools.setdefault(t, {"eager": True})
    changed = True

grok_mcp = shutil.which("wm-grok-mcp") or (str(home / ".local" / "bin" / "wm-grok-mcp") if (home / ".local" / "bin" / "wm-grok-mcp").exists() else None)
ca_bin = shutil.which("cursor-agent") or (str(home / ".local" / "bin" / "cursor-agent") if (home / ".local" / "bin" / "cursor-agent").exists() else None)
if grok_mcp and ca_bin:
    servers["grok"] = {
        "command": str(grok_mcp),
        "args": [],
        "tools": {"grok_dispatch": {"eager": True}},
    }
    changed = True

fc = shutil.which("firecrawl-local-mcp") or (str(home / ".local" / "bin" / "firecrawl-local-mcp") if (home / ".local" / "bin" / "firecrawl-local-mcp").exists() else None)
if fc:
    entry = servers.setdefault("firecrawl", {"command": fc, "disabled": False, "env": {"FIRECRAWL_CLOUD": "off"}})
    entry.setdefault("tools", {}).setdefault("firecrawl_scrape", {"eager": True})
    changed = True

if changed:
    mcp_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Verified selective eager MCP entries in {mcp_path}: {list(servers.keys())}")

# Also promote core continuity tools on the official whitemagic-plugins Gen3 plugin if installed
wm_plugin_mcp = home / ".gemini" / "config" / "plugins" / "whitemagic" / "mcp_config.json"
if wm_plugin_mcp.is_file():
    try:
        pdata = json.loads(wm_plugin_mcp.read_text(encoding="utf-8"))
        pservers = pdata.get("mcpServers") or {}
        if "whitemagic_gen3" in pservers:
            ptools = pservers["whitemagic_gen3"].setdefault("tools", {})
            pchanged = False
            for t in ("memory_recall", "session_continuity", "session_checkpoint"):
                if t not in ptools:
                    ptools[t] = {"eager": True}
                    pchanged = True
            if pchanged:
                wm_plugin_mcp.write_text(json.dumps(pdata, indent=2) + "\n", encoding="utf-8")
                print(f"Promoted selective eager Gen3 tools in {wm_plugin_mcp}")
    except Exception:
        pass
PY

if [ -n "$PULL_PROFILE" ]; then
    echo ""
    echo "=== 8. Pulling Tricameral model profile: ${PULL_PROFILE} ==="
    "${LOCAL_BIN}/wm-models" pull "$PULL_PROFILE"
fi

echo ""
echo "=== 9. Verification Audit ==="
export PATH="${LOCAL_BIN}:${PATH}"
wm-sim audit || true
wm-models list || true
echo ""
echo "Done! Launch with 'agy' (or 'agy-mode longevity' / 'agy-mode orchestrator')."
echo "To pull local Tricameral spores later: wm-models pull huginn | ravens | tricameral"
