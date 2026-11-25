#!/usr/bin/env python3
"""
Portable (macOS + Linux) AGY PostToolUse SIM negative-knowledge capture hook.
Lazy-imports on error only for ~20ms fast-path on successful commands.
"""

import json
import os
import sys


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        sys.stdout.write("{}")
        return

    err = payload.get("error")
    tool_call = payload.get("toolCall") or {}
    tool_name = str(tool_call.get("name") or "unknown")
    args = tool_call.get("args") or tool_call.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            args = {}
    if not isinstance(args, dict):
        args = {}
    raw_out = payload.get("result") or payload.get("output") or payload.get("toolOutput")
    out_str = raw_out if isinstance(raw_out, str) else (json.dumps(raw_out) if raw_out else "")
    write_content = str(args.get("Content") or args.get("content") or "") if tool_name == "write_file" else ""
    edit_content = str(args.get("Replacement") or args.get("replacement") or "") if tool_name == "edit_file" else ""

    spill_bytes = 0
    spill_kind = ""
    if not err:
        if len(out_str) > 16000:
            spill_bytes = len(out_str)
            spill_kind = "output_spill"
        elif len(write_content) > 25000:
            spill_bytes = len(write_content)
            spill_kind = "large_write_file"
        elif len(edit_content) > 25000:
            spill_bytes = len(edit_content)
            spill_kind = "large_edit_file"

    if err or spill_bytes > 0:
        import re
        from datetime import datetime, timezone
        from pathlib import Path

        secret_re = re.compile(
            r"(?i)(bearer\s+[a-z0-9._\-]+|(?:api[_-]?key|token|secret|password|authorization)\s*[=:]\s*['\"]?[a-z0-9._\-/+]{8,}['\"]?)"
        )
        redact = lambda text: secret_re.sub("[REDACTED]", text or "")

        if err:
            ledger_path = Path(
                os.environ.get("WM_SIM_LEDGER", str(Path.home() / ".local" / "share" / "whitemagic" / "sim_ledger.jsonl"))
            )
            if tool_name == "run_command":
                preview = redact(str(args.get("CommandLine") or args.get("command") or ""))[:180]
            elif tool_name == "read_file":
                p = str(args.get("Path") or args.get("path") or "")
                s_l, e_l = args.get("StartLine"), args.get("EndLine")
                rng = (
                    f" [L{s_l if s_l is not None else 1}-{e_l if e_l is not None else 'EOF'}]"
                    if (s_l is not None or e_l is not None)
                    else ""
                )
                preview = redact(f"read_file: {p}{rng}" if p else "read_file")[:180]
            elif tool_name in ("write_file", "edit_file"):
                p = str(args.get("Path") or args.get("path") or "")
                preview = redact(f"{tool_name}: {p}" if p else tool_name)[:180]
            elif tool_name.endswith("_apply_edits"):
                edits = args.get("edits")
                if isinstance(edits, str):
                    try:
                        edits = json.loads(edits)
                    except Exception:
                        edits = []
                if isinstance(edits, list) and edits:
                    paths = [str(e.get("path", "?")) for e in edits[:4] if isinstance(e, dict)]
                    preview = redact(f"apply_edits: {', '.join(paths)}")[:180]
                else:
                    preview = "apply_edits"
            elif tool_name.endswith("_run_checks"):
                cmds = args.get("commands")
                first = cmds[0] if isinstance(cmds, list) and cmds else ""
                if isinstance(first, dict):
                    first = first.get("command", "")
                preview = redact(f"run_checks: {first}")[:180]
            elif tool_name.endswith("_batch_search"):
                qs = args.get("queries")
                qtypes = [str(q.get("type", "?")) for q in qs[:4] if isinstance(q, dict)] if isinstance(qs, list) else []
                preview = redact(f"batch_search: {','.join(qtypes)}")[:180]
            elif tool_name.endswith("_code_graph"):
                preview = redact(f"code_graph: {args.get('mode', '')}:{args.get('target', '')}")[:180]
            else:
                srv = str(args.get("ServerName") or "")
                t_sub = str(args.get("ToolName") or "")
                if srv or t_sub:
                    preview = redact(f"{srv}/{t_sub}")[:180]
                else:
                    preview = redact(tool_name)[:180]

            entry = {
                "event": "tool_failure",
                "ts": datetime.now(timezone.utc).isoformat(),
                "conversation_id": str(payload.get("conversationId", ""))[:16],
                "cwd": str(payload.get("cwd", "")),
                "model": str(payload.get("modelName", "")),
                "tool": tool_name,
                "cmd_preview": preview,
                "error": redact(str(err))[:240],
            }
            try:
                ledger_path.parent.mkdir(parents=True, exist_ok=True)
                with open(ledger_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except Exception:
                pass
        else:
            telem_path = Path(
                os.environ.get("WM_AGY_TELEMETRY", str(Path.home() / ".local" / "share" / "whitemagic" / "agy_telemetry.jsonl"))
            )
            src_txt = str(args.get("Path") or args.get("CommandLine") or out_str[:120])
            entry = {
                "event": "tool_spill",
                "kind": spill_kind,
                "ts": datetime.now(timezone.utc).isoformat(),
                "conv_id": str(payload.get("conversationId", ""))[:16],
                "tool": tool_name,
                "bytes": spill_bytes,
                "preview": redact(src_txt)[:180],
            }
            try:
                telem_path.parent.mkdir(parents=True, exist_ok=True)
                with open(telem_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except Exception:
                pass

    sys.stdout.write("{}")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
