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
    if err:
        import re
        from datetime import datetime, timezone
        from pathlib import Path

        ledger_path = Path(
            os.environ.get("WM_SIM_LEDGER", str(Path.home() / ".local" / "share" / "whitemagic" / "sim_ledger.jsonl"))
        )
        secret_re = re.compile(
            r"(?i)(bearer\s+[a-z0-9._\-]+|(?:api[_-]?key|token|secret|password|authorization)\s*[=:]\s*['\"]?[a-z0-9._\-/+]{8,}['\"]?)"
        )
        redact = lambda text: secret_re.sub("[REDACTED]", text or "")
        tool_call = payload.get("toolCall") or {}
        tool_name = tool_call.get("name", "unknown")
        args = tool_call.get("args") or {}
        if tool_name == "run_command":
            preview = redact(str(args.get("CommandLine") or ""))[:180]
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
            preview = redact(f"{args.get('ServerName', '')}/{args.get('ToolName', '')}")[:180]

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

    sys.stdout.write("{}")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
