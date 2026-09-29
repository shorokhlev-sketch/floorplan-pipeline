#!/usr/bin/env python3
"""Tiny rebuild API for the plan editor + static file server.

POST /api/rebuild  {"unit": "202", "overrides": {"prims": {...}, "signatures": {...}}}
  -> merges the overrides into plan-studio/cad-overrides.json, re-traces the unit
     (walls svg, objects json, raw json) and returns {"ok": true, "unit": ..., "counts": ...}
GET  /api/overrides -> the merged rules file
PUT  /api/v3/plan/<unit>  body = plan v3 document (editor/SCHEMA.md)
  -> validates, backs up the previous file to plan-studio/v3/plans/history/, writes plans/unit-<unit>.json,
     refreshes plans/index.json; returns {"ok": true, "saved": <iso time>} or {"ok": false, "problems": [...]}
GET  anything else  -> static files from ROOT (so it can replace `python -m http.server` locally)

Env: TRACE_ROOT (workspace, default fpconfig.WORK), TRACE_PDF (architect PDF, default fpconfig.PDF),
     TRACE_NO_PNG=1, PLAN_API_PORT. The editor (editor/app) is served from the repository root.
"""
import json, os, sys, threading, importlib.util
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fpconfig

ROOT = Path(os.environ.get("TRACE_ROOT") or fpconfig.WORK)
os.environ.setdefault("TRACE_ROOT", str(ROOT))
spec = importlib.util.spec_from_file_location("tp", str(Path(__file__).resolve().parent / "trace-plans.py"))
tp = importlib.util.module_from_spec(spec); spec.loader.exec_module(tp)
import pymupdf
DOC = pymupdf.open(str(tp.PDF))
LOCK = threading.Lock()
UNITS = set(tp.TARGET_UNITS)

# v3 documents (plan-studio/v3/plans): validate() and write_index() live in tools/plan-import.py
_pi_spec = importlib.util.spec_from_file_location("plan_import", str(Path(__file__).resolve().parent / "plan-import.py"))
pi = importlib.util.module_from_spec(_pi_spec); _pi_spec.loader.exec_module(pi)
V3_PLANS = ROOT / "plan-studio/v3/plans"
V3_UNIT_RE = __import__("re").compile(r"^/api/v3/plan/(\d{3,4})$")


class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")   # редактор правится часто: никакого кэша статики
        super().end_headers()

    def guess_type(self, path):
        if str(path).endswith((".js", ".mjs")):
            return "text/javascript; charset=utf-8"
        return super().guess_type(path)

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0].replace("/plan-studio", "", 1) == "/api/overrides":
            tp.reload_overrides(); return self._json(200, tp.OVERRIDES)
        return super().do_GET()

    def do_PUT(self):
        path = self.path.split("?")[0].replace("/plan-studio", "", 1)
        m = V3_UNIT_RE.match(path)
        if not m:
            return self._json(404, {"ok": False, "error": "unknown endpoint"})
        unit = m.group(1)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 8 * 1024 * 1024:
                return self._json(413, {"ok": False, "error": "too large"})
            doc = json.loads(self.rfile.read(n) or b"{}")
            if doc.get("v") != 3 or str(doc.get("unit")) != unit:
                return self._json(400, {"ok": False, "error": "not a v3 document for this unit"})
            problems = pi.validate(doc)
            if problems:
                return self._json(422, {"ok": False, "problems": problems[:50]})
            import datetime
            ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            doc["saved"] = datetime.datetime.now().isoformat(timespec="seconds")
            with LOCK:
                V3_PLANS.mkdir(parents=True, exist_ok=True)
                target = V3_PLANS / f"unit-{unit}.json"
                if target.exists():
                    hist = V3_PLANS / "history"; hist.mkdir(exist_ok=True)
                    (hist / f"unit-{unit}-{ts}.json").write_bytes(target.read_bytes())
                    old = sorted(hist.glob(f"unit-{unit}-*.json"))
                    for f in old[:-40]:   # keep the last 40 versions per unit
                        f.unlink()
                tmp = target.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1))
                tmp.replace(target)
                pi.OUT_DIR = V3_PLANS
                pi.write_index()
            return self._json(200, {"ok": True, "unit": unit, "saved": doc["saved"]})
        except Exception as e:  # noqa
            import traceback; traceback.print_exc()
            return self._json(500, {"ok": False, "error": str(e)[:300]})

    def do_POST(self):
        if self.path.split("?")[0].replace("/plan-studio", "", 1) != "/api/rebuild":
            return self._json(404, {"ok": False, "error": "unknown endpoint"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
            unit = str(payload.get("unit", ""))
            if unit not in UNITS:
                return self._json(400, {"ok": False, "error": "unknown unit"})
            overrides = payload.get("overrides") or {}
            with LOCK:
                merged = tp.merge_overrides(overrides)
                counts, ncols, nlabels, uncovered, gaps, _ = tp.process_unit(unit, DOC)
            return self._json(200, {"ok": True, "unit": unit, "counts": counts, "columns": ncols,
                                    "labels": nlabels, "rules": len(merged["prims"]) + len(merged["signatures"])})
        except Exception as e:  # noqa
            import traceback; traceback.print_exc()
            return self._json(500, {"ok": False, "error": str(e)[:300]})


if __name__ == "__main__":
    port = int(os.environ.get("PLAN_API_PORT") or 8770)
    print(f"plan-api on http://127.0.0.1:{port}  root={ROOT} pdf={tp.PDF} no_png={tp.NO_PNG}")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
