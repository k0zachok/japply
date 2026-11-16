"""Classify jobs (type, location) and score them against the CV — without and with AI."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

TYPE_PATTERNS = {
    "working_student": r"werkstudent|working[\s-]?student|studentische|student job|studentenjob|student assistant|"
                       r"studentjob|student\s*\(|für studierende|for students|geringfügig",
    "internship": r"praktik|\bintern\b|internship|\binterns\b|pflichtpraktikum|summer intern",
    "thesis": r"masterarbeit|master'?s thesis|\bthesis\b|abschlussarbeit|diplomarbeit|bachelorarbeit",
    "trainee": r"\btrainee|graduate program|absolvent",
    "part_time": r"teilzeit|part[\s_-]?time|geringfügig|\b(1[0-9]|2[0-9]|[5-9])\s?(h|std\.?|stunden|hours)\b",
    "freelance": r"freelanc|freiberuf|werkvertrag|contractor|\bcontract\b|befristet.*projekt",
    "full_time": r"vollzeit|full[\s_-]?time|38[,.]5|40\s?(h|std|stunden|hours)",
}
GERMAN_FLUENT = re.compile(
    r"(fließend|verhandlungssicher|sehr gut|ausgezeichnet|perfekt|exzellent)\w*\s+(\w+\s+){0,3}deutsch|"
    r"deutsch\w*\s*(\(|:|-)?\s*(c1|c2|muttersprach|fließend|verhandlungssicher)|"
    r"(fluent|native|excellent|business[- ]level|proficient)\s+(\w+\s+){0,2}german|german\s*\(?(c1|c2)|"
    r"german\s+(is\s+)?(a\s+)?must|deutschkenntnisse auf c1", re.I)
YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:-\s*\d+\s*)?(?:jahre|years|yrs|jahren)", re.I)
VIENNA_PLZ = re.compile(r"\b1(0[1-9]|1\d|2[0-3])0\b")
REMOTE_WORDS = re.compile(r"remote|home[\s-]?office|work from home|wfh|telearbeit|anywhere|ortsunabhängig", re.I)
BAD_REGIONS = re.compile(
    r"\b(us|usa|u\.s\.|united states|canada|latam|latin america|brazil|india|philippines|apac|"
    r"australia|uk only|united kingdom only|north america|americas|mexico|argentina|colombia|nigeria|"
    r"south africa|pakistan|singapore|japan)\b", re.I)


def classify_types(title: str, hint: str, desc: str) -> list[str]:
    head = f"{title} {hint}".lower()
    body = (desc or "")[:3000].lower()
    types = [t for t, p in TYPE_PATTERNS.items() if re.search(p, head)]
    # description only decides when the title/hint is silent
    if not types:
        types = [t for t in ("working_student", "internship", "part_time", "freelance")
                 if re.search(TYPE_PATTERNS[t], body)]
    if "working_student" in types and "part_time" not in types:
        types.append("part_time")
    if not types or types == ["part_time"] and re.search(TYPE_PATTERNS["full_time"], body):
        types.append("full_time")
    return sorted(set(types))


def where_ok(cfg: dict, location: str, remote: bool, desc: str) -> str | None:
    """'vienna', 'remote', 'hybrid' or None (not reachable)."""
    loc = (location or "").lower()
    s = cfg["search"]
    if VIENNA_PLZ.search(loc) or any(c in loc for c in s["commute_locations"]):
        return "hybrid" if (remote or REMOTE_WORDS.search(loc)) else "vienna"
    if not s.get("include_remote", True):
        return None
    is_remote = remote or bool(REMOTE_WORDS.search(loc))
    if not is_remote:
        # "Austria" / empty location with remote hints in description
        if re.search(r"österreich|austria|\bat\b", loc) and REMOTE_WORDS.search((desc or "")[:4000]):
            return "remote"
        return None
    region = re.sub(r"remote\s*[—:-]?", "", loc).strip(" ,()-—")
    if not region or any(r in region for r in s["remote_regions_ok"]):
        return "remote"
    if BAD_REGIONS.search(region):
        return None
    return "remote"  # unknown region — keep it, the AI/you can judge


def flags_for(cfg: dict, title: str, desc: str) -> list[str]:
    f = []
    text = f"{title}\n{desc or ''}"
    if cfg["profile"].get("german_level", "B1").upper() not in ("C1", "C2") and GERMAN_FLUENT.search(text):
        f.append("fluent German")
    years = [int(m.group(1)) for m in YEARS.finditer(text) if int(m.group(1)) < 20]
    if years and min(years) > cfg["profile"].get("max_years_required", 2):
        f.append(f"{min(years)}+ yrs exp")
    if not desc or len(desc) < 200:
        f.append("short description")
    return f


def keyword_score(cfg: dict, title: str, desc: str, flags: list[str]) -> int:
    p = cfg["profile"]
    t, d = title.lower(), (desc or "").lower()
    score = 0
    if any(r in t for r in p["target_roles"]):
        score += 35
    hits_title = [s for s in p["skills"] if re.search(rf"(?<![a-z]){re.escape(s)}(?![a-z])", t)]
    hits_desc = [s for s in p["skills"] if re.search(rf"(?<![a-z]){re.escape(s)}(?![a-z])", d)]
    score += 8 * len(hits_title) + min(45, 4 * len(set(hits_desc) - set(hits_title)))
    if re.search(r"junior|einsteiger|entry|graduate|absolvent|werkstudent|working student|intern|praktik|trainee", t):
        score += 10
    if "fluent German" in flags:
        score -= 20
    if any("yrs exp" in x for x in flags):
        score -= 15
    return max(0, min(100, score))


def excluded(cfg: dict, title: str) -> bool:
    t = title.lower()
    return any(w in t for w in cfg["search"].get("exclude_title_words", []))


def too_old(cfg: dict, posted_at: str | None) -> bool:
    if not posted_at:
        return False
    try:
        d = datetime.fromisoformat(posted_at)
    except ValueError:
        return False
    return d < datetime.now(timezone.utc) - timedelta(days=int(cfg["search"].get("max_age_days", 30)))


def classify_all(cfg: dict, con, only_new: bool = True) -> dict:
    """Fill types / where_ok / flags / kw_score. Hides jobs that don't fit your filters."""
    q = "SELECT * FROM jobs" + (" WHERE kw_score IS NULL" if only_new else "")
    wanted = set(cfg["search"]["job_types"])
    stats = {"kept": 0, "hidden": 0}
    for r in con.execute(q).fetchall():
        types = classify_types(r["title"], r["employment_hint"] or "", r["description"] or "")
        w = where_ok(cfg, r["location"], bool(r["remote"]), r["description"] or "")
        fl = flags_for(cfg, r["title"], r["description"] or "")
        kw = keyword_score(cfg, r["title"], r["description"] or "", fl)
        status = r["status"]
        if status == "new" and (w is None or excluded(cfg, r["title"]) or not (set(types) & wanted)
                                or too_old(cfg, r["posted_at"])):
            status = "filtered"
        stats["hidden" if status == "filtered" else "kept"] += 1
        con.execute("UPDATE jobs SET types=?, where_ok=?, flags=?, kw_score=?, status=? WHERE id=?",
                    (",".join(types), w or "", ",".join(fl), kw, status, r["id"]))
    con.commit()
    return stats


