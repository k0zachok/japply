"""Job sources. Each fetcher yields Job objects. Only public APIs/feeds are used."""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from typing import Callable, Iterable, Iterator

from .core import HttpError, Job, get_json, http, strip_html, to_iso

Fetcher = Callable[[dict], Iterable[Job]]
REGISTRY: dict[str, Fetcher] = {}


def source(name: str):
    def deco(fn):
        REGISTRY[name] = fn
        return fn
    return deco


def _kw(cfg) -> list[str]:
    return cfg["search"]["keywords"]


# ------------------------------------------------------------- Vienna / Austria
@source("adzuna")
def adzuna(cfg) -> Iterator[Job]:
    """Adzuna aggregates most Austrian job boards and company sites. Free key required."""
    s = cfg["sources"]["adzuna"]
    if not (s.get("app_id") and s.get("app_key")):
        raise RuntimeError("no app_id/app_key — get a free key at developer.adzuna.com")
    for kw in _kw(cfg):
        for page in range(1, int(s.get("pages_per_keyword", 2)) + 1):
            d = get_json(f"https://api.adzuna.com/v1/api/jobs/{s.get('country', 'at')}/search/{page}", params={
                "app_id": s["app_id"], "app_key": s["app_key"], "what": kw, "where": s.get("where", "Wien"),
                "distance": s.get("distance_km", 30), "max_days_old": cfg["search"].get("max_days_old", 30),
                "results_per_page": 50, "sort_by": "date", "content-type": "application/json"})
            res = d.get("results", [])
            for r in res:
                yield Job("adzuna", str(r["id"]), r.get("title", ""),
                          (r.get("company") or {}).get("display_name", ""),
                          (r.get("location") or {}).get("display_name", ""),
                          r.get("redirect_url", ""), strip_html(r.get("description")),
                          to_iso(r.get("created")), False,
                          " ".join(filter(None, [r.get("contract_time"), r.get("contract_type")])),
                          _salary(r.get("salary_min"), r.get("salary_max")))
            if len(res) < 50:
                break
            time.sleep(1.2)


@source("karriere")
def karriere(cfg) -> Iterator[Job]:
    """karriere.at — Austria's biggest job board. Reads its public search pages, then each new job's page."""
    import urllib.parse
    from .core import connect
    from .importer import fetch_job
    s = cfg["sources"]["karriere"]
    where = urllib.parse.quote(s.get("where", "wien"))
    known = {r[0] for r in connect(cfg).execute("SELECT id FROM jobs WHERE source='karriere'")}
    seen, budget = set(), int(s.get("max_new_per_run", 150))
    for kw in _kw(cfg):
        for page in range(1, int(s.get("pages_per_keyword", 2)) + 1):
            html = http(f"https://www.karriere.at/jobs/{urllib.parse.quote(kw.replace(' ', '-'))}/{where}"
                        + (f"?page={page}" if page > 1 else ""), headers={"Accept": "text/html"})
            ids = list(dict.fromkeys(re.findall(r'href="https://www\.karriere\.at/jobs/(\d{5,})"', html)))
            for jid in ids:
                if jid in seen or f"karriere:{jid}" in known:
                    continue
                seen.add(jid)
                if budget <= 0:
                    return
                budget -= 1
                try:
                    j = fetch_job(f"https://www.karriere.at/jobs/{jid}")
                except Exception:
                    continue
                j.source, j.ext_id = "karriere", jid
                yield j
                time.sleep(0.4)
            if len(ids) < 10:
                break
            time.sleep(1.0)


@source("arbeitnow")
def arbeitnow(cfg) -> Iterator[Job]:
    """Free DACH job board API built from company ATS systems (Greenhouse, Personio, …)."""
    pages = int(cfg["sources"]["arbeitnow"].get("pages", 15))
    for page in range(1, pages + 1):
        d = get_json("https://www.arbeitnow.com/api/job-board-api", params={"page": page})
        data = d.get("data", [])
        for r in data:
            yield Job("arbeitnow", r.get("slug", ""), r.get("title", ""), r.get("company_name", ""),
                      r.get("location", ""), r.get("url", ""), strip_html(r.get("description")),
                      to_iso(r.get("created_at")), bool(r.get("remote")),
                      " ".join(r.get("job_types") or []) + " " + " ".join(r.get("tags") or []))
        if not data or not (d.get("links") or {}).get("next"):
            break
        time.sleep(0.7)


