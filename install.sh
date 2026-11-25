#!/usr/bin/env bash
# WhiteMagic Turbo Kit v4.6.0 — Idempotent Multi-Host Cross-Platform Installer (Antigravity, Codex, Cursor)
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
HOSTS="${WM_HOSTS:-agy}"
PULL_PROFILE=""

while [ "$#" -gt 0 ]; do
    case "$1" in
        --skip-agy-fast) SKIP_AGY_FAST=1; shift ;;
        --skip-tricameral-engine) SKIP_TRICAMERAL_ENGINE=1; shift ;;
        --keep-heavy-plugins) KEEP_HEAVY_PLUGINS=1; shift ;;
        --sync-global) SYNC_GLOBAL=1; shift ;;
        --prune-codex-office) PRUNE_CODEX_OFFICE=1; shift ;;
        --hosts)
            HOSTS="${2:?--hosts requires a comma-separated list (e.g. agy,codex,cursor)}"
            shift 2
            ;;
        --hosts=*)
            HOSTS="${1#--hosts=}"
            shift
            ;;
        --with-huginn) PULL_PROFILE="huginn"; shift ;;
        --with-ravens) PULL_PROFILE="ravens"; shift ;;
        --with-tricameral) PULL_PROFILE="tricameral"; shift ;;
        -h|--help)
            echo "Usage: ./install.sh [--hosts agy,codex,cursor|all] [--prune-codex-office] [--skip-agy-fast] [--skip-tricameral-engine] [--keep-heavy-plugins] [--sync-global] [--with-huginn|--with-ravens|--with-tricameral]"
            exit 0
            ;;
        *) shift ;;
    esac
done

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
if [ "${WM_SKIP_CARGO:-0}" -eq 1 ]; then
    echo "Skipping Cargo build (WM_SKIP_CARGO=1)"
elif command -v cargo >/dev/null 2>&1; then
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
    echo "=== 2. Installing unified native plugin: whitemagic-agy-kit v4.6.0 ==="
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
fi # end HAS_AGY plugin install

echo "=== 5. Installing portable CLI tools & model manifest ==="
for tool in wm-sim agy-bench agy-mode agy-token-audit wm-local-digest wm-dream wm-handoff wm-reset wm-cpr-bridge wm-models wm-huginn wm-muninn wm-tricameral wm-tricameral-mcp wm-grok-swarm wm-grok-fleet wm-grok-mcp wm-vps wm-triage; do
    if [ -f "${KIT_DIR}/bin/${tool}" ]; then
        install_bin "${KIT_DIR}/bin/${tool}" "${LOCAL_BIN}/${tool}"
    fi
done
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
    echo "=== 7c. Configuring Cursor adapter (~/.cursor/hooks.json & ~/.cursor/mcp.json) ==="
    mkdir -p "${HOME}/.cursor"
    FAST_MJS="${FAST_MCP_DIR}/mcp-server.mjs" NODE_CMD="${NODE_BIN:-node}" python3 - <<'PY'
import json, os, shutil
from datetime import datetime
from pathlib import Path

home = Path.home()
cursor_dir = home / ".cursor"
ts = datetime.now().strftime("%Y%m%d_%H%M%S")

# 1. Merge ~/.cursor/hooks.json (version: 1)
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
    return [it for it in (items or []) if not (isinstance(it, dict) and "wm-squeeze" in str(it.get("command", "")))]

for ev, extra in (
    ("beforeShellExecution", {}),
    ("beforeMCPExecution", {"matcher": "wm_fast|whitemagic"}),
    ("preToolUse", {}),
):
    lst = _strip_wm(hooks.get(ev))
    entry = dict(extra)
    entry["command"] = (
        f'if command -v wm-squeeze >/dev/null 2>&1; then exec wm-squeeze --pre-tool --host cursor --event {ev}; '
        f'elif [ -x "$HOME/.local/bin/wm-squeeze" ]; then exec "$HOME/.local/bin/wm-squeeze" --pre-tool --host cursor --event {ev}; fi'
    )
    lst.append(entry)
    hooks[ev] = lst

hooks_path.write_text(json.dumps(hdata, indent=2) + "\n", encoding="utf-8")
print(f"Updated {hooks_path} (beforeShellExecution, beforeMCPExecution, preToolUse)")

# 2. Merge wm_fast into ~/.cursor/mcp.json without disturbing whitemagic
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
node_cmd = os.environ.get("NODE_CMD") or "node"
fast_mjs = os.environ.get("FAST_MJS") or ""
if fast_mjs and Path(fast_mjs).is_file():
    servers["wm_fast"] = {"command": node_cmd, "args": [fast_mjs]}
    mcp_path.write_text(json.dumps(mdata, indent=2) + "\n", encoding="utf-8")
    print(f"Updated {mcp_path} servers={list(servers.keys())}")
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
