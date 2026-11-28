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

    if payload.get("is_interrupt") is True:
        sys.stdout.write("{}")
        sys.stdout.flush()
        return

    err = payload.get("error") or payload.get("error_message")
    tool_call = payload.get("toolCall") or {}
    tool_name = str(tool_call.get("name") or payload.get("tool_name") or "unknown")
    args = tool_call.get("args") or tool_call.get("arguments") or payload.get("tool_input") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            args = {}
    if not isinstance(args, dict):
        args = {}
    raw_out = (
        payload.get("result")
        or payload.get("output")
        or payload.get("toolOutput")
        or payload.get("tool_output")
    )
    out_str = raw_out if isinstance(raw_out, str) else (json.dumps(raw_out) if raw_out else "")
    write_content = (
        str(args.get("Content") or args.get("content") or args.get("contents") or "")
        if tool_name in ("write_file", "Write")
        else ""
    )
    edit_content = (
        str(args.get("Replacement") or args.get("replacement") or args.get("new_string") or "")
        if tool_name in ("edit_file", "StrReplace")
        else ""
    )

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
            if tool_name in ("run_command", "Shell"):
                preview = redact(str(args.get("CommandLine") or args.get("command") or ""))[:180]
            elif tool_name in ("read_file", "Read"):
                p = str(args.get("Path") or args.get("path") or args.get("file_path") or "")
                s_l = args.get("StartLine") if args.get("StartLine") is not None else args.get("offset")
                e_l = args.get("EndLine") if args.get("EndLine") is not None else args.get("limit")
                rng = (
                    f" [L{s_l if s_l is not None else 1}-{e_l if e_l is not None else 'EOF'}]"
                    if (s_l is not None or e_l is not None)
                    else ""
                )
                preview = redact(f"{tool_name}: {p}{rng}" if p else tool_name)[:180]
            elif tool_name in ("write_file", "edit_file", "Write", "StrReplace"):
                p = str(args.get("Path") or args.get("path") or args.get("file_path") or "")
                preview = redact(f"{tool_name}: {p}" if p else tool_name)[:180]
            elif tool_name.endswith("_apply_edits") or tool_name == "MCP:apply_edits":
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
            elif tool_name.endswith("_run_checks") or tool_name == "MCP:run_checks":
                cmds = args.get("commands")
                first = cmds[0] if isinstance(cmds, list) and cmds else ""
                if isinstance(first, dict):
                    first = first.get("command", "")
                preview = redact(f"run_checks: {first}")[:180]
            elif tool_name.endswith("_batch_search") or tool_name == "MCP:batch_search":
                qs = args.get("queries")
                qtypes = [str(q.get("type", "?")) for q in qs[:4] if isinstance(q, dict)] if isinstance(qs, list) else []
                preview = redact(f"batch_search: {','.join(qtypes)}")[:180]
            elif tool_name.endswith("_code_graph") or tool_name == "MCP:code_graph":
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
                "conversation_id": str(payload.get("conversationId") or payload.get("conversation_id") or "")[:16],
                "cwd": str(payload.get("cwd", "")),
                "model": str(payload.get("modelName") or payload.get("model") or ""),
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
            src_txt = str(
                args.get("Path")
                or args.get("path")
                or args.get("file_path")
                or args.get("CommandLine")
                or args.get("command")
                or out_str[:120]
            )
            entry = {
                "event": "tool_spill",
                "kind": spill_kind,
                "ts": datetime.now(timezone.utc).isoformat(),
                "conv_id": str(payload.get("conversationId") or payload.get("conversation_id") or "")[:16],
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
