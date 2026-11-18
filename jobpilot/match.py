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
        if status == "filtered" and w is None:       # wrong place: the text will never be needed (keeps the DB small)
            con.execute("UPDATE jobs SET description='' WHERE id=?", (r["id"],))
    con.commit()
    return stats


# ------------------------------------------------------------------ AI scoring
SCORE_PROMPT = """You are a strict, honest career advisor in {city}. Rate how well this candidate fits this job.

CANDIDATE PROFILE:
{cv}

CANDIDATE PREFERENCES: wants {roles}. German level {german}, English {english}. {situation}

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
  "matched": [<up to 5 key requirements of the job the candidate meets, 2-6 words each>],
  "missing": [<up to 5 key requirements the candidate does NOT meet, 2-6 words each>],
  "salary": "<salary or pay stated in the ad, e.g. 'from EUR 3,200 gross/month', else empty>",
  "german_required": "none" | "basic" | "good" | "fluent",
  "language": "de" | "en",
  "seniority": "student" | "junior" | "mid" | "senior"}}
Be realistic: senior roles, fluent-German requirements or 3+ years experience mean a low score."""


PROFILE_PROMPT = """Condense this CV into a factual profile of at most 140 words for matching against job ads.
Keep: degrees and current studies (with dates), total professional experience and what it was, the concrete
technologies used, notable projects in a few words, languages with levels, location. No adjectives, no claims that
are not in the CV. Plain text, no headings.

{cv}"""


def scoring_profile(cfg: dict, llm, log=print) -> str:
    """A short profile used in place of the full CV when scoring (about a quarter of the tokens per job).
    Written once by the AI and cached in cv_profile.txt until the CV changes."""
    import hashlib
    from .core import load_cv_text, resolve
    cv = load_cv_text(cfg)
    f, h = resolve(cfg, "cv_profile.txt"), hashlib.sha1(cv.encode()).hexdigest()[:12]
    if f.exists():
        head, _, body = f.read_text(encoding="utf-8").partition("\n")
        if head.strip() == h and len(body.split()) >= 40:
            return body.strip()
    try:
        body = llm.chat(PROFILE_PROMPT.format(cv=cv[:6000]), max_tokens=400).strip()
        if 40 <= len(body.split()) <= 220:
            f.write_text(h + "\n" + body, encoding="utf-8")
            log("  wrote a short profile for scoring (cv_profile.txt)")
            return body
    except Exception:
        pass
    return cv[:6000]


# Pre-filter for the AI. Deliberately generous: anything that looks like a tech / data / IT role, or any student
# position you can commute to, goes to the AI. Only titles that are clearly another profession are left out.
RELEVANT = re.compile(
    r"software|entwickl|develop|programm|engineer|ingenieur|data|daten|analy[st]|statisti|\bit\b|it-|informatik|"
    r"devops|cloud|platform|plattform|backend|frontend|full.?stack|web|python|java|\bsql\b|\bai\b|\bki\b|"
    r"künstliche|machine learning|\bml\b|mlops|llm|nlp|automati|digital|tech|system|test|qa\b|quality|\bbi\b|"
    r"business intelligence|reporting|insight|research|scien|forsch|algorithm|security|cyber|infrastruct|"
    r"integration|schnittstell|solution|product|produkt|erp|odoo|sap\b|crm|consult|berat|"
    r"mathemati|aktuar|actuar|quant|fintech|operations research", re.I)
OTHER_JOB = re.compile(
    r"verkäuf|verkauf|sales|account executive|außendienst|filial|kassier|lager|logistik(?!.*data)|fahrer|chauffeur|"
    r"koch|küche|service ?mitarbeit|reinigung|pflege|nurse|medical|arzt|ärzt|therap|apothek|pharma ?referent|"
    r"payroll|personalverrechn|lohn|buchhalt|bilanz|steuer|tax|recht|legal|jurist|anwalt|notar|"
    r"recruit|hr |hr-|human resources|personal(?!is)|marketing manager|media planner|grafik|design(?!.*(system|data))|"
    r"assistent|sekretär|office manag|empfang|front office|rezeption|hausmeister|elektriker|mechani|monteur|"
    r"techniker(?!.*(it|software|daten))|bauleit|polier|maurer|tischler|installateur|totenabhol|bestatt", re.I)
