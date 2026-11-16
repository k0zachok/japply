"""Add single jobs by URL (karriere.at, StepStone, LinkedIn, …) and read job-alert e-mails."""
from __future__ import annotations

import email
import hashlib
import imaplib
import json
import re
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from html.parser import HTMLParser

from .core import Job, http, strip_html, to_iso


# ------------------------------------------------------------ page -> Job
def _jsonld_postings(page: str) -> list[dict]:
    out = []
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S | re.I):
        try:
            data = json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop()
            if isinstance(d, dict):
                t = d.get("@type")
                if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                    out.append(d)
                stack.extend(v for v in d.values() if isinstance(v, (dict, list)))
            elif isinstance(d, list):
                stack.extend(d)
    return out


def _loc(jp: dict) -> str:
    locs = jp.get("jobLocation") or []
    locs = locs if isinstance(locs, list) else [locs]
    parts = []
    for l in locs:
        a = (l or {}).get("address") or {}
        if isinstance(a, dict):
            parts.append(", ".join(filter(None, [a.get("postalCode"), a.get("addressLocality"), a.get("addressCountry")
                                                 if isinstance(a.get("addressCountry"), str) else ""])))
    return "; ".join(p for p in parts if p)


def job_from_page(url: str, page: str, source: str = "manual") -> Job:
    """Build a Job from a job page. Uses schema.org JobPosting data when the site provides it."""
    jid = hashlib.sha1(url.encode()).hexdigest()[:16]
    jps = _jsonld_postings(page)
    if jps:
        jp = jps[0]
        org = jp.get("hiringOrganization") or {}
        remote = jp.get("jobLocationType") == "TELECOMMUTE"
        return Job(source, jid, strip_html(jp.get("title", "")), org.get("name", "") if isinstance(org, dict) else str(org),
                   _loc(jp) + (" (remote)" if remote else ""), url, strip_html(jp.get("description", "")),
                   to_iso(jp.get("datePosted")), remote, " ".join(jp.get("employmentType", []))
                   if isinstance(jp.get("employmentType"), list) else str(jp.get("employmentType", "")))
    title = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    body = re.search(r"<main.*?</main>", page, re.S | re.I) or re.search(r"<body.*?</body>", page, re.S | re.I)
    return Job(source, jid, strip_html(title.group(1)) if title else url, "", "", url,
               strip_html(body.group(0) if body else page)[:15000])


def fetch_job(url: str) -> Job:
    return job_from_page(url, http(url, headers={"Accept": "text/html"}))


# ------------------------------------------------------------ job alert e-mails
ALERT_PATTERNS = {
    "karriere.at": re.compile(r"https?://(?:www\.)?karriere\.at/jobs/(\d{5,})"),
    "linkedin": re.compile(r"https?://(?:[a-z]+\.)?linkedin\.com/(?:comm/)?jobs/view/(\d+)"),
    "stepstone": re.compile(r"https?://(?:www\.)?stepstone\.(?:at|de)/stellenangebote--[^\s\"'<>?]*?-(\d{6,})-inline\.html"),
    "ams": re.compile(r"https?://jobs\.ams\.at/public/emps/jobs/([0-9a-f-]{20,})"),
    "willhaben": re.compile(r"https?://(?:www\.)?willhaben\.at/jobs/job/[^\s\"'<>?]*?-(\d{5,})"),
    "jobs.at": re.compile(r"https?://(?:www\.)?jobs\.at/j/[^\s\"'<>?]+"),
    "indeed": re.compile(r"https?://(?:at\.)?indeed\.com/(?:rc/clk|viewjob|pagead/clk)\?[^\s\"'<>]*?jk=([0-9a-f]{10,})"),
    "devjobs.at": re.compile(r"https?://(?:en\.)?devjobs\.at/jobs/[^\s\"'<>?]+"),
}


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self._href, self._txt = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href, self._txt = dict(attrs).get("href"), []

    def handle_data(self, data):
        if self._href is not None:
            self._txt.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            self.links.append((self._href, " ".join(" ".join(self._txt).split())))
            self._href = None


def jobs_from_email_html(body: str, subject: str = "") -> list[Job]:
    p = _Links()
    p.feed(body)
    found: dict[str, Job] = {}
    for href, text in p.links:
        for site, pat in ALERT_PATTERNS.items():
            m = pat.search(href)
            if not m:
                continue
            key = m.group(1) if m.groups() else hashlib.sha1(m.group(0).encode()).hexdigest()[:12]
            url = m.group(0)
            if site == "linkedin":
                url = f"https://www.linkedin.com/jobs/view/{key}"
            elif site == "indeed":
                url = f"https://at.indeed.com/viewjob?jk={key}"
            j = found.get(f"{site}:{key}")
            # keep the longest anchor text as title (logo links have none)
            if text and len(text) > 3 and (not j or len(text) > len(j.title)) and len(text) < 160:
                found[f"{site}:{key}"] = Job(f"alert-{site}", key, text, "", "", url, "", None, False, subject)
            elif not j:
                found[f"{site}:{key}"] = Job(f"alert-{site}", key, "", "", "", url, "", None, False, subject)
    return [j for j in found.values() if j.title]


def read_alert_emails(cfg: dict, log=print) -> list[Job]:
    s = cfg["sources"]["email_alerts"]
    if not s.get("app_password"):
        raise RuntimeError("no app_password set for e-mail alerts")
    since = (datetime.now() - timedelta(days=int(s.get("since_days", 14)))).strftime("%d-%b-%Y")
    m = imaplib.IMAP4_SSL(s.get("imap_host", "imap.gmail.com"))
    m.login(s["user"], s["app_password"])
    m.select(s.get("folder", "INBOX"), readonly=True)
    senders = ["karriere.at", "linkedin.com", "stepstone", "ams.at", "willhaben", "jobs.at", "indeed", "devjobs"]
    ids: set[bytes] = set()
    for snd in senders:
        typ, data = m.search(None, "SINCE", since, "FROM", f'"{snd}"')
        if typ == "OK":
            ids.update(data[0].split())
    jobs: list[Job] = []
    for i in sorted(ids):
        typ, data = m.fetch(i, "(RFC822)")
        if typ != "OK":
            continue
        msg = email.message_from_bytes(data[0][1])
        subj = str(make_header(decode_header(msg.get("Subject", ""))))
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                html_body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                got = jobs_from_email_html(html_body, subj)
                for j in got:
                    j.posted_at = to_iso(msg.get("Date"))
                jobs.extend(got)
    m.logout()
    log(f"  read {len(ids)} alert e-mails")
    return jobs
