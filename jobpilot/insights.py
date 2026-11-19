"""What keeps coming up as a gap in the AI's verdicts — so you can see what to learn or state better in your CV."""
from __future__ import annotations

import collections
import json
import re

# (label, pattern over the gap text, tip). Order matters only for display ties.
CATEGORIES = [
    ("German level", r"german|deutsch|\bc1\b|\bc2\b|\bb2\b",
     "A course or exam date next to your level (\"B1, B2 exam in March\") reads better than the level alone."),
    ("Years of experience / seniority", r"\d\+? ?years|years of|senior|mid-level|months of|extensive (professional )?experience|staff",
     "Describe internships and projects as experience with concrete results; years can't be added."),
    ("Location / remote region", r"located|based in|relocat|on-?site|outside|\bcountry\b|germany|\buk\b|ireland|\bus\b|united states",
     "Not a CV issue — these are jobs in places you can't work from."),
    ("Degree / field of study", r"degree|\bmsc\b|\bphd\b|bachelor|master'?s|enrolled|student status|htl",
     "Make sure your degree, field and enrolment status are clearly stated."),
    ("Cloud / DevOps", r"\baws\b|azure|\bgcp\b|google cloud|kubernetes|docker|terraform|devops|ci/cd|infrastructure as code",
     "One small deployed project or a beginner certificate (e.g. AZ-900) closes many of these."),
    ("SAP / ERP", r"\bsap\b|\berp\b|abap|s/4", "Only worth it if you have real SAP exposure to state."),
    ("Big data (Spark, dbt, Databricks…)", r"spark|databricks|\bdbt\b|airflow|kafka|snowflake|bigquery|clickhouse|lakehouse",
     "A small dbt / Spark project on GitHub is a cheap, visible fix."),
    ("BI tools (Power BI, Tableau…)", r"power ?bi|tableau|qlik|looker|\bbi tool",
     "Power BI is quick to learn; a sample dashboard or PL-300 helps."),
    ("Java / .NET / C#", r"\bjava\b|c#|\.net|kotlin|spring", "Different stack — usually not worth chasing."),
    ("JavaScript / TypeScript / frontend", r"typescript|javascript|node\.?js|react|angular|vue|frontend", ""),
    ("ML / deep learning", r"pytorch|tensorflow|deep learning|mlops|\bllm", ""),
    ("Consulting / business experience", r"consult|business analys|stakeholder|client-facing", ""),
    ("Full-time availability", r"full-?time|vollzeit|38,5|40 ?h", "Not a CV issue — the role needs more hours than you offer."),
]


def _gaps(d: dict) -> tuple[str, list[str]]:
    missing = d.get("missing") or []
    if isinstance(missing, str):
        missing = [missing]
    return (d.get("gaps") or "") + " " + " ".join(missing), [str(m).strip() for m in missing if str(m).strip()]


def gap_report(con, min_score: int = 40) -> dict:
    """Counts each gap category over AI-scored jobs with score >= min_score (the jobs you'd realistically go for)."""
    rows = con.execute("SELECT id, title, company, ai_score, ai_json FROM jobs "
                       "WHERE ai_json IS NOT NULL AND ai_score >= ? ORDER BY ai_score DESC", (min_score,)).fetchall()
    hits = {c[0]: [] for c in CATEGORIES}
    missing = collections.Counter()
    for r in rows:
        try:
            d = json.loads(r["ai_json"])
        except Exception:
            continue
        text, items = _gaps(d)
        low = text.lower()
        for label, pat, _ in CATEGORIES:
            if re.search(pat, low):
                hits[label].append({"id": r["id"], "title": r["title"], "company": r["company"], "score": r["ai_score"]})
        missing.update(re.sub(r"\s+", " ", m.lower()) for m in items)
    n = len(rows)
    cats = [{"label": label, "count": len(hits[label]), "share": round(100 * len(hits[label]) / n) if n else 0,
             "tip": tip, "jobs": hits[label][:4]} for label, _, tip in CATEGORIES if hits[label]]
    cats.sort(key=lambda c: -c["count"])
    return {"jobs": n, "min_score": min_score, "categories": cats,
            "missing": [{"item": k, "count": v} for k, v in missing.most_common(20) if v > 1]}