# ------------------------------------------------------------- remote boards
@source("himalayas")
def himalayas(cfg) -> Iterator[Job]:
    pages = int(cfg["sources"]["himalayas"].get("pages_per_keyword", 2))
    for kw in _kw(cfg):
        for page in range(1, pages + 1):
            d = get_json("https://himalayas.app/jobs/api/search", params={"q": kw, "sort": "recent", "page": page})
            jobs = d.get("jobs", [])
            for r in jobs:
                loc = ", ".join(r.get("locationRestrictions") or []) or "Remote (worldwide)"
                yield Job("himalayas", r.get("guid") or r.get("applicationLink", ""), r.get("title", ""),
                          r.get("companyName", ""), f"Remote — {loc}", r.get("applicationLink", ""),
                          strip_html(r.get("description") or r.get("excerpt")), to_iso(r.get("pubDate")), True,
                          f"{r.get('employmentType', '')} {r.get('seniority', '')}",
                          _salary(r.get("minSalary"), r.get("maxSalary"), r.get("currency") or ""))
            if len(jobs) < 20:
                break
            time.sleep(1)


@source("jobicy")
def jobicy(cfg) -> Iterator[Job]:
    geo = cfg["sources"]["jobicy"].get("geo", "europe")
    seen = set()
    for params in ({"count": 100, "geo": geo}, {"count": 100, "tag": "python"}):
        d = get_json("https://jobicy.com/api/v2/remote-jobs", params=params)
        for r in d.get("jobs", []):
            if r.get("id") in seen:
                continue
            seen.add(r.get("id"))
            yield Job("jobicy", str(r.get("id")), r.get("jobTitle", ""), r.get("companyName", ""),
                      f"Remote — {r.get('jobGeo', '')}", r.get("url", ""),
                      strip_html(r.get("jobDescription") or r.get("jobExcerpt")), to_iso(r.get("pubDate")), True,
                      " ".join(r.get("jobType") or []) + f" {r.get('jobLevel', '')}",
                      _salary(r.get("annualSalaryMin"), r.get("annualSalaryMax"), r.get("salaryCurrency") or ""))
        time.sleep(1)


@source("remoteok")
def remoteok(cfg) -> Iterator[Job]:
    d = get_json("https://remoteok.com/api")
    for r in d:
        if not isinstance(r, dict) or "id" not in r or "position" not in r:
            continue  # first element is the legal notice
        yield Job("remoteok", str(r["id"]), r.get("position", ""), r.get("company", ""),
                  f"Remote — {r.get('location') or 'worldwide'}", r.get("url", ""),
                  strip_html(r.get("description")), to_iso(r.get("epoch") or r.get("date")), True,
                  " ".join(r.get("tags") or []), _salary(r.get("salary_min"), r.get("salary_max"), "USD"))


@source("remotive")
def remotive(cfg) -> Iterator[Job]:
    d = get_json("https://remotive.com/api/remote-jobs")
    for r in d.get("jobs", []):
        yield Job("remotive", str(r.get("id")), r.get("title", ""), r.get("company_name", ""),
                  f"Remote — {r.get('candidate_required_location', '')}", r.get("url", ""),
                  strip_html(r.get("description")), to_iso(r.get("publication_date")), True,
                  r.get("job_type", ""), r.get("salary", ""))


@source("weworkremotely")
def weworkremotely(cfg) -> Iterator[Job]:
    for feed in cfg["sources"]["weworkremotely"].get("feeds", []):
        for j in _rss(feed, "weworkremotely", remote=True):
            # WWR titles look like "Company: Job title"
            if ":" in j.title and not j.company:
                j.company, j.title = [x.strip() for x in j.title.split(":", 1)]
            yield j


@source("rss")
def rss(cfg) -> Iterator[Job]:
    for feed in cfg["sources"]["rss"].get("feeds", []):
        yield from _rss(feed, "rss")


def _rss(url: str, src: str, remote: bool = False) -> Iterator[Job]:
    root = ET.fromstring(http(url).encode())
    ns = {"a": "http://www.w3.org/2005/Atom"}
    items = root.findall(".//item") or root.findall(".//a:entry", ns)
    for it in items:
        g = lambda tag: (it.findtext(tag) or it.findtext(f"a:{tag}", namespaces=ns) or "").strip()
        link = g("link")
        if not link:
            el = it.find("a:link", ns)
            link = el.get("href", "") if el is not None else ""
        region = g("region") or g("location")
        yield Job(src, g("guid") or g("id") or link, g("title"), g("company"),
                  (f"Remote — {region}" if remote else region), link,
                  strip_html(g("description") or g("summary") or g("content")),
                  to_iso(g("pubDate") or g("updated") or g("published")), remote, g("type"))


