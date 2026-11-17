"""Local dashboard on http://127.0.0.1:8765 — only reachable from your own computer."""
from __future__ import annotations

import json
import mimetypes
import threading
import traceback
import re
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


from .apply import FORCE


def make_handler(cfg):
    app_dir = resolve(cfg, cfg["app"]["applications_dir"])

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json", filename=None):
            data = body if isinstance(body, bytes) else (
                json.dumps(body, ensure_ascii=False).encode() if ctype == "application/json" else body.encode())
            self.send_response(code)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if "text" in ctype or "json" in ctype else ""))
            if filename:                          # download under a proper name (ASCII fallback + UTF-8 form)
                ascii_name = filename.encode("ascii", "ignore").decode() or "document.pdf"
                self.send_header("Content-Disposition", f"attachment; filename=\"{ascii_name}\"; "
                                                        f"filename*=UTF-8''{urllib.parse.quote(filename)}")
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
                if u.path == "/api/jobs":      # ?only=filtered loads the (large) filtered-out list, on demand
                    con = connect(cfg)
                    only = q.get("only", [""])[0] == "filtered"
                    rows = con.execute(
                        "SELECT id, source, title, company, location, posted_at, first_seen, types, where_ok, "
                        "flags, kw_score, ai_score, status, app_dir, apply_note "
                        "FROM jobs WHERE status " + ("= 'filtered' " if only else "!= 'filtered' ") +
                        "ORDER BY COALESCE(ai_score, kw_score) DESC, first_seen DESC LIMIT " + ("500" if only else "3000")).fetchall()
                    n = con.execute("SELECT COUNT(*) FROM jobs WHERE status='filtered'").fetchone()[0]
                    con.close()
                    return self._send(200, {"jobs": [dict(r) for r in rows], "filtered": n})
                if u.path.startswith("/api/job/"):
                    con = connect(cfg)
                    d = pipeline.job_row(con, urllib.parse.unquote(u.path[9:]))
                    con.close()
                    if d.get("app_dir"):
                        p = Path(d["app_dir"])
                        d["files"] = [f"/files/{p.name}/{f.name}" for f in sorted(p.iterdir())
                                      if not f.name.startswith(".")] if p.exists() else []
                        d["template"] = (p / ".template").exists()
                    desc = (d.get("description") or "")
                    low = desc.lower()
                    d["skills_hit"] = [k for k in cfg["profile"].get("skills", [])
                                       if re.search(rf"(?<![a-z]){re.escape(k)}(?![a-z])", low)][:14]
                    m = re.search(r"[^.\n•]{0,90}(?:(?:€|EUR|Euro)\s?\d[\d.,']{2,}|\d[\d.,']{3,}\s?(?:€|EUR|Euro))[^.\n•]{0,70}", desc)
                    d["salary_text"] = (d.get("salary") if not re.match(r"\s*0\b", d.get("salary") or "0") else "") or ((d.get("ai") or {}).get("salary") if isinstance(d.get("ai"), dict) else "") \
                        or (" ".join(m.group(0).split()) if m else "")
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
                    on = set(pipeline.enabled_sources(cfg))      # hide sources that are off or not set up
                    return self._send(200, {"runs": [dict(r) for r in rows if r["source"] in on],
                                            "counts": [dict(r) for r in counts]})
                if u.path == "/api/pdf":          # ?id=<job>&doc=cv|letter -> the tailored document as a PDF download
                    jid, doc = q.get("id", [""])[0], q.get("doc", ["cv"])[0]
                    con = connect(cfg)
                    job = pipeline.job_row(con, jid)
                    con.close()
                    folder = Path(job.get("app_dir") or "")
                    if not job.get("app_dir") or not (folder / "cover_letter.txt").exists():
                        return self._send(409, {"error": "Write the cover letter + CV for this job first."})
                    if q.get("fmt", ["pdf"])[0] == "docx":
                        from .tailor import docx_bytes
                        return self._send(200, docx_bytes(cfg, folder, doc),
                                          "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                          filename=_pdf_name(cfg, job, doc)[:-4] + ".docx")
                    with _pdf_lock:
                        cv_pdf, letter_pdf = _pdfs(cfg, job, folder)
                    f = letter_pdf if doc == "letter" else cv_pdf
                    return self._send(200, f.read_bytes(), "application/pdf", filename=_pdf_name(cfg, job, doc))
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
                        con.execute("UPDATE jobs SET status='queued', apply_note=? WHERE id=?", (FORCE, b["id"]))
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
                    if State.busy or not _apply_lock.acquire(blocking=False):
                        return self._send(409, {"error": "A run is already in progress — its log is shown here."})
                    State.busy, State.log = True, []       # set before the thread starts, so the page never sees "idle"
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
                    return self._send(200, {"ok": True, "template": (folder / ".template").exists(),
                                            "files": [f"/files/{folder.name}/{f.name}" for f in sorted(folder.iterdir())
                                                      if not f.name.startswith(".")]})
                if u.path == "/api/add":
                    con = connect(cfg)
                    if not (b.get("url", "").strip() or b.get("text", "").strip()):
                        return self._send(400, {"error": "Paste the job's link or its text first."})
                    jid = pipeline.add_url(cfg, con, b.get("url", "").strip(), b.get("text", ""),
                                           b.get("title", ""), b.get("company", ""), log=lambda *_: None)
                    con.close()
                    return self._send(200, {"ok": True, "id": jid})
                self._send(404, {"error": "not found"})
            except Exception as e:
                traceback.print_exc()
                self._send(500, {"error": str(e)})

    return H


def _bg(cfg):                       # called with _apply_lock held: only one browser may use the profile at a time
    try:
        pipeline.run(cfg, log=State.log.append)
        State.log.append("Done ✓")
    except Exception as e:
        State.log.append(f"Error: {e}")
    finally:
        State.busy = False
        _apply_lock.release()


_apply_lock = threading.Lock()
_pdf_lock = threading.Lock()


def _pdfs(cfg, job, folder: Path):
    """The job's CV and letter as PDFs, rebuilt whenever the text is newer than the PDF."""
    name = cfg["profile"]["name"].replace(" ", "_")
    cv_pdf, letter_pdf = folder / f"{name}_CV.pdf", folder / f"{name}_Cover_Letter.pdf"
    src = max((folder / n).stat().st_mtime for n in ("cover_letter.txt", "cv_tailored.md") if (folder / n).exists())
    if not (cv_pdf.exists() and letter_pdf.exists() and min(cv_pdf.stat().st_mtime, letter_pdf.stat().st_mtime) >= src):
        from playwright.sync_api import sync_playwright
        from .apply import make_documents
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            try:
                cv_pdf, letter_pdf, _ = make_documents(cfg, job, folder, b)
            finally:
                b.close()
    return cv_pdf, letter_pdf


def _pdf_name(cfg, job, doc: str) -> str:
    """Jane_Doe_CV_Magenta_Telekom.pdf / Jane_Doe_Cover_Letter_Magenta_Telekom.pdf"""
    comp = re.sub(r"\b(gmbh|ag|kg|co|se|flexco|og|mbh|ltd|inc)\b\.?", " ", job.get("company") or "", flags=re.I)
    comp = "_".join(re.findall(r"[\wÄÖÜäöüß]+", comp)[:4])
    name = cfg["profile"]["name"].replace(" ", "_")
    return f"{name}_{'Cover_Letter' if doc == 'letter' else 'CV'}{'_' + comp if comp else ''}.pdf"


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
    print(f"JobMan dashboard: {url}   (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
