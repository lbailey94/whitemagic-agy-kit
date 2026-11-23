---
name: estate-comms-triage
description: One-shot parallel estate & comms sweep (`wm-triage`) combining cached preflight, live GitHub notifications, newest 5 Sangha board posts, and WhiteMagic Gen3 continuity in <5s (<= 4.5 KB).
---

# Estate & Comms Triage (`wm-triage`)

Use `wm-triage` for morning/evening status sweeps instead of 10–25 serial `run_command` / `read_file` calls.

```bash
wm-triage            # 4 parallel lanes (<5s, <=4.5 KB)
wm-triage --dry-run  # local cache + Sangha board only (zero network)
```

Follow up with targeted `wm-vps health` or `gh pr view <num>` only for items that need action.
