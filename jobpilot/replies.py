"""Reply tracking: read your inbox (IMAP, read-only) and note what employers answered.

Confirmations are added to the job's note, rejections move the job to "Rejected", clear interview invitations
move it to "Interview". Nothing is ever sent, moved, marked read or deleted in the mailbox.
"""
from __future__ import annotations

import email
import imaplib
import os
import re
import time
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

from .core import strip_html

REJECT = re.compile(r"leider|bedauer|absage|nicht berücksichtig|keine (positive|zusage)|anderen? (bewerber|kandidat)|"
                    r"unfortunately|we regret|regret to|not (be )?(moving|proceeding) forward|other candidates|"
                    r"decided not to|not been successful|won'?t be (moving|progressing)|position has been filled", re.I)
INVITE = re.compile(r"einladung zu(m|r|\s+einem)|laden (sie|dich) (herzlich )?(zu|ein)|termin (vereinbaren|vorschlagen|finden)|"
                    r"(would|we'd) like to (invite|speak|meet|schedule)|invite you (to|for)|schedule (a|an|your) "
                    r"(call|interview|meeting)|interview (invitation|termin)|kennenlern(gespräch|termin)|"
                    r"wann (hätten|haben) sie zeit|your availability", re.I)
CONFIRM = re.compile(r"eingegangen|erhalten|received your application|thank you for (your )?(application|applying|interest)|"
                     r"(danke|vielen dank) für (deine|ihre|die) bewerbung|bewerbung.{0,30}bestätig|successfully submitted|"
                     r"application.{0,20}(submitted|received)", re.I)
STOP = {"gmbh", "group", "gruppe", "austria", "österreich", "konzern", "wien", "vienna", "the", "und", "and", "für"}


def accounts(cfg: dict) -> list[dict]:
    """Every mailbox we have a password for: the application mailbox and (if set up) your personal one."""
    a, out = cfg.get("apply", {}), []
    m, e = a.get("mailbox", {}), a.get("email", {})
    pw = os.environ.get("JOBPILOT_MAILBOX_PASSWORD") or m.get("app_password", "")
    if m.get("user") and pw:
        out.append({"host": m.get("imap_host", "imap.gmail.com"), "user": m["user"], "pw": pw, "folder": m.get("folder", "INBOX")})
    pw = os.environ.get("JOBPILOT_EMAIL_PASSWORD") or e.get("app_password", "")
    if e.get("user") and pw and e["user"] != m.get("user"):
        out.append({"host": e.get("imap_host", "imap.gmail.com"), "user": e["user"], "pw": pw, "folder": "INBOX"})
    return out


def company_keys(company: str) -> list[str]:
    """Distinctive words of a company name, e.g. "Wolf Theiss Rechtsanwälte GmbH & Co KG" -> ["wolf", "theiss", ...]."""
    return [w for w in re.findall(r"[a-zäöüß0-9]{4,}", (company or "").lower()) if w not in STOP][:3]


def classify(subject: str, body: str) -> str | None:
    """'rejected' | 'interview' | 'confirmed' | None — rejection wins, an invitation must be explicit."""
    text = f"{subject}\n{body[:3000]}"
    if REJECT.search(text):
        return "rejected"
    if INVITE.search(text) and not CONFIRM.search(subject):
        return "interview"
    if CONFIRM.search(text):
        return "confirmed"
    return None


def match_job(jobs: list[dict], sender: str, subject: str, body: str) -> dict | None:
    """The applied job this message is about: company words in sender/subject/body, title words break ties."""
    hay_head, hay = f"{sender} {subject}".lower(), f"{sender} {subject} {body[:2500]}".lower()
    best, score = None, 0
    for j in jobs:
        keys = company_keys(j.get("company", ""))
        if not keys:
            continue
        s = 3 * sum(k in hay_head for k in keys) + sum(k in hay for k in keys)
        if not s:
            continue
        s += sum(w in hay for w in re.findall(r"[a-zäöüß]{5,}", (j.get("title") or "").lower())[:5])
        if s > score:
            best, score = j, s
    return best if score >= 2 else None


def _messages(acc: dict, since: datetime, limit: int = 250):
    box = imaplib.IMAP4_SSL(acc["host"])
    try:
        box.login(acc["user"], acc["pw"])
        box.select(acc["folder"], readonly=True)
        typ, data = box.search(None, "SINCE", (since - timedelta(days=1)).strftime("%d-%b-%Y"))
        for i in reversed(data[0].split()[-limit:] if typ == "OK" else []):
            typ, d = box.fetch(i, "(BODY.PEEK[])")
            if typ != "OK" or not d or not d[0]:
                continue
            msg = email.message_from_bytes(d[0][1])
            try:
                when = parsedate_to_datetime(msg.get("Date"))
            except Exception:
                continue
            body = ""
            for part in msg.walk():
                if part.get_content_type() in ("text/plain", "text/html"):
                    try:
                        body += "\n" + part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                    except Exception:
                        pass
            yield (msg.get("Message-ID") or f"{acc['user']}:{i.decode()}", when,
                   str(make_header(decode_header(msg.get("From", "")))),
                   str(make_header(decode_header(msg.get("Subject", "")))), strip_html(body))
    finally:
        try:
            box.logout()
        except Exception:
            pass


def check(cfg: dict, con, log=print) -> dict:
    stats = {"confirmed": 0, "interview": 0, "rejected": 0}
    accs = accounts(cfg)
    jobs = [dict(r) for r in con.execute(
        "SELECT id, title, company, status, apply_note, applied_at FROM jobs WHERE status IN ('applied', 'interview')")]
    if not accs or not jobs:
        if jobs and not accs:
            log("  inbox: no mailbox password set, replies are not tracked "
                "(JOBPILOT_EMAIL_PASSWORD for the address you put on forms)")
        return stats
    con.execute("CREATE TABLE IF NOT EXISTS mail_seen (msgid TEXT PRIMARY KEY)")
    seen = {r[0] for r in con.execute("SELECT msgid FROM mail_seen")}
    since = datetime.now(timezone.utc) - timedelta(days=45)
    for acc in accs:
        try:
            for msgid, when, sender, subject, body in _messages(acc, since):
                if msgid in seen:
                    continue
                seen.add(msgid)
                con.execute("INSERT OR IGNORE INTO mail_seen (msgid) VALUES (?)", (msgid,))
                kind = classify(subject, body)
                job = match_job(jobs, sender, subject, body) if kind else None
                if not job:
                    continue
                note = f"✉ {when.strftime('%d %b')}: " + {"confirmed": "confirmation e-mail received",
                       "interview": "they want to talk — see your inbox", "rejected": "rejection received"}[kind] \
                       + f" (“{subject[:60]}”)"
                status = {"rejected": "rejected", "interview": "interview"}.get(kind, job["status"])
                con.execute("UPDATE jobs SET status=?, apply_note=? WHERE id=?",
                            (status, (note + " · " + (job.get("apply_note") or ""))[:400], job["id"]))
                job["status"], job["apply_note"] = status, note
                stats[kind] += 1
            con.commit()
        except Exception as e:
            log(f"  inbox ({acc['user']}): couldn't read it — {str(e)[:100]}")
    if any(stats.values()):
        log(f"  inbox: {stats['confirmed']} confirmation(s), {stats['interview']} invitation(s), {stats['rejected']} rejection(s)")
    return stats