# ------------------------------------------------------------- company ATS boards
def _post_json(url: str, body: dict) -> dict:
    import json as _j, urllib.request
    req = urllib.request.Request(url, data=_j.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json",
                                          "User-Agent": "Mozilla/5.0 (JobPilot personal job search)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return _j.loads(r.read().decode("utf-8", "replace"))


def ats_jobs(ats: str, slug: str, company: str = "") -> list[Job]:
    """Public career-page APIs. Returns [] if the board doesn't exist."""
    name = company or slug
    out: list[Job] = []
    if ats == "greenhouse":
        d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"})
        for r in d.get("jobs", []):
            out.append(Job(f"greenhouse", f"{slug}-{r['id']}", r.get("title", ""), name,
                           (r.get("location") or {}).get("name", ""), r.get("absolute_url", ""),
                           strip_html(r.get("content")), to_iso(r.get("updated_at") or r.get("first_published"))))
    elif ats == "lever":
        d = None
        for host in ("api.lever.co", "api.eu.lever.co"):
            try:
                d = get_json(f"https://{host}/v0/postings/{slug}", params={"mode": "json"})
                if d:
                    break
            except HttpError as e:
                if e.status != 404:
                    raise
        for r in d or []:
            c = r.get("categories") or {}
            locs = c.get("allLocations") or [c.get("location", "")]
            out.append(Job("lever", f"{slug}-{r['id']}", r.get("text", ""), name, ", ".join(filter(None, locs)),
                           r.get("hostedUrl", ""), r.get("descriptionPlain") or strip_html(r.get("description")),
                           to_iso(r.get("createdAt")), (r.get("workplaceType") == "remote"),
                           c.get("commitment", "")))
    elif ats == "ashby":
        d = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
        for r in d.get("jobs", []):
            locs = [r.get("location", "")] + [x.get("location", "") for x in r.get("secondaryLocations") or []]
            out.append(Job("ashby", f"{slug}-{r['id']}", r.get("title", ""), name, ", ".join(filter(None, locs)),
                           r.get("jobUrl", ""), r.get("descriptionPlain") or strip_html(r.get("descriptionHtml")),
                           to_iso(r.get("publishedAt")), bool(r.get("isRemote")), r.get("employmentType", "")))
    elif ats == "smartrecruiters":
        offset = 0
        slug, _, country = slug.partition("@")          # "BoschGroup@at" = only that country's jobs
        while True:
            d = get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
                         params={"limit": 100, "offset": offset, **({"country": country} if country else {})})
            content = d.get("content", [])
            for r in content:
                loc = r.get("location") or {}
                out.append(Job("smartrecruiters", f"{slug}-{r['id']}", r.get("name", ""),
                               (r.get("company") or {}).get("name", name),
                               ", ".join(filter(None, [loc.get("city"), loc.get("country", "").upper()])),
                               f"https://jobs.smartrecruiters.com/{slug}/{r['id']}", "",
                               to_iso(r.get("releasedDate")), bool(loc.get("remote")),
                               (r.get("typeOfEmployment") or {}).get("label", ""),
                               extra={"detail": r.get("ref", "")}))
                if country:                                   # small filtered set: fetch the job text right away
                    try:
                        secs = ((get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{r['id']}")
                                 .get("jobAd") or {}).get("sections") or {})
                        out[-1].description = "\n\n".join(strip_html((v or {}).get("text")) for v in secs.values())
                    except Exception:
                        pass
            offset += len(content)
            if not content or offset >= d.get("totalFound", 0):
                break
    elif ats == "personio":
        xml = None
        for tld in ("de", "com"):
            try:
                xml = http(f"https://{slug}.jobs.personio.{tld}/xml")
                if "<position" in xml:
                    base = f"https://{slug}.jobs.personio.{tld}"
                    break
            except HttpError:
                continue
        if xml and "<position" in xml:
            root = ET.fromstring(xml.encode())
            for p in root.findall(".//position"):
                desc = "\n\n".join(f"{(jd.findtext('name') or '').strip()}\n{strip_html(jd.findtext('value'))}"
                                   for jd in p.findall(".//jobDescription"))
                pid = p.findtext("id") or ""
                offices = [p.findtext("office") or ""] + [o.text or "" for o in p.findall(".//additionalOffices/office")]
                out.append(Job("personio", f"{slug}-{pid}", p.findtext("name") or "", p.findtext("subcompany") or name,
                               ", ".join(filter(None, offices)), f"{base}/job/{pid}", desc,
                               to_iso(p.findtext("createdAt")), False,
                               f"{p.findtext('employmentType') or ''} {p.findtext('schedule') or ''} "
                               f"{p.findtext('seniority') or ''}"))
    elif ats == "workable":
        d = get_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}", params={"details": "true"})
        for r in d.get("jobs", []):
            out.append(Job("workable", f"{slug}-{r.get('shortcode')}", r.get("title", ""), d.get("name", name),
                           ", ".join(filter(None, [r.get("city"), r.get("country")])), r.get("url", ""),
                           strip_html(r.get("description")), to_iso(r.get("published_on") or r.get("created_at")),
                           bool(r.get("telecommuting")), r.get("employment_type", "")))
    elif ats == "workday":                 # slug = "tenant.wdN/site", e.g. a1group.wd3/karriere_at
        host, _, site = slug.partition("/")
        tenant = host.split(".")[0]
        api = f"https://{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
        for offset in range(0, 200, 20):
            d = _post_json(api + "/jobs", {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""})
            posts = d.get("jobPostings") or []
            for r in posts:
                path = r.get("externalPath") or ""
                if not path:
                    continue
                desc, posted = "", None
                try:
                    info = (get_json(api + path).get("jobPostingInfo") or {})
                    desc, posted = strip_html(info.get("jobDescription")), to_iso(info.get("startDate"))
                except Exception:
                    pass
                out.append(Job("workday", f"{tenant}-{path.rsplit('_', 1)[-1]}", r.get("title", ""), name,
                               r.get("locationsText", ""), f"https://{host}.myworkdayjobs.com/{site}{path}", desc, posted))
                time.sleep(0.8)
            if len(posts) < 20:
                break
    elif ats == "dvinci":                  # slug = the career site's host, e.g. jobs.wienerstadtwerke.at
        for r in get_json(f"https://{slug}/jobPublication/list.json"):
            o = r.get("jobOpening") or {}
            loc = "; ".join(filter(None, (f"{(l.get('address') or {}).get('zipCode') or ''} "
                                          f"{(l.get('address') or {}).get('city') or l.get('name') or ''}".strip()
                                          for l in o.get("locations") or []))) or o.get("location") or ""
            out.append(Job("dvinci", f"{slug}-{r['id']}", r.get("position", ""), (o.get("orgUnit") or {}).get("name") or name,
                           loc, r.get("jobPublicationURL", ""),
                           strip_html("\n".join(filter(None, [r.get("introduction"), r.get("tasks"), r.get("profile"), r.get("weOffer")]))),
                           to_iso(o.get("createdDate")), False,
                           " ".join(w.get("internalName", "") for w in o.get("workingTimes") or [])))
    elif ats == "recruitee":
        d = get_json(f"https://{slug}.recruitee.com/api/offers/")
        for r in d.get("offers", []):
            out.append(Job("recruitee", f"{slug}-{r['id']}", r.get("title", ""), r.get("company_name") or name,
                           ", ".join(filter(None, [r.get("city"), r.get("country")])), r.get("careers_url", ""),
                           strip_html((r.get("description") or "") + "\n" + (r.get("requirements") or "")),
                           to_iso(r.get("published_at") or r.get("created_at")), bool(r.get("remote")),
                           r.get("employment_type_code", "")))
    else:
        raise ValueError(f"unknown ats '{ats}'")
    return out


