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
