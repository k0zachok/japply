"""Config, HTTP, database and text helpers."""
from __future__ import annotations

import gzip
import html
import json
import re
import sqlite3
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UA = "Mozilla/5.0 (X11; Linux x86_64) JobMan/1.0 (personal job search)"


# ---------------------------------------------------------------- config
def load_config(path: str | Path | None = None) -> dict:
    p = Path(path) if path else ROOT / "config.toml"
    with open(p, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_path"] = str(p)
    return cfg


def resolve(cfg: dict, rel: str) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else Path(cfg["_path"]).parent / p


def load_cv_text(cfg: dict) -> str:
    p = resolve(cfg, cfg["profile"]["cv_file"])
    if p.suffix.lower() == ".pdf":
        from pypdf import PdfReader  # optional dependency
        return "\n".join(pg.extract_text() or "" for pg in PdfReader(str(p)).pages)
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------- http
class HttpError(Exception):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} for {url}")
        self.status, self.url, self.body = status, url, body


def http(url: str, *, params: dict | None = None, data=None, headers: dict | None = None,
         method: str | None = None, timeout: int = 30, retries: int = 2) -> str:
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(
            {k: v for k, v in params.items() if v not in (None, "")}, doseq=True)
    hdrs = {"User-Agent": UA, "Accept": "application/json, text/html, application/xml;q=0.9, */*;q=0.8",
            "Accept-Encoding": "gzip"}
    hdrs.update(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode() if not isinstance(data, (bytes, str)) else (
            data.encode() if isinstance(data, str) else data)
        hdrs.setdefault("Content-Type", "application/json")
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                cs = r.headers.get_content_charset() or "utf-8"
                return raw.decode(cs, errors="replace")
        except urllib.error.HTTPError as e:
            txt = ""
            try:
                txt = e.read().decode(errors="replace")[:3000]
            except Exception:
                pass
            last = HttpError(e.code, url, txt)
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(3 * (attempt + 1))
                continue
            raise last
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            raise
    raise last  # pragma: no cover


def get_json(url: str, **kw):
    return json.loads(http(url, **kw))


# ---------------------------------------------------------------- text
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")


def strip_html(s: str | None) -> str:
    if not s:
        return ""
    s = html.unescape(s)  # some APIs double-escape HTML
    s = re.sub(r"(?i)<\s*(br|/p|/li|/h\d|/div|/tr)\s*/?>", "\n", s)
    s = re.sub(r"(?i)<\s*li[^>]*>", "\n• ", s)
    s = re.sub(r"(?is)<(script|style).*?</\1>", "", s)
    s = html.unescape(_TAG.sub("", s))
    s = _WS.sub(" ", s)
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)
    return s.strip()


def norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"\((m|w|d|f|x|div|all genders?)[^)]*\)", " ", s)
    s = re.sub(r"[^a-z0-9äöüß]+", " ", s)
    return " ".join(s.split())


def to_iso(v) -> str | None:
    """Accept unix seconds/ms, ISO strings, RFC822 — return ISO date-time (UTC)."""
    if v in (None, "", 0):
        return None
    try:
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
            n = float(v)
            if n > 1e12:
                n /= 1000
            return datetime.fromtimestamp(n, tz=timezone.utc).isoformat()
        s = str(v).strip()
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            from email.utils import parsedate_to_datetime
            d = parsedate_to_datetime(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- model
@dataclass
class Job:
    source: str
    ext_id: str
    title: str
    company: str = ""
    location: str = ""
    url: str = ""
    description: str = ""
    posted_at: str | None = None
    remote: bool = False
    employment_hint: str = ""     # raw text like "part_time", "Werkstudent", "Internship"
    salary: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.source}:{self.ext_id}"


# ---------------------------------------------------------------- db
SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  dedup_key TEXT,
  source TEXT, title TEXT, company TEXT, location TEXT, url TEXT,
  description TEXT, posted_at TEXT, remote INTEGER, employment_hint TEXT, salary TEXT,
  first_seen TEXT, last_seen TEXT,
  types TEXT, where_ok TEXT, flags TEXT,
  kw_score INTEGER, ai_score INTEGER, ai_json TEXT,
  status TEXT DEFAULT 'new', app_dir TEXT, notes TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_dedup ON jobs(dedup_key);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started TEXT, source TEXT, fetched INTEGER,
  new INTEGER, error TEXT
);
-- answers to application-form questions; answer NULL = waiting for you to answer it in the dashboard
CREATE TABLE IF NOT EXISTS answers (
  key TEXT PRIMARY KEY, question TEXT, options TEXT, answer TEXT, job_id TEXT
);
"""


def connect(cfg: dict) -> sqlite3.Connection:
    # The dashboard, the applier and a fetch/score run may all write at once: wait for a lock instead of failing,
    # and use WAL so readers never block writers.
    con = sqlite3.connect(resolve(cfg, cfg["app"]["db_file"]), check_same_thread=False, timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=60000")
    con.executescript(SCHEMA)
    have = {r["name"] for r in con.execute("PRAGMA table_info(jobs)")}
    for col in ("apply_note", "applied_at"):
        if col not in have:
            con.execute(f"ALTER TABLE jobs ADD COLUMN {col} TEXT")
    con.commit()
    return con


def dedup_key(j: Job) -> str:
    return f"{norm(j.company)}|{norm(j.title)}" if j.company else f"{j.source}|{j.ext_id}"


def upsert(con: sqlite3.Connection, j: Job) -> bool:
    """Insert job; returns True if it is new. Same company+title from another source = duplicate."""
    ts = now_iso()
    key = dedup_key(j)
    row = con.execute("SELECT id, description FROM jobs WHERE id=? OR dedup_key=?", (j.id, key)).fetchone()
    if row:
        # keep the longest description we have seen
        if j.description and len(j.description) > len(row["description"] or ""):
            con.execute("UPDATE jobs SET description=?, last_seen=? WHERE id=?", (j.description, ts, row["id"]))
        else:
            con.execute("UPDATE jobs SET last_seen=? WHERE id=?", (ts, row["id"]))
        return False
    con.execute(
        """INSERT INTO jobs (id, dedup_key, source, title, company, location, url, description, posted_at,
           remote, employment_hint, salary, first_seen, last_seen)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (j.id, key, j.source, j.title.strip(), (j.company or "").strip(), (j.location or "").strip(), j.url,
         j.description, j.posted_at, int(bool(j.remote)), j.employment_hint, j.salary, ts, ts))
    return True
