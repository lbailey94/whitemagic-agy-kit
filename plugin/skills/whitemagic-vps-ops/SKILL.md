---
name: whitemagic-vps-ops
description: Strictly read-only bounded VPS diagnostics (`wm-vps health|services|logs|disk|all`) over multiplexed SSH ControlMaster, capped <= 4 KB to eliminate heredoc output.txt spills.
---

# WhiteMagic VPS Read-Only Ops (`wm-vps`)

Use `wm-vps` instead of ad-hoc `ssh whitemagic 'python3 - <<PY ...'` heredocs (which caused 9/10 historical `output.txt` spills).

## Usage
```bash
wm-vps health      # hostname, uptime, loadavg, MemTotal/MemAvailable
wm-vps services    # top 40 running systemd units
wm-vps logs        # last 25 journalctl lines (<= 3.5 KB)
wm-vps disk        # df -P -h / /var /home
wm-vps all         # combined bounded snapshot in 1 multiplexed SSH hop
```

- Runs over `~/.ssh/config` `ControlMaster auto` (`~15ms` warm hop, `BatchMode=yes`, `10s` timeout).
- Strictly read-only: never modifies VPS files or restarts services.
