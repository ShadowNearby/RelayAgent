"""Multi-app flow runner.

Reads a YAML flow under `manifests/_flows/`, executes its steps as a
sequence of (a) `mw test` sub-runs pinned to one app + one capability,
(b) user-input prompts, and (c) text-LLM extract steps that parse the
last sub-run's captured reply into structured data.

Design notes (see CLAUDE.md for project context):

- Each app step is a fresh `mw test` subprocess. We DON'T reuse one long-
  lived AppCardsAgent across apps because plan cursor / chat history are
  scoped to a single card.
- The capability router is bypassed via APPCARDS_FORCE_CAPABILITY +
  APPCARDS_INVOCATION_TEXT, so each sub-run skips the routing LLM call
  and goes straight into plan building.
- The captured in-app reply is shipped from the sub-process to the parent
  via APPCARDS_REPLY_OUT (a JSON file written at handoff/done).
- Extract steps run a small text-only chat completion against the same
  LLM endpoint configured in `.env` (LLM_BASE_URL / LLM_API_KEY / LLM_MODEL).
- Templating: `{var}` and `{var.field}` substitution against a flat
  blackboard dict that starts as `inputs` and grows as steps bind values.

Usage:
    scripts/run_flow.py manifests/_flows/xhs_to_amap_place.yaml \\
        --input category="独立书店" --input city=北京
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from loguru import logger
from openai import OpenAI

from agents._adb import cold_launch as _cold_launch

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"
AGENT_FILE = REPO_ROOT / "agents" / "appcards_agent.py"
MW_BIN = REPO_ROOT / ".venv" / "bin" / "mw"


# --------------------------------------------------------------------------- #
# small helpers. `cold_launch` is shared via agents/_adb.py; `.env` parsing
# is intentionally inlined here to keep flow_runner standalone-importable
# without depending on the `scripts/` directory being on sys.path.
# --------------------------------------------------------------------------- #


def _load_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip("'\"")
    return out


# cold-launch delegates to agents._adb so all three call sites (run_test.py,
# flow_runner, appcards_agent open_app) share one implementation.


# --------------------------------------------------------------------------- #
# templating
# --------------------------------------------------------------------------- #

_VAR_RE = re.compile(r"\{([a-zA-Z_][\w.]*)\}")


def render(template: str, ctx: dict[str, Any]) -> str:
    """Substitute `{var}` and `{var.field}` against ctx. Missing keys → ''."""
    def repl(m: re.Match) -> str:
        path = m.group(1).split(".")
        v: Any = ctx
        for p in path:
            if isinstance(v, dict):
                v = v.get(p, "")
            else:
                v = getattr(v, p, "")
        return "" if v is None else str(v)
    return _VAR_RE.sub(repl, template)


# --------------------------------------------------------------------------- #
# FlowRunner
# --------------------------------------------------------------------------- #


class FlowRunner:
    def __init__(
        self,
        flow_path: Path,
        env_overrides: dict[str, str] | None = None,
        input_overrides: dict[str, str] | None = None,
        nl_request: str | None = None,
        extra_mw_args: list[str] | None = None,
    ) -> None:
        self.flow_path = flow_path
        self.flow = yaml.safe_load(flow_path.read_text(encoding="utf-8"))
        if "steps" not in self.flow:
            raise ValueError(f"Flow {flow_path} has no `steps`")

        env_file = _load_dotenv(ENV_FILE)
        self.env = {**env_file, **(env_overrides or {})}
        for k in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
            v = os.environ.get(k) or self.env.get(k)
            if not v:
                raise RuntimeError(f"Missing required config: {k} (set in .env or env)")
            self.env[k] = v

        self.extra_mw_args = extra_mw_args or []
        self._llm = OpenAI(base_url=self.env["LLM_BASE_URL"], api_key=self.env["LLM_API_KEY"])

        # Each flow run gets its own traj root so the sub-runs don't keep
        # overwriting `traj_logs/user_task/`. MW's TrajLogger always writes
        # to `<log_file_root>/user_task/`, so we give each step its own
        # `log_file_root` and group them under one flow-scoped parent.
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.flow_traj_root = REPO_ROOT / "traj_logs" / f"{flow_path.stem}_{ts}"
        self._step_idx = 0
        logger.info(f"flow traj root: {self.flow_traj_root}")

        inputs_spec = self.flow.get("inputs") or {}
        nl_derived: dict[str, Any] = {}
        if nl_request:
            nl_derived = self._resolve_nl_inputs(nl_request, inputs_spec)
            logger.info(f"NL → inputs: {nl_derived}")

        self.bb: dict[str, Any] = {}
        for name, spec in inputs_spec.items():
            type_name = spec.get("type", "string")
            if input_overrides and name in input_overrides:
                self.bb[name] = _coerce(input_overrides[name], type_name)
            elif name in nl_derived:
                self.bb[name] = _coerce_value(nl_derived[name], type_name)
            elif "default" in spec:
                dflt = spec["default"]
                # Defaults can be templates that reference earlier inputs
                # (e.g. topic: "{city}{category}"); render against the
                # partially-built blackboard so later steps see the
                # composed string, not the literal template.
                if isinstance(dflt, str) and "{" in dflt:
                    dflt = render(dflt, self.bb)
                self.bb[name] = dflt
            else:
                raise ValueError(f"Flow input {name!r} has no default and was not supplied")
        logger.info(f"resolved inputs: {_redact(self.bb)}")

    # ------------------------------------------------------------- NL inputs

    def _resolve_nl_inputs(self, nl: str, inputs_spec: dict) -> dict[str, Any]:
        """Ask the text LLM to map a natural-language request to flow inputs.

        Only fields the LLM actually reads from the sentence are returned;
        everything else falls back to YAML defaults so we don't hallucinate
        values the user never mentioned.
        """
        if not inputs_spec:
            return {}
        schema_lines = []
        for name, spec in inputs_spec.items():
            t = spec.get("type", "string")
            desc = spec.get("description", "")
            dflt = spec.get("default", "")
            schema_lines.append(f"- {name} ({t}): {desc} [default: {dflt!r}]")
        schema = "\n".join(schema_lines)
        system = (
            "You map a user's natural-language request to a flow's input "
            "parameters. Return ONLY a JSON object inside a ```json``` fence. "
            "Include a key ONLY if the sentence clearly specifies it; omit "
            "keys the user did not mention (the caller will use defaults). "
            "Do not invent values."
        )
        user = (
            f"Flow inputs schema:\n{schema}\n\n"
            f"User request:\n{nl}\n\n"
            "Return the JSON object now."
        )
        resp = self._llm.chat.completions.create(
            model=self.env["LLM_MODEL"],
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            max_tokens=512,
        )
        out = (resp.choices[0].message.content or "").strip()
        logger.debug(f"NL-inputs raw reply: {out}")
        try:
            data = _parse_fenced_json(out)
        except Exception as e:
            logger.warning(f"NL-inputs parse failed ({e}); ignoring NL hint")
            return {}
        if not isinstance(data, dict):
            logger.warning(f"NL-inputs expected object, got {type(data).__name__}; ignoring")
            return {}
        return {k: v for k, v in data.items() if k in inputs_spec}

    # ------------------------------------------------------------------ run

    def run(self) -> dict[str, Any]:
        logger.info(f"FlowRunner start: {self.flow_path.name}  inputs={self.bb}")
        for step in self.flow["steps"]:
            kind = step.get("type") or "app_step"
            logger.info(f"--- step {step['id']!r} ({kind}) ---")
            if kind == "app_step":
                self._run_app_step(step)
            elif kind == "ask_user":
                self._run_ask_user(step)
            else:
                raise ValueError(f"Unknown step type: {kind}")
            logger.info(f"blackboard after {step['id']!r}: {_redact(self.bb)}")
        logger.info("FlowRunner done")
        return self.bb

    # ------------------------------------------------------------ app_step

    def _run_app_step(self, step: dict) -> None:
        app = step["app"]
        capability = step["capability"]
        prompt = render(step["prompt"], self.bb)

        _cold_launch(app)  # from agents._adb

        self._step_idx += 1
        step_log_root = self.flow_traj_root / f"{self._step_idx:02d}_{step['id']}"
        step_log_root.mkdir(parents=True, exist_ok=True)

        with tempfile.NamedTemporaryFile(
            mode="w+", suffix=".json", prefix="appcards_reply_", delete=False
        ) as fh:
            reply_path = Path(fh.name)
        try:
            # Priority: explicit overrides (the per-step APPCARDS_* keys
            # below) > shell env > .env file. Putting `self.env` (sourced
            # from .env) underneath `os.environ` lets a user override any
            # LLM_* / APPCARDS_* setting from their shell without editing
            # .env. The per-step keys at the end always win.
            child_env = {
                **self.env,
                **os.environ,
                "APPCARDS_TARGET_APP": app,
                "APPCARDS_SKIP_OPEN_APP": "1",
                "APPCARDS_FORCE_CAPABILITY": capability,
                "APPCARDS_INVOCATION_TEXT": prompt,
                "APPCARDS_REPLY_OUT": str(reply_path),
            }
            cmd = [
                str(MW_BIN), "test", prompt,
                "--agent-type", str(AGENT_FILE),
                "--model_name", self.env["LLM_MODEL"],
                "--llm_base_url", self.env["LLM_BASE_URL"],
                "--api_key", self.env["LLM_API_KEY"],
                "--log-file-root", str(step_log_root),
                *self.extra_mw_args,
            ]
            logger.info(
                f"→ mw test for app={app} capability={capability!r} prompt={prompt!r}"
            )
            # Feed empty stdin so the final ask_user handoff (when present)
            # closes cleanly with EOF rather than blocking the flow.
            rc = subprocess.call(cmd, cwd=REPO_ROOT, env=child_env, stdin=subprocess.DEVNULL)
            if rc != 0:
                logger.warning(f"mw test exited rc={rc}; continuing if reply was captured")

            reply = ""
            if reply_path.exists() and reply_path.stat().st_size > 0:
                payload = json.loads(reply_path.read_text(encoding="utf-8"))
                reply = (payload.get("reply") or "").strip()
            if not reply:
                raise RuntimeError(
                    f"Step {step['id']!r}: no reply captured at {reply_path}. "
                    f"Check the sub-run's {step_log_root}/user_task/."
                )
            logger.info(f"captured reply ({len(reply)} chars) from {app}")
        finally:
            try:
                reply_path.unlink()
            except OSError:
                pass

        if "bind" not in step:
            return
        if "extract" in step:
            value = self._extract(reply, step["extract"])
        else:
            value = reply
        self.bb[step["bind"]] = value

    # ---------------------------------------------------------- ask_user

    def _run_ask_user(self, step: dict) -> None:
        header = render(step.get("prompt_header", ""), self.bb)
        bind = step["bind"]

        if "select_from" in step:
            arr_key = step["select_from"]
            items = self.bb.get(arr_key) or []
            if not items:
                raise RuntimeError(f"ask_user {step['id']!r}: nothing in {arr_key!r} to choose from")
            label_tpl = step.get("item_label", "{name}")
            print(header)
            for i, it in enumerate(items, 1):
                print(f"  {i}. {render(label_tpl, it)}")
            print(f"  (1-{len(items)}, or empty to pick 1)", flush=True)
            try:
                raw = input("> ").strip()
            except EOFError:
                raw = ""
            chosen = _resolve_choice(raw, items, label_tpl)
            logger.info(f"user chose: {chosen}")
            self.bb[bind] = chosen
            return

        # plain freeform input
        print(header, flush=True)
        try:
            raw = input("> ").strip()
        except EOFError:
            raw = ""
        self.bb[bind] = raw

    # ----------------------------------------------------------- extract

    def _extract(self, raw_text: str, spec: dict) -> Any:
        prompt = render(spec["prompt"], self.bb)
        system = (
            "You extract structured data from text. "
            "Reply with ONE JSON value inside a ```json``` fence. "
            "No prose outside the fence."
        )
        user = f"{prompt}\n\n文本：\n{raw_text}"
        logger.info(f"extract LLM call ({len(user)} chars of text)")
        resp = self._llm.chat.completions.create(
            model=self.env["LLM_MODEL"],
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            max_tokens=1024,
        )
        out = (resp.choices[0].message.content or "").strip()
        logger.debug(f"extract raw reply: {out}")
        data = _parse_fenced_json(out)
        if "bind_to_array_key" in spec and isinstance(data, dict):
            data = data.get(spec["bind_to_array_key"], data)
        return data


# --------------------------------------------------------------------------- #
# small utilities
# --------------------------------------------------------------------------- #


_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


def _parse_fenced_json(text: str) -> Any:
    m = _FENCE_RE.search(text)
    payload = m.group(1) if m else text
    return json.loads(payload)


def _coerce(value: str, type_name: str) -> Any:
    if type_name == "int":
        return int(value)
    if type_name == "float":
        return float(value)
    if type_name == "bool":
        return value.lower() in ("1", "true", "yes", "y")
    return value


def _coerce_value(value: Any, type_name: str) -> Any:
    """Coerce a possibly-non-string value (LLM may already return int/float/bool)."""
    if isinstance(value, str):
        return _coerce(value, type_name)
    if type_name == "int":
        return int(value)
    if type_name == "float":
        return float(value)
    if type_name == "bool":
        return bool(value)
    return str(value)


def _redact(d: dict[str, Any]) -> dict[str, Any]:
    """Shallow redact obvious secrets in blackboard logging."""
    out = {}
    for k, v in d.items():
        if "key" in k.lower() or "token" in k.lower():
            out[k] = "***"
        else:
            out[k] = v
    return out


def _resolve_choice(raw: str, items: list[Any], label_tpl: str) -> Any:
    if not raw:
        return items[0]
    if raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(items):
            return items[idx]
    # substring match against rendered label, then `name`
    lowered = raw.lower()
    for it in items:
        if lowered in render(label_tpl, it).lower():
            return it
    for it in items:
        if isinstance(it, dict) and lowered in str(it.get("name", "")).lower():
            return it
    raise ValueError(f"Could not resolve user choice {raw!r} among {len(items)} items")


def _parse_kv(kvs: list[str]) -> dict[str, str]:
    out = {}
    for s in kvs or []:
        if "=" not in s:
            raise SystemExit(f"--input expects KEY=VALUE, got {s!r}")
        k, _, v = s.partition("=")
        out[k.strip()] = v.strip()
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("flow", help="Path to a flow YAML (e.g. manifests/_flows/xhs_to_amap_place.yaml)")
    p.add_argument("--input", action="append", default=[], metavar="KEY=VALUE",
                   help="Override a flow input; repeatable")
    p.add_argument("--nl", default=None, metavar="TEXT",
                   help="Natural-language request; LLM extracts flow inputs from it. "
                        "Explicit --input values still take precedence.")
    args, extra = p.parse_known_args(argv)

    runner = FlowRunner(
        flow_path=Path(args.flow).resolve(),
        input_overrides=_parse_kv(args.input),
        nl_request=args.nl,
        extra_mw_args=extra,
    )
    runner.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