@source("companies")
def companies(cfg) -> Iterator[Job]:
    errors = []
    for c in cfg.get("companies", []):
        if not c.get("slug"):
            continue
        try:
            yield from ats_jobs(c["ats"], c["slug"], c.get("name", ""))
        except Exception as e:  # one broken board must not stop the rest
            errors.append(f"{c.get('name')}: {e}")
        time.sleep(0.5)
    if errors:
        raise RuntimeError("; ".join(errors))


def slug_candidates(name: str) -> list[str]:
    base = re.sub(r"\b(gmbh|ag|flexco|og|kg|se|inc|ltd|llc|austria|österreich)\b", "", name.lower())
    base = base.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    words = re.findall(r"[a-z0-9]+", base)
    cands = ["".join(words), "-".join(words), "_".join(words)]
    if words:
        cands += [words[0], words[0] + "gmbh", "".join(words) + "gmbh", "".join(words) + "ag"]
    return list(dict.fromkeys(c for c in cands if c))


def discover(name: str, log=print) -> list[dict]:
    """Try a company name against every supported ATS and return boards that exist."""
    found = []
    for ats in ("greenhouse", "lever", "ashby", "smartrecruiters", "personio", "workable", "recruitee"):
        for slug in slug_candidates(name):
            try:
                jobs = ats_jobs(ats, slug, name)
            except Exception:
                continue
            if jobs:
                log(f"  ✓ {ats}:{slug} — {len(jobs)} open jobs")
                found.append({"name": name, "ats": ats, "slug": slug, "count": len(jobs)})
                break
    return found


def _salary(lo, hi, cur: str = "€") -> str:
    def f(x):
        try:
            return f"{int(float(x)):,}".replace(",", ".")
        except (TypeError, ValueError):
            return ""
    lo, hi = f(lo), f(hi)
    if lo and hi and lo != hi:
        return f"{lo}–{hi} {cur}".strip()
    return f"{lo or hi} {cur}".strip() if (lo or hi) else ""