STUDENT = ("intern", "working_student", "trainee", "thesis")


def worth_scoring(r) -> bool:
    """Should this job go to the AI? Generous on purpose — see RELEVANT / OTHER_JOB above."""
    title, kw = r["title"] or "", r["kw_score"] or 0
    student = any(t in (r["types"] or "") for t in STUDENT) and r["where_ok"] != "remote"
    if kw >= 20:
        return True
    if RELEVANT.search(title):
        return True
    if student and not OTHER_JOB.search(title):
        return True
    return False


BATCH_PROMPT = """You are a strict, honest career advisor in {city}. Rate how well this candidate fits
EACH of the jobs below. Judge every job on its own.

CANDIDATE PROFILE:
{cv}

CANDIDATE PREFERENCES: wants {roles}. German level {german}, English {english}. {situation}

{taste}JOBS:
{jobs}

Return ONLY a JSON object {{"results": [...]}} with one entry per job, in the same order, each:
{{"id": "<the job's id>", "score": <0-100 realistic chance this candidate gets an interview>,
  "verdict": "apply" | "maybe" | "skip", "why": "<max 2 sentences: strongest match points>",
  "gaps": "<max 2 sentences: missing requirements, or empty>",
  "matched": [<up to 5 key requirements the candidate meets, 2-6 words each>],
  "missing": [<up to 5 key requirements the candidate does NOT meet, 2-6 words each>],
  "salary": "<salary stated in the ad, else empty>",
  "german_required": "none" | "basic" | "good" | "fluent", "language": "de" | "en",
  "seniority": "student" | "junior" | "mid" | "senior"}}
Be realistic: senior roles, fluent-German requirements or 3+ years experience mean a low score."""


def _situation(cfg: dict) -> dict:
    p = cfg["profile"]
    return {"situation": p.get("situation") or "", "city": p.get("city") or cfg.get("apply", {}).get("answers", {}).get("city") or "Austria"}


def _taste(con) -> str:
    """What you approved / rejected so far teaches the model your taste."""
    liked = [f"{x['title']} — {x['company']}" for x in con.execute(
        "SELECT title, company FROM jobs WHERE status IN ('applied','interview','offer','queued','manual','saved') "
        "ORDER BY last_seen DESC LIMIT 12")]
    disliked = [f"{x['title']} — {x['company']}" for x in con.execute(
        "SELECT title, company FROM jobs WHERE status='hidden' ORDER BY last_seen DESC LIMIT 12")]
    if not (liked or disliked):
        return ""
    return ("THE CANDIDATE'S OWN RECENT DECISIONS (weigh similar jobs accordingly):\n"
            + ("Chose to apply: " + "; ".join(liked) + "\n" if liked else "")
            + ("Rejected: " + "; ".join(disliked) + "\n" if disliked else "") + "\n")


