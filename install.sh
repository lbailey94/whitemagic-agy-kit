#!/usr/bin/env bash
# WhiteMagic Turbo Kit v4.7.0 — Idempotent Multi-Host Cross-Platform Installer (Antigravity, Codex, Cursor)
# Installs:
#   1. Zero-dependency Rust binaries `wm-squeeze` (multi-host `--host agy|codex|cursor`), `wm-codegraph`, & `muninn-engine` -> ~/.local/bin/
#   2. Host adapters (`--hosts agy,codex,cursor` or `--hosts all`; default `agy`):
#      - agy:    Native Antigravity plugin `whitemagic-agy-kit` (+ eager MCPs + resident sidecar + settings)
#      - codex:  ~/.codex/hooks.json (PreToolUse/PostToolUse) + `[mcp_servers.wm_fast]` in ~/.codex/config.toml (+ optional `--prune-codex-office`)
#      - cursor: ~/.cursor/hooks.json (beforeShellExecution/beforeMCPExecution/preToolUse) + `wm_fast` in ~/.cursor/mcp.json
#   3. Shared portable 5-tool fast MCP bundle (`~/.local/share/whitemagic/wm-fast-mcp/mcp-server.mjs`)
#   4. Portable CLI tools (`wm-sim`, `agy-bench`, `agy-mode`, `agy-token-audit`, `wm-local-digest`, `wm-dream`, `wm-handoff`, `wm-reset`, `wm-cpr-bridge`, ...)
#   5. Optional Tricameral spore pull (`--with-huginn` | `--with-ravens` | `--with-tricameral`) via `wm-models`
set -euo pipefail

KIT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PLUGINS_DIR="${HOME}/.gemini/config/plugins"
LOCAL_BIN="${HOME}/.local/bin"
AGY_CLI_DIR="${HOME}/.gemini/antigravity-cli"
FAST_MCP_DIR="${HOME}/.local/share/whitemagic/wm-fast-mcp"
MODELS_DIR="${WM_MODELS_DIR:-${HOME}/.local/share/whitemagic/models}"
SKIP_AGY_FAST=0
SKIP_TRICAMERAL_ENGINE=0
KEEP_HEAVY_PLUGINS=0
SYNC_GLOBAL=0
PRUNE_CODEX_OFFICE=0
CURSOR_FLEET_ONLY=0
WIRE_CURSOR_MCP=0
HOSTS="${WM_HOSTS:-}"
HOSTS_EXPLICIT=0
if [ -n "${WM_HOSTS:-}" ]; then
    HOSTS_EXPLICIT=1
fi
PULL_PROFILE=""
CURSOR_SAFE_ALLOW=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --skip-agy-fast) SKIP_AGY_FAST=1; shift ;;
        --skip-tricameral-engine) SKIP_TRICAMERAL_ENGINE=1; shift ;;
        --keep-heavy-plugins) KEEP_HEAVY_PLUGINS=1; shift ;;
        --sync-global) SYNC_GLOBAL=1; shift ;;
        --prune-codex-office) PRUNE_CODEX_OFFICE=1; shift ;;
        --cursor-fleet-only) CURSOR_FLEET_ONLY=1; shift ;;
        --wire-cursor-mcp) WIRE_CURSOR_MCP=1; shift ;;
        --cursor-safe-allow) CURSOR_SAFE_ALLOW=1; shift ;;
        --cursor) HOSTS="agy,cursor"; HOSTS_EXPLICIT=1; shift ;;
        --cursor-only) HOSTS="cursor"; HOSTS_EXPLICIT=1; shift ;;
        --hosts)
            HOSTS="${2:?--hosts requires a comma-separated list (e.g. agy,codex,cursor)}"
            HOSTS_EXPLICIT=1
            shift 2
            ;;
        --hosts=*)
            HOSTS="${1#--hosts=}"
            HOSTS_EXPLICIT=1
            shift
            ;;
        --with-huginn) PULL_PROFILE="huginn"; shift ;;
        --with-ravens) PULL_PROFILE="ravens"; shift ;;
        --with-tricameral) PULL_PROFILE="tricameral"; shift ;;
        -h|--help)
            echo "Usage: ./install.sh [--cursor-fleet-only [--wire-cursor-mcp]] [--hosts agy,codex,cursor|all|--cursor|--cursor-only] [--cursor-safe-allow] [--prune-codex-office] [--skip-agy-fast] [--skip-tricameral-engine] [--keep-heavy-plugins] [--sync-global] [--with-huginn|--with-ravens|--with-tricameral]"
            exit 0
            ;;
        *) shift ;;
    esac
