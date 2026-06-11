"""Auto-activated probe shim for MobileWorld baseline runs.

Python imports ``sitecustomize`` at interpreter startup if it is found on the
path. RelayAgent's mw driver (``scripts/run_mobileworld.py``) prepends THIS
directory to ``PYTHONPATH`` for the ``mw test`` subprocess when per-call LLM
metrics are requested (``--llm-calls-out``), so this installs
``agents.mw_llm_probe`` before MobileWorld builds its agent — without modifying
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
        import agents.mw_llm_probe as _probe

        _probe.install()
    except Exception:
        pass