# ---------- salary: one short, readable line ("€3,143 gross / month") out of whatever the ad says ----------
_CUR = r"(?:€|eur(?:o)?(?![a-z]))"
_NUM = r"\d{1,3}(?:[.,' ]\d{3})+(?:[.,]\d{1,2})?|\d{4,6}(?:[.,]\d{1,2})?|\d{1,3}(?:[.,]\d)?\s?k\b|\d{1,3}(?:[.,]\d{1,2})?"
_AMOUNT = re.compile(rf"(?:{_CUR}\s?(?P<a>{_NUM}))|(?:(?P<b>{_NUM})\s?(?:,-|\.-|-)?\s?{_CUR})", re.I)
_PERIOD = [("month", r"monat|month|mtl|/\s?m\b|p\.?\s?m\b"), ("year", r"jahr|year|annual|jährl|p\.?\s?a\b|/\s?a\b"),
           ("hour", r"pro stunde|per hour|hourly|stundenlohn|/\s?(?:h|std)\b")]
_RANGE_SEP = r"\s*(?:-|–|—|bis|to)\s*"


def _value(s: str) -> float | None:
    s = s.strip().lower().replace(" ", "").replace("'", "")
    if s.endswith("k"):
        return float(s[:-1].replace(",", ".")) * 1000
    s = re.sub(r"[.,]\d{1,2}$", "", s) if re.search(r"[.,]\d{3}", s) or len(re.sub(r"\D", "", s)) > 4 else s
    s = re.sub(r"[.,]", "", s) if re.search(r"[.,]\d{3}$", s) or re.search(r"[.,]\d{3}[.,]", s) else re.sub(r"[.,]\d{1,2}$", "", s)
    try:
        return float(s)
    except ValueError:
        return None


def salary_line(text: str) -> str:
    """'EUR 2.064,00 brutto pro Monat' -> '€2,064 gross / month'; '' if no plausible salary is stated."""
    if not text:
        return ""
    for m in _AMOUNT.finditer(text):
        v = _value(m.group("a") or m.group("b"))
        after = text[m.end(): m.end() + 90].lower()
        before = text[max(0, m.start() - 70): m.start()].lower()
        explicit = next((p for p, pat in _PERIOD if re.search(pat, after[:45])), None) \
            or next((p for p, pat in _PERIOD if re.search(pat, before[-45:])), None)
        if not v or v > 400000 or v < (8 if explicit == "hour" else 300):     # not a salary (dates, counts, small fees)
            continue
        lo = hi = None
        nxt = _AMOUNT.search(text, m.end(), m.end() + 25)                       # "€3,000 – €3,800"
        if nxt and re.fullmatch(_RANGE_SEP, text[m.end():nxt.start()]):
            hi = _value(nxt.group("a") or nxt.group("b"))
        prev = re.search(rf"({_NUM}){_RANGE_SEP}$", text[max(0, m.start() - 30): m.start()])   # "3.000 bis 3.800 €", "40-55k EUR"
        if prev and not hi:
            lo, hi = _value(prev.group(1)), v
            if lo and hi and lo < 1000 <= hi and "k" in (m.group(0) or "").lower():
                lo *= 1000
            v = lo or v
        around = before + after
        period = explicit or next((p for p, pat in _PERIOD[:2] if re.search(pat, around)), None) \
            or ("hour" if v < 100 else "month" if v < 12000 else "year")
        gross = "gross " if re.search(r"brutto|gross", around) else "net " if re.search(r"netto|\bnet\b", around) else ""
        word = "from " if re.search(r"(?:\bab|\bfrom|mindest|minimum|\bmin\b|at least|starts? at|starting)[^.\n€]{0,30}$", before) \
            else "up to " if re.search(r"(?:bis zu|up to|max(?:imal|imum)?)\W{0,10}$", before) else ""
        f = (lambda x: f"€{x:,.2f}".rstrip("0").rstrip(".")) if period == "hour" else (lambda x: f"€{x:,.0f}")
        amount = f"{f(v)} – {f(hi)}" if hi and hi > v else f"{word}{f(v)}"
        return f"{amount} {gross}/ {period}"
    return ""


def salary_for(job: dict, ai: dict | None = None) -> str:
    """Best short salary line for a job: the board's own field, the AI's reading, or the ad text."""
    for cand in (job.get("salary"), (ai or {}).get("salary")):
        if not isinstance(cand, str):
            continue
        cand = (cand or "").strip()
        if not cand or re.match(r"(0|none|n/?a|not (stated|specified|mentioned)|unknown|-)\b", cand, re.I):
            continue
        line = salary_line(cand)
        if line:
            return line
        if len(cand) <= 40 and re.search(r"\d", cand):
            return cand
    return salary_line(job.get("description") or "")