done

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

if [ "$CURSOR_FLEET_ONLY" -eq 1 ]; then
    echo "=== Standalone Cursor / Grok Fleet Install (zero Rust/Node/WhiteMagic deps) ==="
    mkdir -p "$LOCAL_BIN"
    for tool in wm-grok-fleet wm-grok-swarm wm-grok-mcp; do
        install_bin "${KIT_DIR}/bin/${tool}" "${LOCAL_BIN}/${tool}"
    done
    ln -sf "${LOCAL_BIN}/wm-grok-fleet" "${LOCAL_BIN}/cursor-fleet"
    echo "Installed: wm-grok-fleet (alias: cursor-fleet), wm-grok-swarm, wm-grok-mcp -> ${LOCAL_BIN}"
    if [ "$WIRE_CURSOR_MCP" -eq 1 ]; then
        python3 - <<'PY'
import json
from pathlib import Path
mcp_path = Path.home() / ".cursor" / "mcp.json"
mcp_path.parent.mkdir(parents=True, exist_ok=True)
data = json.loads(mcp_path.read_text(encoding="utf-8")) if mcp_path.is_file() else {}
servers = data.setdefault("mcpServers", {})
servers["grok"] = {"command": str(Path.home() / ".local" / "bin" / "wm-grok-mcp"), "args": []}
mcp_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
print(f"Registered `grok` MCP server in {mcp_path}")
PY
    fi
    "${LOCAL_BIN}/wm-grok-fleet" doctor --fast || true
    exit 0
fi

if [ "$HOSTS_EXPLICIT" -eq 0 ] && [ -z "$HOSTS" ]; then
    if [ -d "${HOME}/.cursor" ]; then
        HOSTS="agy,cursor"
    else
        HOSTS="agy"
    fi
fi
if [ "$HOSTS" = "all" ]; then
    HOSTS="agy,codex,cursor"
fi
HAS_AGY=0; HAS_CODEX=0; HAS_CURSOR=0
case ",${HOSTS}," in *,agy,*) HAS_AGY=1 ;; esac
case ",${HOSTS}," in *,codex,*) HAS_CODEX=1 ;; esac
case ",${HOSTS}," in *,cursor,*) HAS_CURSOR=1 ;; esac

mkdir -p "$LOCAL_BIN" "$MODELS_DIR" "$FAST_MCP_DIR"
if [ "$HAS_AGY" -eq 1 ]; then
    mkdir -p "$PLUGINS_DIR" "$AGY_CLI_DIR" "${HOME}/.gemini/config/hooks"
fi

# Convenience macOS / Gen3 CLI symlinks when present locally
if [ ! -e "${LOCAL_BIN}/wm3" ] && [ -x "${LOCAL_BIN}/wm-gen3" ]; then
    ln -sf "${LOCAL_BIN}/wm-gen3" "${LOCAL_BIN}/wm3"
fi
if [ "$(uname -s)" = "Darwin" ] && [ ! -e "${LOCAL_BIN}/cursor" ] && [ -x "/Applications/Cursor.app/Contents/Resources/app/bin/cursor" ]; then
    ln -sf "/Applications/Cursor.app/Contents/Resources/app/bin/cursor" "${LOCAL_BIN}/cursor"
fi
echo "=== 1. Building Rust binaries: wm-squeeze, wm-codegraph & Safe-Rust muninn-engine ==="
if [ "${WM_SKIP_CARGO:-0}" -eq 1 ]; then
    echo "Skipping Cargo build (WM_SKIP_CARGO=1)"
