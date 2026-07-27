"""Auto-activated probe shim for MobileWorld baseline runs.

Python imports ``sitecustomize`` at interpreter startup if it is found on the
path. RelayAgent's mw driver (``scripts/run_mobileworld.py``) prepends THIS
directory to ``PYTHONPATH`` for the ``mw test`` subprocess when per-call LLM
metrics are requested (``--llm-calls-out``), so this installs
``agents.llm.mw_llm_probe`` before MobileWorld builds its agent — without modifying
MobileWorld's installed source.

Gated on ``RELAY_MW_LLM_CALLS_OUT`` so it stays inert for any other process that
happens to have this directory on its path. The repo root is *appended* (not
prepended) to ``sys.path`` so it can never shadow MobileWorld's own modules.
"""
import os

if os.getenv("RELAY_MW_LLM_CALLS_OUT"):
    import sys
    from pathlib import Path

    _repo_root = str(Path(__file__).resolve().parents[2])
    if _repo_root not in sys.path:
        sys.path.append(_repo_root)
    try:
        import agents.llm.mw_llm_probe as _probe

        _probe.install()
    except Exception as e:
        # Never break the mw run, but never fail silently either (repo rule:
        # primary→fallback misses are info/warning-visible). Without the probe
        # llm_calls.json is never written, so the benchmark's mw rows get
        # llm_time_actual_s=0 and elapsed_s_norm silently degrades to the raw
        # queue-tainted wall-clock. This lands in the run's stderr.log.
        print(f"[relay mw probe] install failed ({type(e).__name__}: {e}) — "
              f"llm_calls.json will be missing and mw wall-clock normalization "
              f"degrades to raw elapsed", file=sys.stderr)