# ------------------------------------------------------------------ AI scoring
SCORE_PROMPT = """You are a strict, honest career advisor in Vienna, Austria. Rate how well this candidate fits this job.

CANDIDATE CV:
{cv}

CANDIDATE PREFERENCES: wants {roles}. German level {german}, English {english}. Currently enrolled in a part-time
MSc (so eligible for working-student / Werkstudent roles). Lives in Vienna.

{taste}JOB:
Title: {title}
Company: {company}
Location: {location}
Description:
{desc}

Return ONLY a JSON object:
{{"score": <0-100 realistic chance this candidate gets an interview>,
  "verdict": "apply" | "maybe" | "skip",
  "why": "<max 2 sentences: strongest match points>",
  "gaps": "<max 2 sentences: missing requirements, or empty>",
  "german_required": "none" | "basic" | "good" | "fluent",
  "language": "de" | "en",
  "seniority": "student" | "junior" | "mid" | "senior"}}
Be realistic: senior roles, fluent-German requirements or 3+ years experience mean a low score."""


def ai_score_all(cfg: dict, con, llm, log=print) -> int:
    from .core import load_cv_text
    cv = load_cv_text(cfg)
    lc = cfg["llm"]
    rows = con.execute(
        "SELECT * FROM jobs WHERE status IN ('new','saved') AND ai_score IS NULL AND (kw_score >= ? "
        # commutable student / entry-level jobs are rare, so they get an AI look even on a weak keyword score
        "OR (where_ok != 'remote' AND kw_score >= 8 AND (types LIKE '%intern%' OR types LIKE '%working_student%' "
        "OR types LIKE '%trainee%' OR types LIKE '%thesis%'))) "
        "ORDER BY kw_score DESC LIMIT ?", (lc.get("min_keyword_score", 20), lc.get("score_top_n", 60))).fetchall()
    # what you approved / rejected so far teaches the model your taste
    liked = [f"{x['title']} — {x['company']}" for x in con.execute(
        "SELECT title, company FROM jobs WHERE status IN ('applied','interview','offer','queued','manual','saved') "
        "ORDER BY last_seen DESC LIMIT 12")]
    disliked = [f"{x['title']} — {x['company']}" for x in con.execute(
        "SELECT title, company FROM jobs WHERE status='hidden' ORDER BY last_seen DESC LIMIT 12")]
    taste = ""
    if liked or disliked:
        taste = ("THE CANDIDATE'S OWN RECENT DECISIONS (weigh similar jobs accordingly):\n"
                 + ("Chose to apply: " + "; ".join(liked) + "\n" if liked else "")
                 + ("Rejected: " + "; ".join(disliked) + "\n" if disliked else "") + "\n")
    n = 0
    for i, r in enumerate(rows, 1):
        prompt = SCORE_PROMPT.format(taste=taste, 
            cv=cv[:6000], roles=", ".join(cfg["profile"]["target_roles"][:10]),
            german=cfg["profile"].get("german_level"), english=cfg["profile"].get("english_level"),
            title=r["title"], company=r["company"], location=r["location"], desc=(r["description"] or "")[:6000])
        try:
            res = llm.json(prompt)
            score = int(max(0, min(100, float(res.get("score", 0)))))
            res["model"] = llm.last_model
            # Cheap models score lower than strong ones, so a promising job gets a second opinion from the
            # strongest model that still has quota; its verdict is the one that counts.
            if len(llm.models) > 1 and score >= int(cfg.get("apply", {}).get("review_min_score", 45)) - 10 \
                    and res.get("verdict") != "skip":
                try:
                    res2 = llm.json(prompt, best=True)
                    if llm.last_model != res["model"]:
                        res2["model"], res2["first_opinion"] = llm.last_model, f"{res['model']}: {score}"
                        res, score = res2, int(max(0, min(100, float(res2.get("score", 0)))))
                except Exception:
                    pass
        except Exception as e:
            if getattr(e, "url", "") == "llm":      # provider quota / every model overloaded: stop, the rest stay in the Inbox for next run
                log(f"  ⚠ AI quota reached after {n} jobs — {len(rows) - i + 1} wait in the Inbox for the next run")
                break
            log(f"  AI scoring failed for {r['title'][:50]}: {e}")
            continue
        con.execute("UPDATE jobs SET ai_score=?, ai_json=? WHERE id=?", (score, json.dumps(res, ensure_ascii=False), r["id"]))
        con.commit()
        n += 1
        log(f"  [{i}/{len(rows)}] {score:3d}  {r['title'][:60]} — {r['company'][:30]}")
    return n