elif command -v cargo >/dev/null 2>&1; then
    SQ_TARGET="${CARGO_TARGET_DIR:-${KIT_DIR}/tools/wm-squeeze/target}"
    CG_TARGET="${CARGO_TARGET_DIR:-${KIT_DIR}/tools/wm-codegraph/target}"
    TRI_TARGET="${CARGO_TARGET_DIR:-${KIT_DIR}/tools/wm-tricameral-engine/target}"
    cargo build --release --manifest-path "${KIT_DIR}/tools/wm-squeeze/Cargo.toml"
    install_bin "${SQ_TARGET}/release/wm-squeeze" "${LOCAL_BIN}/wm-squeeze"
    cargo build --release --manifest-path "${KIT_DIR}/tools/wm-codegraph/Cargo.toml"
    install_bin "${CG_TARGET}/release/wm-codegraph" "${LOCAL_BIN}/wm-codegraph"
    echo "Installed ${LOCAL_BIN}/wm-squeeze & ${LOCAL_BIN}/wm-codegraph"
    if [ "$SKIP_TRICAMERAL_ENGINE" -eq 0 ] && [ -f "${KIT_DIR}/tools/wm-tricameral-engine/Cargo.toml" ]; then
        if cargo build --release --manifest-path "${KIT_DIR}/tools/wm-tricameral-engine/Cargo.toml"; then
            install_bin "${TRI_TARGET}/release/muninn-engine" "${LOCAL_BIN}/muninn-engine"
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
TURBO_MCP_DIR="${KIT_DIR}/tools/agy-turbo-mcp"
if [ -n "$NODE_BIN" ] && [ -x "$NODE_BIN" ]; then
    if [ "$NODE_BIN" != "${LOCAL_BIN}/node" ]; then
        ln -sf "$NODE_BIN" "${LOCAL_BIN}/node"
    fi
    if [ -d "$TURBO_MCP_DIR" ] && [ ! -f "${KIT_DIR}/plugin/dist/mcp-server.mjs" -o "${TURBO_MCP_DIR}/src/mcp-server.ts" -nt "${KIT_DIR}/plugin/dist/mcp-server.mjs" ]; then
        if [ -d "${TURBO_MCP_DIR}/node_modules" ] && command -v npm >/dev/null 2>&1; then
            (cd "$TURBO_MCP_DIR" && npm run build)
        fi
    fi
fi
if [ -f "${KIT_DIR}/plugin/dist/mcp-server.mjs" ]; then
    install_bin "${KIT_DIR}/plugin/dist/mcp-server.mjs" "${FAST_MCP_DIR}/mcp-server.mjs"
fi