def ai_score_all(cfg: dict, con, llm, log=print) -> int:
    """Score the Inbox with the AI, several jobs per call (score_batch, default 6) so the free quota goes far.
    Jobs that look promising get a second opinion from the strongest model, one by one."""
    from .pipeline import ensure_description
    lc = cfg["llm"]
    batch, budget = max(1, int(lc.get("score_batch", 6))), int(lc.get("score_top_n", 40))   # budget = AI calls per run
    cands = [r for r in con.execute(
        "SELECT * FROM jobs WHERE status IN ('new','saved') AND ai_score IS NULL "
        "ORDER BY (where_ok IN ('vienna','hybrid')) DESC, kw_score DESC, first_seen DESC").fetchall() if worth_scoring(r)]
    if not cands:
        return 0
    cv = scoring_profile(cfg, llm, log)
    taste = _taste(con)
    # a second opinion only where it decides something: jobs close to the auto-apply bar
    second_min = int(cfg.get("apply", {}).get("auto_min_score", 75)) - 15
    n = calls = 0
    for k in range(0, len(cands), batch):
        if calls >= budget:
            log(f"  {len(cands) - k} job(s) wait for the next run (AI calls per run: {budget})")
            break
        chunk = []
        for r in cands[k:k + batch]:
            job = dict(r)
            if len(job.get("description") or "") < 300:      # text missing in the feed: read the job page once
                job = ensure_description(cfg, con, job, log=lambda *_: None)
            chunk.append(job)
        jobs_txt = "\n\n".join(
            f"--- id: {j['id']}\nTitle: {j['title']}\nCompany: {j['company']}\nLocation: {j['location']}\n"
            f"Description:\n{(j.get('description') or '(no description available)')[:1800]}" for j in chunk)
        prompt = BATCH_PROMPT.format(taste=taste, cv=cv[:3000], roles=", ".join(cfg["profile"]["target_roles"][:10]),
                                     german=cfg["profile"].get("german_level"), english=cfg["profile"].get("english_level"), **_situation(cfg),
                                     jobs=jobs_txt)
        try:
            res = llm.json(prompt, max_tokens=450 * len(chunk))
            calls += 1
        except Exception as e:
            if getattr(e, "url", "") == "llm":
                log(f"  ⚠ AI quota reached after {n} jobs — the rest wait in the Inbox for the next run")
                break
            log(f"  AI scoring failed for a batch: {str(e)[:120]}")
            calls += 1
            continue
        by_id = {str(x.get("id")): x for x in (res.get("results") or []) if isinstance(x, dict)}
        for j in chunk:
            r = by_id.get(j["id"])
            if not r:
                continue                                   # left out by the model: stays in the Inbox, retried next run
            try:
                score = int(max(0, min(100, float(r.get("score", 0)))))
            except Exception:
                continue
            r["model"] = llm.last_model
            if len(llm.models) > 1 and score >= second_min and r.get("verdict") != "skip" and calls < budget:
                single = SCORE_PROMPT.format(taste=taste, cv=cv[:3000], roles=", ".join(cfg["profile"]["target_roles"][:10]),
                                             german=cfg["profile"].get("german_level"),
                                             english=cfg["profile"].get("english_level"), **_situation(cfg), title=j["title"],
                                             company=j["company"], location=j["location"],
                                             desc=(j.get("description") or "")[:4500])
                try:
                    r2 = llm.json(single, best=True, max_tokens=900)
                    calls += 1
                    if llm.last_model != r["model"]:
                        r2["model"], r2["first_opinion"] = llm.last_model, f"{r['model']}: {score}"
                        r, score = r2, int(max(0, min(100, float(r2.get("score", 0)))))
                except Exception:
                    pass
            r.pop("id", None)
            con.execute("UPDATE jobs SET ai_score=?, ai_json=? WHERE id=?", (score, json.dumps(r, ensure_ascii=False), j["id"]))
            n += 1
            log(f"  {score:3d}  {j['title'][:60]} — {(j['company'] or '')[:30]}")
        con.commit()
    log(f"  AI-scored {n} jobs with {calls} AI call(s)")
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
    # The same posting often arrives twice under different company spellings ("ADMIRAL" / "ADMIRAL Technologies").
    # Never queue or show one that was already applied to, is waiting on you, or that you rejected.
    dup = con.execute(
        "SELECT a.id FROM jobs a JOIN jobs b ON lower(a.title) = lower(b.title) AND a.id != b.id "
        "AND (lower(a.company) LIKE '%' || lower(substr(b.company, 1, 6)) || '%' "
        "     OR lower(b.company) LIKE '%' || lower(substr(a.company, 1, 6)) || '%') "
        "WHERE a.status IN ('queued', 'review') AND length(b.company) >= 4 AND length(a.company) >= 4 "
        "AND (b.status IN ('applied', 'interview', 'offer', 'rejected', 'hidden', 'manual') "
        "     OR (b.status IN ('queued', 'review') AND b.id < a.id))").fetchall()
    for r in dup:
        con.execute("UPDATE jobs SET status='skipped', apply_note='same job as one already handled (duplicate listing)' "
                    "WHERE id=?", (r["id"],))
    stats["duplicates"] = len(dup)
    con.commit()
    return stats
