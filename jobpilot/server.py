"""Local dashboard on http://127.0.0.1:8765 — only reachable from your own computer."""
from __future__ import annotations

import json
import mimetypes
import threading
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import pipeline
from .core import connect, resolve
from .llm import LLM
from .tailor import tailor

WEB = Path(__file__).parent / "web"
STATUSES = {"new", "saved", "applied", "interview", "offer", "rejected", "hidden", "filtered",
            "review", "queued", "manual", "skipped"}


class State:
    log: list[str] = []
    busy: bool = False


def make_handler(cfg):
    app_dir = resolve(cfg, cfg["app"]["applications_dir"])

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else (
                json.dumps(body, ensure_ascii=False).encode() if ctype == "application/json" else body.encode())
            self.send_response(code)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if "text" in ctype or "json" in ctype else ""))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            try:
                if u.path == "/":
                    return self._send(200, (WEB / "index.html").read_bytes(), "text/html")
                if u.path == "/api/jobs":
                    con = connect(cfg)
                    show_all = q.get("all", ["0"])[0] == "1"
                    rows = con.execute(
                        "SELECT id, source, title, company, location, url, posted_at, first_seen, types, where_ok, "
                        "flags, kw_score, ai_score, ai_json, status, app_dir, salary, apply_note, applied_at, substr(description,1,400) AS snippet "
                        "FROM jobs " + ("" if show_all else "WHERE status != 'filtered' ") +
                        "ORDER BY COALESCE(ai_score, kw_score) DESC, first_seen DESC LIMIT 3000").fetchall()
                    con.close()
                    return self._send(200, [dict(r) for r in rows])
                if u.path.startswith("/api/job/"):
                    con = connect(cfg)
                    d = pipeline.job_row(con, urllib.parse.unquote(u.path[9:]))
                    con.close()
                    if d.get("app_dir"):
                        p = Path(d["app_dir"])
                        d["files"] = [f"/files/{p.name}/{f.name}" for f in sorted(p.iterdir())] if p.exists() else []
                    return self._send(200, d)
                if u.path == "/api/questions":
                    con = connect(cfg)
                    rows = con.execute("SELECT key, question, options, answer FROM answers ORDER BY answer IS NOT NULL, question").fetchall()
                    con.close()
                    return self._send(200, [dict(r) for r in rows])
                if u.path == "/api/progress":
                    return self._send(200, {"busy": State.busy, "log": State.log[-300:],
                                            "dry_run": bool(cfg.get("apply", {}).get("dry_run", True))})
                if u.path == "/api/sources":
                    con = connect(cfg)
                    rows = con.execute(
                        "SELECT source, fetched, new, error, started FROM runs WHERE id IN "
                        "(SELECT MAX(id) FROM runs GROUP BY source) ORDER BY source").fetchall()
                    counts = con.execute("SELECT source, COUNT(*) n, SUM(status!='filtered') kept FROM jobs GROUP BY source").fetchall()
                    con.close()
                    return self._send(200, {"runs": [dict(r) for r in rows], "counts": [dict(r) for r in counts]})
                if u.path.startswith("/files/"):
                    p = (app_dir / urllib.parse.unquote(u.path[7:])).resolve()
                    if app_dir.resolve() not in p.parents or not p.is_file():
                        return self._send(404, {"error": "not found"})
                    ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
                    if p.suffix in (".md", ".txt"):
                        ctype = "text/plain"
                    return self._send(200, p.read_bytes(), ctype)
                self._send(404, {"error": "not found"})
            except Exception as e:
                self._send(500, {"error": str(e)})

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            try:
                b = self._body()
                if u.path == "/api/status":
                    if b.get("status") not in STATUSES:
                        return self._send(400, {"error": "bad status"})
                    con = connect(cfg)
                    con.execute("UPDATE jobs SET status=? WHERE id=?", (b["status"], b["id"]))
                    con.commit()
                    con.close()
                    return self._send(200, {"ok": True})
                if u.path == "/api/apply":      # {"id": ...} approve one job, or {} to run everything queued
                    con = connect(cfg)
                    if b.get("id"):
                        con.execute("UPDATE jobs SET status='queued', apply_note=NULL WHERE id=?", (b["id"],))
                        con.commit()
                    con.close()
                    _kick_apply(cfg)
                    return self._send(200, {"ok": True})
                if u.path == "/api/answer":
                    con = connect(cfg)
                    con.execute("UPDATE answers SET answer=? WHERE key=?", (b.get("answer", "").strip(), b["key"]))
                    con.commit()
                    con.close()
                    return self._send(200, {"ok": True})
                if u.path == "/api/refresh":
                    if State.busy:
                        return self._send(409, {"error": "already running"})
                    threading.Thread(target=_bg, args=(cfg,), daemon=True).start()
                    return self._send(200, {"ok": True})
                if u.path == "/api/tailor":
                    con = connect(cfg)
                    job = pipeline.ensure_description(cfg, con, pipeline.job_row(con, b["id"]))
                    folder = tailor(cfg, job, LLM(cfg))
                    con.execute("UPDATE jobs SET app_dir=?, status=CASE WHEN status IN ('new','filtered') "
                                "THEN 'saved' ELSE status END WHERE id=?", (str(folder), b["id"]))
                    con.commit()
                    con.close()
                    return self._send(200, {"ok": True, "files": [f"/files/{folder.name}/{f.name}"
                                                                  for f in sorted(folder.iterdir())]})
                if u.path == "/api/add":
                    con = connect(cfg)
                    jid = pipeline.add_url(cfg, con, b.get("url", "").strip(), b.get("text", ""),
                                           b.get("title", ""), b.get("company", ""), log=lambda *_: None)
                    con.close()
                    return self._send(200, {"ok": True, "id": jid})
                self._send(404, {"error": "not found"})
            except Exception as e:
                traceback.print_exc()
                self._send(500, {"error": str(e)})

    return H


def _bg(cfg):
    State.busy, State.log = True, []
    try:
        pipeline.run(cfg, log=State.log.append)
        State.log.append("Done ✓")
    except Exception as e:
        State.log.append(f"Error: {e}")
    finally:
        State.busy = False


_apply_lock = threading.Lock()


def _kick_apply(cfg):
    """Apply to queued jobs in the background; jobs approved while it runs are picked up in the same pass."""
    def work():
        if not _apply_lock.acquire(blocking=False):
            return
        try:
            from .apply import apply_queued
            State.busy = True
            while True:
                con = connect(cfg)
                st = apply_queued(cfg, con, log=State.log.append)
                con.close()
                if not (st["applied"] or st["manual"]):
                    break
        except Exception as e:
            State.log.append(f"Error: {e}")
        finally:
            State.busy = False
            _apply_lock.release()
    threading.Thread(target=work, daemon=True).start()


def serve(cfg, open_browser=True):
    port = int(cfg["app"].get("port", 8765))
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(cfg))
    url = f"http://127.0.0.1:{port}"
    print(f"JobPilot dashboard: {url}   (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