if [ "$HAS_AGY" -eq 1 ]; then

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
                "matcher": "run_command|read_file|write_file|edit_file|manage_task|swarm|post_message|invoke_subagent|mcp_whitemagic-agy-kit_fast_run_checks|mcp_whitemagic-agy-kit_fast_apply_edits|mcp_agy-fast_fast_run_checks|mcp_agy-fast_fast_apply_edits|mcp_whitemagic_session_continuity|mcp_whitemagic_memory_recall|mcp_whitemagic_whitemagic_gen3_session_continuity|mcp_whitemagic_whitemagic_gen3_memory_recall|mcp_wm-research-engine_research_hunt|mcp_firecrawl_firecrawl_scrape",
                "hooks": [
                    {
                        "type": "command",
                        "command": 'if command -v wm-squeeze >/dev/null 2>&1; then exec wm-squeeze --pre-tool --py "$HOME/.gemini/config/hooks/pre_tool_guard.py"; elif [ -x "$HOME/.local/bin/wm-squeeze" ]; then exec "$HOME/.local/bin/wm-squeeze" --pre-tool --py "$HOME/.gemini/config/hooks/pre_tool_guard.py"; else exec python3 -S -B ~/.gemini/config/hooks/pre_tool_guard.py; fi',
                        "timeout": 5,
                    }
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
    echo "=== 2. Installing unified native plugin: whitemagic-agy-kit v4.7.0 ==="
    if [ -z "$NODE_BIN" ] || [ ! -x "$NODE_BIN" ]; then
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
models_dir = Path(os.environ.get("WM_MODELS_DIR") or str(Path.home() / ".local" / "share" / "whitemagic" / "models"))
has_spores = models_dir.is_dir() and any(models_dir.glob("*.tm7b"))
if engine.is_file() and tri_mcp.is_file() and "tricameral" not in global_servers:
    tri_cfg = {"command": str(tri_mcp), "args": []}
    if has_spores:
        tri_cfg["tools"] = {"tricameral_dispatch": {"eager": True}}
    else:
        tri_cfg["tools"] = {"tricameral_dispatch": {"eager": False}}
    servers["tricameral"] = tri_cfg

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
fi # end HAS_AGY plugin install

echo "=== 5. Installing portable CLI tools & model manifest ==="
for tool in wm-sim agy-bench agy-mode agy-token-audit wm-local-digest wm-dream wm-handoff wm-reset wm-cpr-bridge wm-models wm-huginn wm-muninn wm-tricameral wm-tricameral-mcp wm-grok-swarm wm-grok-fleet wm-grok-mcp wm-vps wm-triage; do
    if [ -f "${KIT_DIR}/bin/${tool}" ]; then
        install_bin "${KIT_DIR}/bin/${tool}" "${LOCAL_BIN}/${tool}"
    fi
done
if [ -f "${LOCAL_BIN}/wm-grok-fleet" ]; then
    ln -sf "${LOCAL_BIN}/wm-grok-fleet" "${LOCAL_BIN}/cursor-fleet"
fi
cp "${KIT_DIR}/models/manifest.json" "${MODELS_DIR}/manifest.json"

if [ "$HAS_AGY" -eq 1 ]; then
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
    models_dir = Path(os.environ.get("WM_MODELS_DIR") or str(home / ".local" / "share" / "whitemagic" / "models"))
    has_spores = models_dir.is_dir() and any(models_dir.glob("*.tm7b"))
    if engine.is_file() and tri_mcp.is_file():
        servers["tricameral"] = {
            "command": str(tri_mcp),
            "args": [],
            "tools": {"tricameral_dispatch": {"eager": bool(has_spores)}},
        }
        changed = True

wm3 = shutil.which("wm3") or (str(home / ".local" / "bin" / "wm3") if (home / ".local" / "bin" / "wm3").exists() else None)
if wm3:
    entry = servers.setdefault("whitemagic", {"command": wm3, "args": ["serve", "--store", str(home / ".local" / "share" / "whitemagic" / "gen3")]})
    tools = entry.setdefault("tools", {})
    for t in ("memory_recall", "memory_get", "session_continuity"):
        tools.setdefault(t, {"eager": True})
    # Turn-0 trim (v4.7.0): session_checkpoint is always refused on --readonly; keep it lazy
    if "session_checkpoint" in tools:
        tools.pop("session_checkpoint", None)
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
    # Turn-0 trim (v4.7.0): keep firecrawl_scrape lazy-loaded on demand
    fc_tools = entry.get("tools")
    if isinstance(fc_tools, dict) and "firecrawl_scrape" in fc_tools:
        fc_tools.pop("firecrawl_scrape", None)
        if not fc_tools:
            entry.pop("tools", None)
    changed = True

# Turn-0 trim (v4.7.0): keep low-frequency wm-research-engine tools lazy-loaded
if isinstance(servers.get("wm-research-engine"), dict):
    re_tools = servers["wm-research-engine"].get("tools")
    if isinstance(re_tools, dict):
        for rt in ("research_hunt", "research_daydream"):
            if rt in re_tools:
                re_tools.pop(rt, None)
                changed = True
        if not re_tools:
            servers["wm-research-engine"].pop("tools", None)

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
            for t in ("memory_recall", "session_continuity"):
                if t not in ptools:
                    ptools[t] = {"eager": True}
                    pchanged = True
            if "session_checkpoint" in ptools:
                ptools.pop("session_checkpoint", None)
                pchanged = True
            if pchanged:
                wm_plugin_mcp.write_text(json.dumps(pdata, indent=2) + "\n", encoding="utf-8")
                print(f"Promoted selective eager Gen3 tools in {wm_plugin_mcp}")
    except Exception:
        pass
PY
fi # end HAS_AGY settings & MCP

if [ "$HAS_CODEX" -eq 1 ]; then
    echo "=== 7b. Configuring Codex adapter (~/.codex/hooks.json & ~/.codex/config.toml) ==="
    mkdir -p "${HOME}/.codex"
    PRUNE_OFFICE="${PRUNE_CODEX_OFFICE}" FAST_MJS="${FAST_MCP_DIR}/mcp-server.mjs" NODE_CMD="${NODE_BIN:-node}" python3 - <<'PY'
import json, os, re, shutil
from datetime import datetime
from pathlib import Path

home = Path.home()
codex_dir = home / ".codex"
ts = datetime.now().strftime("%Y%m%d_%H%M%S")

# 1. Merge ~/.codex/hooks.json idempotently
hooks_path = codex_dir / "hooks.json"
if hooks_path.is_file():
    shutil.copy2(hooks_path, hooks_path.with_name(f"hooks.json.bak-{ts}"))
    try:
        hdata = json.loads(hooks_path.read_text(encoding="utf-8"))
    except Exception:
        hdata = {}
else:
    hdata = {}
if not isinstance(hdata, dict):
    hdata = {}
hooks = hdata.setdefault("hooks", {})

def _without_wm(entries):
    out = []
    for ent in entries or []:
        inner = ent.get("hooks") if isinstance(ent, dict) else None
        if isinstance(inner, list) and any("wm-squeeze" in str(h.get("command", "")) for h in inner if isinstance(h, dict)):
            continue
        out.append(ent)
    return out

pre = _without_wm(hooks.get("PreToolUse"))
pre.append({
    "matcher": "Bash|apply_patch",
    "hooks": [{
        "type": "command",
        "command": 'if command -v wm-squeeze >/dev/null 2>&1; then exec wm-squeeze --pre-tool --host codex; elif [ -x "$HOME/.local/bin/wm-squeeze" ]; then exec "$HOME/.local/bin/wm-squeeze" --pre-tool --host codex; fi',
        "timeout": 5,
    }],
})
hooks["PreToolUse"] = pre

post = _without_wm(hooks.get("PostToolUse"))
post.append({
    "matcher": "Bash",
    "hooks": [{
        "type": "command",
        "command": 'if command -v wm-squeeze >/dev/null 2>&1; then exec wm-squeeze --post-tool --host codex; elif [ -x "$HOME/.local/bin/wm-squeeze" ]; then exec "$HOME/.local/bin/wm-squeeze" --post-tool --host codex; fi',
        "timeout": 5,
    }],
})
hooks["PostToolUse"] = post
hooks_path.write_text(json.dumps(hdata, indent=2) + "\n", encoding="utf-8")
print(f"Updated {hooks_path} (PreToolUse + PostToolUse for Codex)")

# 2. Merge [mcp_servers.wm_fast] into ~/.codex/config.toml (preserving unrelated keys & plugins)
cfg_path = codex_dir / "config.toml"
node_cmd = os.environ.get("NODE_CMD") or "node"
fast_mjs = os.environ.get("FAST_MJS") or ""
cur_toml = cfg_path.read_text(encoding="utf-8") if cfg_path.is_file() else ""
if cfg_path.is_file():
    shutil.copy2(cfg_path, cfg_path.with_name(f"config.toml.bak-{ts}"))

if fast_mjs and Path(fast_mjs).is_file():
    block = (
        "# WhiteMagic Turbo Fast 5-tool MCP (v4.6.0)\n"
        "[mcp_servers.wm_fast]\n"
        f'command = "{node_cmd}"\n'
        f'args = ["{fast_mjs}"]\n'
        "startup_timeout_sec = 15\n"
        "tool_timeout_sec = 60\n"
    )
    if "[mcp_servers.wm_fast]" in cur_toml:
        cur_toml = re.sub(
            r"(?ms)^(?:# WhiteMagic Turbo Fast[^\n]*\n)?\[mcp_servers\.wm_fast\]\n.*?(?=\n\[|\Z)",
            block.rstrip("\n"),
            cur_toml,
            count=1,
        )
    else:
        if cur_toml and not cur_toml.endswith("\n"):
            cur_toml += "\n"
        cur_toml += ("\n" if cur_toml else "") + block

if os.environ.get("PRUNE_OFFICE") == "1" and cur_toml:
    for plugin_id in (
        "documents@openai-primary-runtime",
        "pdf@openai-primary-runtime",
        "spreadsheets@openai-primary-runtime",
        "presentations@openai-primary-runtime",
        "template-creator@openai-primary-runtime",
        "visualize@openai-bundled",
    ):
        pat = rf'(\[plugins\."{re.escape(plugin_id)}"\]\s*\n\s*enabled\s*=\s*)true'
        cur_toml = re.sub(pat, r"\1false", cur_toml)

if cur_toml:
    cfg_path.write_text(cur_toml.rstrip("\n") + "\n", encoding="utf-8")
    print(f"Updated {cfg_path} ([mcp_servers.wm_fast])")
PY
fi

if [ "$HAS_CURSOR" -eq 1 ]; then
    echo "=== 7c. Configuring Cursor adapter (~/.cursor/{hooks.json,mcp.json,hooks,rules,agents,cli-config.json}) ==="
    mkdir -p "${HOME}/.cursor/hooks" "${HOME}/.cursor/rules" "${HOME}/.cursor/agents"
    for h in session_start_brief.py pre_tool_guard.py post_tool_sim.py stop_telemetry.py; do
        install_bin "${KIT_DIR}/plugin/hooks/${h}" "${HOME}/.cursor/hooks/${h}"
    done
    install_bin "${KIT_DIR}/bin/statusline.py" "${HOME}/.cursor/statusline.py"

    if [ -f "${KIT_DIR}/cursor/rules/whitemagic-turbo.mdc" ]; then
        cp "${KIT_DIR}/cursor/rules/whitemagic-turbo.mdc" "${HOME}/.cursor/rules/whitemagic-turbo.mdc"
    fi
    for ag in "${KIT_DIR}/cursor/agents/"*.md; do
        [ -f "$ag" ] || continue
        cp "$ag" "${HOME}/.cursor/agents/$(basename "$ag")"
    done

    CURSOR_SAFE_ALLOW="${CURSOR_SAFE_ALLOW}" KIT_ROOT="${KIT_DIR}" FAST_MJS="${FAST_MCP_DIR}/mcp-server.mjs" NODE_CMD="${NODE_BIN:-node}" LOCAL_BIN_DIR="${LOCAL_BIN}" python3 - <<'PY'
import glob, json, os, shutil
from datetime import datetime
from pathlib import Path

home = Path.home()
kit_dir = Path(os.environ.get("KIT_ROOT") or ".")
cursor_dir = home / ".cursor"
local_bin = Path(os.environ.get("LOCAL_BIN_DIR") or str(home / ".local" / "bin"))
ts = datetime.now().strftime("%Y%m%d_%H%M%S")
gui_path = f"{local_bin}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
path_pref = 'export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"; '

# 1. Merge ~/.cursor/hooks.json (version: 1) cleanly across all 7 Cursor hook events
hooks_path = cursor_dir / "hooks.json"
if hooks_path.is_file():
    shutil.copy2(hooks_path, hooks_path.with_name(f"hooks.json.bak-{ts}"))
    try:
        hdata = json.loads(hooks_path.read_text(encoding="utf-8"))
    except Exception:
        hdata = {}
else:
    hdata = {}
if not isinstance(hdata, dict):
    hdata = {}
hdata["version"] = 1
hooks = hdata.setdefault("hooks", {})

def _strip_wm(items):
    out = []
    for it in (items or []):
        cmd = str(it.get("command", "")) if isinstance(it, dict) else ""
        if any(tok in cmd for tok in ("wm-squeeze", "session_start_brief.py", "pre_tool_guard.py", "post_tool_sim.py", "stop_telemetry.py")):
            continue
        out.append(it)
    return out

def _sq_pre(flags: str) -> str:
    return (
        path_pref
        + f'if command -v wm-squeeze >/dev/null 2>&1; then exec wm-squeeze {flags}; '
        + f'elif [ -x "$HOME/.local/bin/wm-squeeze" ]; then exec "$HOME/.local/bin/wm-squeeze" {flags}; '
        + 'else exec python3 -S -B "$HOME/.cursor/hooks/pre_tool_guard.py" --host cursor; fi'
    )

def _sq_post(flags: str) -> str:
    return (
        path_pref
        + f'if command -v wm-squeeze >/dev/null 2>&1; then exec wm-squeeze {flags}; '
        + f'elif [ -x "$HOME/.local/bin/wm-squeeze" ]; then exec "$HOME/.local/bin/wm-squeeze" {flags}; '
        + 'else exec python3 -S -B "$HOME/.cursor/hooks/post_tool_sim.py"; fi'
    )

cursor_specs = (
    ("sessionStart", {
        "command": path_pref + 'exec python3 -S -B "$HOME/.cursor/hooks/session_start_brief.py" --host cursor',
        "timeout": 5,
    }),
    ("beforeShellExecution", {
        "command": _sq_pre('--pre-tool --host cursor --event beforeShellExecution --py "$HOME/.cursor/hooks/pre_tool_guard.py"'),
        "timeout": 5,
    }),
    ("beforeMCPExecution", {
        "matcher": "wm_fast|whitemagic|whitemagic_crystals|whitemagic_receipts|tricameral|grok|firecrawl|wm-research-engine|whitemagic_gen4",
        "command": _sq_pre('--pre-tool --host cursor --event beforeMCPExecution --py "$HOME/.cursor/hooks/pre_tool_guard.py"'),
        "timeout": 5,
    }),
    ("preToolUse", {
        "matcher": "Shell|Read|Write|Edit|StrReplace",
        "command": _sq_pre('--pre-tool --host cursor --event preToolUse --py "$HOME/.cursor/hooks/pre_tool_guard.py"'),
        "timeout": 5,
    }),
    ("postToolUse", {
        "matcher": "Shell|Read|Write|StrReplace|MCP:.*",
        "command": _sq_post("--post-tool --host cursor"),
        "timeout": 5,
    }),
    ("postToolUseFailure", {
        "command": _sq_post("--post-tool --host cursor"),
        "timeout": 5,
    }),
    ("stop", {
        "command": path_pref + 'exec python3 -S -B "$HOME/.cursor/hooks/stop_telemetry.py" --host cursor',
        "timeout": 5,
    }),
)

for ev, entry in cursor_specs:
    lst = _strip_wm(hooks.get(ev))
    lst.append(entry)
    hooks[ev] = lst

hooks_path.write_text(json.dumps(hdata, indent=2) + "\n", encoding="utf-8")
print(f"Updated {hooks_path} ({', '.join(ev for ev, _ in cursor_specs)})")

# 2. Merge wm_fast + locally present sovereign MCP servers into ~/.cursor/mcp.json without disturbing existing whitemagic
mcp_path = cursor_dir / "mcp.json"
if mcp_path.is_file():
    shutil.copy2(mcp_path, mcp_path.with_name(f"mcp.json.bak-{ts}"))
    try:
        mdata = json.loads(mcp_path.read_text(encoding="utf-8"))
    except Exception:
        mdata = {}
else:
    mdata = {}
if not isinstance(mdata, dict):
    mdata = {}
servers = mdata.setdefault("mcpServers", {})
base_env = {"PATH": gui_path}
node_cmd = os.environ.get("NODE_CMD") or "node"
fast_mjs = os.environ.get("FAST_MJS") or ""

def _find_local(name: str) -> str | None:
    cand = local_bin / name
    if cand.exists() and os.access(cand, os.X_OK):
        return str(cand)
    return shutil.which(name)

if fast_mjs and Path(fast_mjs).is_file():
    servers["wm_fast"] = {"command": node_cmd, "args": [fast_mjs], "env": dict(base_env)}
elif _find_local("wm-codegraph"):
    servers["wm_fast"] = {"command": _find_local("wm-codegraph"), "args": ["--mcp"], "env": dict(base_env)}

if "whitemagic" not in servers:
    wm_gen3 = _find_local("wm-gen3") or _find_local("wm3")
    wm_bin = _find_local("wm")
    # Store discovery: explicit $WM_GEN3_STORE first, then well-known per-user locations.
    gen3_store = os.environ.get("WM_GEN3_STORE") or ""
    if not gen3_store:
        for cand_store in (
            home / ".local" / "share" / "whitemagic-gen3",
            home / ".local" / "share" / "whitemagic" / "gen3",
        ):
            if Path(cand_store, "data.mdb").is_file():
                gen3_store = str(cand_store.resolve())
                break
        else:
            gen3_store = str(home / ".local" / "share" / "whitemagic" / "gen3")
    agy_mcp_p = home / ".gemini" / "config" / "mcp_config.json"
    agy_wm_cfg = {}
    if agy_mcp_p.is_file():
        try:
            agy_wm_cfg = (json.loads(agy_mcp_p.read_text(encoding="utf-8")).get("mcpServers") or {}).get("whitemagic") or {}
        except Exception:
            agy_wm_cfg = {}
    if wm_gen3 and Path(gen3_store, "data.mdb").is_file():
        a_cmd = str(agy_wm_cfg.get("command") or "")
        a_args = [str(x) for x in (agy_wm_cfg.get("args") or [])]
        agy_owns_gen3 = ("wm3" in a_cmd or "wm-gen3" in a_cmd) and "--readonly" not in a_args
        wm_args = ["serve", "--store", gen3_store, "--profile", "cyberbrain"]
        if agy_owns_gen3:
            wm_args.append("--readonly")
        servers["whitemagic"] = {
            "command": wm_gen3,
            "args": wm_args,
            "env": {**base_env, "RUST_LOG": "warn"},
        }
    elif wm_bin:
        a_cmd = str(agy_wm_cfg.get("command") or "")
        a_args = [str(x) for x in (agy_wm_cfg.get("args") or [])]
        agy_owns_wm = Path(a_cmd).name == "wm" and "--readonly" not in a_args
        wm_args = ["serve", "--profile", "curated"]
        if agy_owns_wm:
            wm_args.append("--readonly")
        servers["whitemagic"] = {
            "command": wm_bin,
            "args": wm_args,
            "env": {**base_env, "RUST_LOG": "warn", "WM_TOOL_ALLOWLIST": "wm"},
        }

tri_mcp = _find_local("wm-tricameral-mcp")
if tri_mcp and _find_local("muninn-engine"):
    servers.setdefault("tricameral", {"command": tri_mcp, "args": [], "env": dict(base_env)})

if node_cmd and (Path(node_cmd).is_file() or shutil.which(node_cmd)):
    for srv_key, bin_name in (
        ("whitemagic_crystals", "whitemagic-crystals-mcp"),
        ("whitemagic_receipts", "whitemagic-receipts-mcp"),
    ):
        cand_b = local_bin / bin_name
        if cand_b.exists() and srv_key not in servers:
            servers[srv_key] = {
                "command": node_cmd,
                "args": [str(cand_b.resolve())],
                "env": dict(base_env),
            }

grok_mcp = _find_local("wm-grok-mcp")
if grok_mcp and _find_local("cursor-agent"):
    servers.setdefault("grok", {"command": grok_mcp, "args": [], "env": dict(base_env)})

fc_mcp = _find_local("firecrawl-local-mcp")
if fc_mcp:
    servers.setdefault("firecrawl", {"command": fc_mcp, "env": {**base_env, "FIRECRAWL_CLOUD": "off"}})

wm4_mcp = _find_local("wm4-mcp")
if wm4_mcp:
    servers.setdefault("whitemagic_gen4", {"command": wm4_mcp, "args": ["--mcp"], "env": dict(base_env)})

mcp_path.write_text(json.dumps(mdata, indent=2) + "\n", encoding="utf-8")
print(f"Updated {mcp_path} servers={list(servers.keys())}")

# 3. Sync plugin/skills/* into Cursor Personal User Agent Store when present under HOME
store_cands = sorted(
    glob.glob(str(home / "Library" / "Application Support" / "Cursor" / "AgentStores" / "cursor_agent_stores" / "u*" / "files"))
    + glob.glob(str(home / ".config" / "Cursor" / "AgentStores" / "cursor_agent_stores" / "u*" / "files"))
)
if store_cands and (kit_dir / "plugin" / "skills").is_dir():
    skills_dst = Path(store_cands[0]) / "skills"
    skills_dst.mkdir(parents=True, exist_ok=True)
    synced_skills = []
    for src_skill in sorted((kit_dir / "plugin" / "skills").iterdir()):
        if not src_skill.is_dir() or not (src_skill / "SKILL.md").is_file():
            continue
        dst_skill = skills_dst / src_skill.name
        if dst_skill.exists():
            shutil.rmtree(dst_skill)
        shutil.copytree(src_skill, dst_skill)
        synced_skills.append(src_skill.name)
    print(f"Synced {len(synced_skills)} skills to {skills_dst}: {synced_skills}")

# 4. Non-destructively configure ~/.cursor/cli-config.json (statusLine; read-only allowlist only with --cursor-safe-allow)
cli_cfg_path = cursor_dir / "cli-config.json"
if cli_cfg_path.is_file():
    shutil.copy2(cli_cfg_path, cli_cfg_path.with_name(f"cli-config.json.bak-{ts}"))
    try:
        cli_cfg = json.loads(cli_cfg_path.read_text(encoding="utf-8"))
    except Exception:
        cli_cfg = {"version": 1}
else:
    cli_cfg = {"version": 1}
if not isinstance(cli_cfg, dict):
    cli_cfg = {"version": 1}
existing_sl = cli_cfg.get("statusLine")
if not existing_sl or "statusline.py" in json.dumps(existing_sl):
    cli_cfg["statusLine"] = {
        "type": "command",
        "command": "python3 -S -B ~/.cursor/statusline.py",
        "padding": 1,
        "timeoutMs": 1500,
    }
    sl_msg = "statusLine"
else:
    sl_msg = "statusLine kept (user-defined)"
n_rules = 0
if os.environ.get("CURSOR_SAFE_ALLOW") == "1":
    # Opt-in (--cursor-safe-allow): strictly read-only commands with no argument-driven side effects.
    c_perms = cli_cfg.setdefault("permissions", {})
    c_allow = c_perms.setdefault("allow", [])
    for rule in (
        "Shell(ls)",
        "Shell(pwd)",
        "Shell(git status)",
        "Shell(git diff)",
        "Shell(git log)",
        "Shell(git show)",
        "Shell(wm-squeeze --safe)",
        "Shell(wm-codegraph)",
    ):
        if rule not in c_allow:
            c_allow.append(rule)
            n_rules += 1
cli_cfg_path.write_text(json.dumps(cli_cfg, indent=2) + "\n", encoding="utf-8")
print(f"Updated {cli_cfg_path} ({sl_msg}; +{n_rules} Shell allow rules)")
PY
fi

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
echo "Done! Installed hosts=${HOSTS}."
echo "To pull local Tricameral spores later: wm-models pull huginn | ravens | tricameral"
