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