# ------------------------------------------------------------------ triage
def triage(cfg: dict, con, ai_on: bool) -> dict:
    """Sort scored Inbox jobs into: queued (apply automatically), review (you decide), skipped.

    Auto-apply needs an AI verdict of "apply", a high score and no red flags. Without an AI model nothing is
    ever auto-applied: good keyword matches go to Review instead.
    """
    a = cfg.get("apply", {})
    hi, lo = int(a.get("auto_min_score", 75)), int(a.get("review_min_score", 45))
    stats = {"queued": 0, "review": 0, "skipped": 0}
    for r in con.execute("SELECT id, kw_score, ai_score, ai_json, flags FROM jobs "
                         "WHERE status='new' AND kw_score IS NOT NULL").fetchall():
        red = [f for f in (r["flags"] or "").split(",") if f and f != "short description"]
        if r["ai_score"] is not None:
            ai = json.loads(r["ai_json"] or "{}")
            verdict, s = ai.get("verdict", "maybe"), r["ai_score"]
            if ai.get("german_required") == "fluent" and cfg["profile"].get("german_level", "B1").upper() < "C1":
                red.append("fluent German")
            if s < lo:               # the score decides; a "skip" verdict above the bar still goes to Review
                st = "skipped"
            elif verdict == "apply" and s >= hi and not red:
                st = "queued"
            else:
                st = "review"
        elif ai_on:
            continue                      # waits in the Inbox until the AI has scored it
        else:
            st = "review" if r["kw_score"] >= lo else "skipped"
        if st == "queued" and not a.get("enabled", True):
            st = "review"
        stats[st] += 1
        con.execute("UPDATE jobs SET status=? WHERE id=?", (st, r["id"]))
    con.commit()
    return stats
