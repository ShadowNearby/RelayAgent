---
name: Bug report
about: A runtime, adapter, or Android-app defect (for card problems use "Card issue")
labels: bug
---

**What happened**

**What you expected**

**Repro**

```bash
# exact command, e.g.
uv run python -m agents.runtime.native_runner <pkg> "<goal>"
```

**Environment**
- Device / emulator + Android version:
- App + version (if a specific app is involved):
- LLM endpoint/model (`LLM_MODEL`):
- Commit: `git rev-parse --short HEAD`

**Logs / trajectory**

Attach the relevant leg dir under `traj_logs/` (at least `traj.json` and the
last few `steps/step_*.png`) if you can — strip anything personal first.
