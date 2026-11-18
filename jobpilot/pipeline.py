"""fetch -> classify -> AI score."""
from __future__ import annotations

import json

from .core import connect, now_iso, upsert
from .importer import fetch_job, read_alert_emails
from .llm import LLM
from .match import ai_score_all, classify_all, triage
from .sources import REGISTRY


def enabled_sources(cfg: dict) -> list[str]:
    src = cfg["sources"]
    names = [n for n in REGISTRY if n == "companies" or src.get(n, {}).get("enabled", False)]
    # sources that are switched on but not set up yet are skipped quietly instead of failing every run
    if "adzuna" in names and not (src["adzuna"].get("app_id") and src["adzuna"].get("app_key")):
        names.remove("adzuna")
    if "rss" in names and not src.get("rss", {}).get("feeds"):
        names.remove("rss")
    if src.get("email_alerts", {}).get("enabled"):
        names.append("email_alerts")
    return names


def fetch(cfg: dict, con, only: list[str] | None = None, log=print) -> dict:
    totals = {}
    for name in only or enabled_sources(cfg):
        fetched = new = 0
        err = None
        try:
            if name == "email_alerts":
                it = read_alert_emails(cfg, log)
                for j in it:
                    j.location = j.location or "Wien (from your job alert)"
            else:
                it = REGISTRY[name](cfg)
            known_run = 0
            for j in it:
                if not j.title or not j.url:
                    continue
                fetched += 1
                is_new = upsert(con, j)
                con.commit()          # never hold the write lock across the next (slow) network request
                new += is_new
                known_run = 0 if is_new else known_run + 1
                if name == "arbeitnow" and known_run >= 250:      # newest-first feed: the rest is already in the DB
                    break
        except Exception as e:
            err = str(e)[:300]
        con.execute("INSERT INTO runs (started, source, fetched, new, error) VALUES (?,?,?,?,?)",
                    (now_iso(), name, fetched, new, err))
        con.commit()
        totals[name] = (fetched, new, err)
        log(f"  {name:15s} {fetched:5d} fetched, {new:5d} new" + (f"   ⚠ {err}" if err else ""))
    return totals


def score(cfg: dict, con, log=print) -> None:
    st = classify_all(cfg, con)
    log(f"  classified: {st['kept']} match your filters, {st['hidden']} filtered out")
    llm = LLM(cfg)
    if llm.enabled:
        try:
            n = ai_score_all(cfg, con, llm, log)
        except Exception as e:
            log(f"  ⚠ AI scoring unavailable ({e}). Keyword scores are still used.")
    ai_on = llm.enabled and con.execute("SELECT 1 FROM jobs WHERE ai_score IS NOT NULL LIMIT 1").fetchone() is not None
    t = triage(cfg, con, ai_on)
    log(f"  triage: {t['queued']} to auto-apply, {t['review']} for you to review, {t['skipped']} skipped")


STATUSES_SHOWN = ("applied", "manual", "review", "queued", "interview", "rejected")


def _counts(con) -> dict:
    return {r[0]: r[1] for r in con.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status")}


def notify(title: str, text: str) -> None:
    """A macOS notification (silently skipped elsewhere)."""
    import subprocess
    esc = lambda x: x.replace("\\", "\\\\").replace('"', '\\"')
    try:
        subprocess.run(["osascript", "-e", f'display notification "{esc(text)}" with title "{esc(title)}"'],
                       timeout=10, capture_output=True)
    except Exception:
        pass


def run(cfg: dict, log=print) -> dict:
    """The full daily pass. Writes a summary to last_run.json and shows a notification."""
    import json as _json
    from .core import resolve
    started, errors, lines = now_iso(), [], []

    def say(msg):
        lines.append(msg)
        if "⚠" in msg or "failed" in msg.lower():
            errors.append(msg.strip()[:200])
        log(msg)

    con = connect(cfg)
    before = _counts(con)
    inbox = {}
    try:                                    # what did employers answer since the last run?
        from . import replies
        inbox = replies.check(cfg, con, log=say)
    except Exception as e:
        say(f"  inbox check failed: {str(e)[:120]}")
    say("Fetching jobs…")
    totals = fetch(cfg, con, log=say)
    say("Scoring…")
    score(cfg, con, log=say)
    if cfg.get("apply", {}).get("enabled", True):
        from .apply import apply_queued
        apply_queued(cfg, con, log=say)
    after = _counts(con)
    con.close()
    diff = {k: after.get(k, 0) - before.get(k, 0) for k in STATUSES_SHOWN}
    summary = {"started": started, "finished": now_iso(),
               "new_jobs": sum(t[1] for t in totals.values()), "applied": max(0, diff["applied"]),
               "needs_you": after.get("manual", 0), "to_review": after.get("review", 0),
               "queued": after.get("queued", 0), "replies": inbox,
               "errors": errors[-8:], "totals": {k: after.get(k, 0) for k in STATUSES_SHOWN}}
    try:
        resolve(cfg, "last_run.json").write_text(_json.dumps(summary, ensure_ascii=False, indent=1))
    except Exception:
        pass
    text = (f"{summary['applied']} applied · {summary['to_review']} to review · {summary['needs_you']} need you"
            + (f" · {inbox.get('interview', 0)} invitation(s)!" if inbox.get("interview") else "")
            + (f" · {len(errors)} problem(s)" if errors else ""))
    notify("JobMan daily run", text)
    return summary


def add_url(cfg: dict, con, url: str, text: str = "", title: str = "", company: str = "", log=print) -> str:
    """Add one job by URL (and optionally its pasted text — needed for LinkedIn, which requires login)."""
    from .core import Job
    import hashlib
    if text:
        j = Job("manual", hashlib.sha1((url or text).encode()).hexdigest()[:16], title or text.strip().splitlines()[0][:120],
                company, "", url or "", text)
    else:
        j = fetch_job(url)
        if title:
            j.title = title
        if company:
            j.company = company
    from .core import dedup_key
    upsert(con, j)
    jid = con.execute("SELECT id FROM jobs WHERE id=? OR dedup_key=?", (j.id, dedup_key(j))).fetchone()["id"]
    # manual jobs are never auto-hidden
    con.execute("UPDATE jobs SET status='saved', kw_score=NULL, ai_score=NULL WHERE id=?", (jid,))
    con.commit()
    score(cfg, con, log=log)
    return jid


def job_row(con, job_id: str) -> dict:
    r = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not r:
        raise KeyError(job_id)
    d = dict(r)
    d["ai"] = json.loads(d["ai_json"]) if d.get("ai_json") else None
    return d


def ensure_description(cfg, con, job: dict, log=print) -> dict:
    """Alert/SmartRecruiters jobs arrive without text — fetch the single job page when you tailor."""
    if job.get("description") and len(job["description"]) > 300:
        return job
    try:
        if job["source"] == "smartrecruiters":
            from .core import get_json, strip_html
            slug, pid = job["id"].split(":", 1)[1].rsplit("-", 1)[0], job["url"].rsplit("/", 1)[-1]
            d = get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{pid}")
            secs = (d.get("jobAd") or {}).get("sections") or {}
            desc = "\n\n".join(strip_html((v or {}).get("text")) for v in secs.values())
        else:
            desc = fetch_job(job["url"]).description
        if desc:
            con.execute("UPDATE jobs SET description=? WHERE id=?", (desc, job["id"]))
            con.commit()
            job["description"] = desc
    except Exception as e:
        log(f"  couldn't fetch the job text ({e}). Paste it via 'add --text' for a better letter.")
    return job
