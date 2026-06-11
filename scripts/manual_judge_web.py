#!/usr/bin/env python3
"""Local web UI for human review of PhaseB VLM verdicts.

Thin browser front-end over ``scripts/manual_judge.py``: shows each ``(id, system)``
cell with its final screenshot, the task instruction, and the VLM verdict, and lets
you click SUCCESS / FAILURE / revert-to-VLM. Decisions persist to the same sidecar
``traj_logs/phaseB/<bench>/manual_overrides.json`` (re-runnable, auditable), and the
APPLY button folds them into ``results.jsonl`` + regenerates ``summary.{json,md}``
exactly like the CLI ``apply`` — so nothing here is a separate code path.

Run::

    uv run python scripts/manual_judge_web.py --bench mobileworld          # http://127.0.0.1:8765
    uv run python scripts/manual_judge_web.py --bench mobileworld --port 9000

stdlib only (http.server). Binds to 127.0.0.1 by default.
"""
from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import manual_judge as mj  # noqa: E402

PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PhaseB 人工复核 — {bench}</title>
<style>
:root{{--bg:#0f1115;--card:#1a1d24;--line:#2a2f3a;--fg:#e7e9ee;--mut:#9aa3b2;
--ok:#2ea043;--bad:#d1242f;--vlm:#6b7280;--accent:#4493f8;}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,Segoe UI,Roboto,system-ui,sans-serif}}
header{{position:sticky;top:0;z-index:10;background:#0f1115ee;backdrop-filter:blur(6px);
border-bottom:1px solid var(--line);padding:10px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}}
header h1{{font-size:15px;margin:0;font-weight:600}}
.pill{{background:var(--card);border:1px solid var(--line);border-radius:999px;padding:3px 10px;color:var(--mut)}}
.pill b{{color:var(--fg)}}
select,button{{font:inherit;color:var(--fg);background:var(--card);border:1px solid var(--line);
border-radius:7px;padding:5px 10px;cursor:pointer}}
button.apply{{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}}
button:disabled{{opacity:.5;cursor:not-allowed}}
#grid{{display:flex;flex-direction:column;gap:14px;padding:16px;max-width:1200px;margin:0 auto}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;display:flex;flex-direction:row}}
.card.s-success{{border-color:var(--ok)}} .card.s-failure{{border-color:var(--bad)}}
.shot{{background:#000;width:440px;flex:0 0 440px;aspect-ratio:9/16;object-fit:contain;cursor:zoom-in}}
.metrics{{display:flex;gap:8px;flex-wrap:wrap}}
.metric{{background:#11141a;border:1px solid var(--line);border-radius:7px;padding:4px 9px;font-size:12px;color:var(--mut)}}
.metric b{{color:var(--fg);font-variant-numeric:tabular-nums}}
.metric .u{{color:var(--mut);font-size:11px}}
.meta{{padding:12px 14px;display:flex;flex-direction:column;gap:8px;flex:1;min-width:0}}
.row1{{display:flex;justify-content:space-between;align-items:center;gap:8px}}
.tid{{font-weight:600;font-size:13px;word-break:break-all}}
.sys{{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut);border:1px solid var(--line);border-radius:5px;padding:1px 6px}}
.instr{{color:var(--mut);font-size:12px;max-height:3em;overflow:hidden}}
.instr.open{{max-height:none}}
.vlm{{font-size:12px;border-left:3px solid var(--vlm);padding-left:8px;color:#c7ccd6}}
.vlm .st{{font-weight:700}}
.eff{{font-size:12px}}
.badge{{display:inline-block;border-radius:5px;padding:1px 7px;font-weight:700;font-size:11px}}
.badge.success{{background:#0d3a1d;color:#3fb950}} .badge.failure{{background:#3d1418;color:#f85149}}
.badge.human{{outline:1px dashed var(--accent)}}
.btns{{display:flex;gap:6px;margin-top:2px}}
.btns button{{flex:1;padding:6px}}
.btns .b-success.on{{background:var(--ok);border-color:var(--ok);color:#fff}}
.btns .b-failure.on{{background:var(--bad);border-color:var(--bad);color:#fff}}
.btns .b-vlm.on{{background:var(--vlm);border-color:var(--vlm);color:#fff}}
#toast{{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:#000c;
border:1px solid var(--line);padding:8px 16px;border-radius:8px;opacity:0;transition:.2s;pointer-events:none}}
#toast.show{{opacity:1}}
dialog{{border:none;background:transparent;max-width:96vw;max-height:96vh;padding:0}}
dialog img{{max-width:96vw;max-height:96vh}}
dialog::backdrop{{background:#000d}}
</style></head><body>
<header>
  <h1>PhaseB 人工复核</h1>
  <span class="pill">bench <b>{bench}</b></span>
  <span class="pill" id="counts"></span>
  <select id="fsys"><option value="">全部系统</option><option value="mw">mw</option><option value="relay">relay</option></select>
  <select id="fst"><option value="">全部判定</option><option value="success">success</option><option value="failure">failure</option><option value="overridden">已改判</option></select>
  <button class="apply" id="apply">APPLY → results.jsonl</button>
  <span class="pill" id="status"></span>
</header>
<div id="grid"></div>
<div id="toast"></div>
<dialog id="zoom"><img id="zoomimg" src=""></dialog>
<script>
const BENCH="{bench}";
let cells=[];
const grid=document.getElementById('grid'),counts=document.getElementById('counts');
const toastEl=document.getElementById('toast');
function toast(m){{toastEl.textContent=m;toastEl.classList.add('show');clearTimeout(toast._t);toast._t=setTimeout(()=>toastEl.classList.remove('show'),1600);}}
function badge(st,by){{return `<span class="badge ${{st}} ${{by==='human'?'human':''}}">${{st.toUpperCase()}}${{by==='human'?' ✎':''}}</span>`;}}
function fmtT(v){{return (v==null||isNaN(v))?'—':Number(v).toFixed(1);}}
function fmtN(v){{return (v==null||isNaN(v))?'—':Number(v).toLocaleString('en-US');}}
function render(){{
  const fs=document.getElementById('fsys').value, ft=document.getElementById('fst').value;
  grid.innerHTML='';
  let nS=0,nF=0,nO=0;
  for(const c of cells){{
    if(c.effective==='success')nS++; if(c.effective==='failure')nF++; if(c.override)nO++;
    if(fs&&c.system!==fs)continue;
    if(ft==='overridden'&&!c.override)continue;
    if((ft==='success'||ft==='failure')&&c.effective!==ft)continue;
    const sel=c.override?c.override:'';
    const card=document.createElement('div');
    card.className='card s-'+c.effective;
    card.innerHTML=`
      <img class="shot" loading="lazy" src="/shot?bench=${{BENCH}}&id=${{encodeURIComponent(c.id)}}&system=${{c.system}}" onclick="zoom(this.src)">
      <div class="meta">
        <div class="row1"><span class="tid">${{c.id}}</span><span class="sys">${{c.system}}</span></div>
        <div class="instr" onclick="this.classList.toggle('open')">${{c.instruction||'(no instruction)'}}</div>
        <div class="vlm"><span class="st">VLM ${{c.vlm_status.toUpperCase()}}</span> — ${{c.vlm_reason||''}}</div>
        <div class="metrics">
          <span class="metric">时间 <b>${{fmtT(c.elapsed_s)}}</b><span class="u">s</span></span>
          <span class="metric">归一化 <b>${{fmtT(c.elapsed_s_norm)}}</b><span class="u">s</span></span>
          <span class="metric">token <b>${{fmtN(c.total_tokens)}}</b></span>
          <span class="metric">步数 <b>${{c.steps??'—'}}</b></span>
        </div>
        <div class="eff">当前生效: ${{badge(c.effective,c.by)}}</div>
        <div class="btns">
          <button class="b-success ${{sel==='success'?'on':''}}" onclick="setv('${{c.id}}','${{c.system}}','success')">SUCCESS</button>
          <button class="b-failure ${{sel==='failure'?'on':''}}" onclick="setv('${{c.id}}','${{c.system}}','failure')">FAILURE</button>
          <button class="b-vlm ${{sel===''?'on':''}}" onclick="setv('${{c.id}}','${{c.system}}','vlm')">还原VLM</button>
        </div>
      </div>`;
    grid.appendChild(card);
  }}
  counts.innerHTML=`生效 <b>${{nS}}</b>✓ / <b>${{nF}}</b>✗ · 已改判 <b>${{nO}}</b>`;
}}
async function load(){{cells=await (await fetch('/api/cells?bench='+BENCH)).json();render();}}
async function setv(id,system,status){{
  const r=await fetch('/api/override',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{bench:BENCH,id,system,status}})}});
  if(!r.ok){{toast('保存失败');return;}}
  const c=cells.find(x=>x.id===id&&x.system===system);
  c.override = status==='vlm'? null : status;
  c.effective = status==='vlm'? c.vlm_status : status;
  c.by = status==='vlm'? 'vlm':'human';
  render();
  toast(status==='vlm'?'已还原VLM':('改判为 '+status.toUpperCase()));
}}
function zoom(src){{document.getElementById('zoomimg').src=src;document.getElementById('zoom').showModal();}}
document.getElementById('zoom').onclick=function(){{this.close();}};
document.getElementById('fsys').onchange=render;
document.getElementById('fst').onchange=render;
document.getElementById('apply').onclick=async function(){{
  this.disabled=true;document.getElementById('status').textContent='applying...';
  const r=await fetch('/api/apply?bench='+BENCH,{{method:'POST'}});
  const j=await r.json();this.disabled=false;
  if(!r.ok){{document.getElementById('status').textContent='apply 失败';toast(j.error||'失败');return;}}
  document.getElementById('status').textContent='applied '+j.changed+' 行 · summary 已重算';
  toast('已写入 results.jsonl + summary');
  load();
}};
load();
</script></body></html>"""


def _load_norm(bench_dir: Path) -> dict[tuple[str, str], float]:
    """(id, system) -> normalized wall-clock, from results_normalized.jsonl if present."""
    p = bench_dir / "results_normalized.jsonl"
    if not p.exists():
        return {}
    out: dict[tuple[str, str], float] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        v = r.get("elapsed_s_norm")
        if isinstance(v, (int, float)):
            out[(r.get("id"), r.get("system"))] = float(v)
    return out


def _cells(bench: str):
    bench_dir = mj._bench_dir(bench)
    rows = mj._load_rows(bench_dir)
    id2dir = mj._id_to_task_dir(bench_dir)
    ov = mj._load_overrides(bench_dir)
    norm = _load_norm(bench_dir)
    out = []
    for r in sorted(rows, key=lambda x: (x["id"], x["system"])):
        vlm = r.get("verdict_vlm") or r.get("verdict") or {}
        eff = r.get("verdict") or {}
        human = (ov.get(r["id"], {}) or {}).get(r["system"])
        tdir = id2dir.get(r["id"])
        out.append({
            "id": r["id"], "system": r["system"],
            "instruction": mj._instruction(tdir),
            "vlm_status": vlm.get("status", "unknown"),
            "vlm_reason": vlm.get("reason", ""),
            "effective": eff.get("status", "unknown"),
            "by": eff.get("by", "vlm"),
            "override": human["status"] if human else None,
            "elapsed_s": r.get("elapsed_s"),
            "elapsed_s_norm": norm.get((r["id"], r["system"])),
            "total_tokens": r.get("total_tokens"),
            "steps": r.get("steps"),
        })
    return out


class Handler(BaseHTTPRequestHandler):
    bench = "mobileworld"

    def log_message(self, *a):  # quiet
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/":
                html = PAGE.format(bench=self.bench).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
            elif u.path == "/api/cells":
                self._json(_cells(q.get("bench", [self.bench])[0]))
            elif u.path == "/shot":
                bench = q.get("bench", [self.bench])[0]
                tid = q.get("id", [""])[0]
                system = q.get("system", [""])[0]
                tdir = mj._id_to_task_dir(mj._bench_dir(bench)).get(tid)
                p = (tdir / f"{system}_final.png") if tdir else None
                if not p or not p.exists():
                    self._json({"error": "no screenshot"}, 404)
                    return
                data = p.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        ln = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(ln) if ln else b""
        try:
            if u.path == "/api/override":
                body = json.loads(raw or b"{}")
                bench = body["bench"]
                bench_dir = mj._bench_dir(bench)
                status = body["status"]
                system = body["system"]
                tid = body["id"]
                if status not in mj.VALID_STATUS or system not in mj.SYSTEMS:
                    self._json({"error": "bad status/system"}, 400)
                    return
                ov = mj._load_overrides(bench_dir)
                cell = ov.setdefault(tid, {})
                if status == "vlm":
                    cell.pop(system, None)
                    if not cell:
                        ov.pop(tid, None)
                else:
                    entry = {"status": status, "by": "human"}
                    if body.get("reason"):
                        entry["reason"] = body["reason"]
                    cell[system] = entry
                mj._save_overrides(bench_dir, ov)
                self._json({"ok": True})
            elif u.path == "/api/apply":
                bench = q.get("bench", [self.bench])[0]
                changed = self._apply(bench)
                self._json({"ok": True, "changed": changed})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)}, 500)

    @staticmethod
    def _apply(bench: str) -> int:
        ns = argparse.Namespace(bench=bench, dry_run=False)
        # reuse the exact CLI apply (writes results.jsonl + summary.{json,md})
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            mj.cmd_apply(ns)
        # cmd_apply prints "applied N override row(s)"; recompute N cheaply
        bench_dir = mj._bench_dir(bench)
        rows = mj._load_rows(bench_dir)
        return sum(1 for r in rows if (r.get("verdict") or {}).get("by") == "human")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bench", default="mobileworld")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    mj._bench_dir(args.bench)  # fail fast if no results.jsonl
    Handler.bench = args.bench
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"PhaseB 人工复核 [{args.bench}]  →  {url}")
    print("Ctrl-C 退出。点 SUCCESS/FAILURE 即存入 manual_overrides.json；APPLY 折进 results.jsonl。")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
