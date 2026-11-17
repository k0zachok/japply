"""JobMan command line.

  python -m jobpilot run                 fetch new jobs from all sources and score them
  python -m jobpilot serve               open the dashboard (also has a "Find new jobs" button)
  python -m jobpilot list [--min 50]     print the best matches
  python -m jobpilot tailor JOB_ID       write cover letter + tailored CV for one job
  python -m jobpilot add URL [--text file.txt] [--title ..] [--company ..]
  python -m jobpilot discover "Company"  find a company's career board and add it to config.toml
  python -m jobpilot fetch [--only adzuna,arbeitnow]   fetch without scoring
  python -m jobpilot login SITE          sign in to a site once (e.g. join.com); the session is reused when applying
  python -m jobpilot apply [JOB_ID]      auto-apply to every queued job (or just one)
  python -m jobpilot finish [JOB_ID]     fill the form in a visible window; you check it and click Submit
  python -m jobpilot rescore             re-apply filters/keyword scores to every job (after editing config)
"""
from __future__ import annotations

import argparse
import sys

from . import pipeline
from .core import connect, load_config
from .llm import LLM


def main(argv=None):
    ap = argparse.ArgumentParser(prog="jobpilot", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    s = sub.add_parser("serve"); s.add_argument("--no-browser", action="store_true")
    s = sub.add_parser("fetch"); s.add_argument("--only", default="")
    sub.add_parser("score")
    sub.add_parser("rescore")
    s = sub.add_parser("apply"); s.add_argument("job_id", nargs="?")
    s = sub.add_parser("finish"); s.add_argument("job_id", nargs="?")
    s = sub.add_parser("login"); s.add_argument("site")
    s = sub.add_parser("list"); s.add_argument("--min", type=int, default=40); s.add_argument("--limit", type=int, default=30)
    s = sub.add_parser("tailor"); s.add_argument("job_id")
    s = sub.add_parser("add"); s.add_argument("url", nargs="?", default="")
    s.add_argument("--text"); s.add_argument("--title", default=""); s.add_argument("--company", default="")
    s = sub.add_parser("discover"); s.add_argument("names", nargs="+")
    s = sub.add_parser("status"); s.add_argument("job_id"); s.add_argument("value")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)

    if a.cmd == "run":
        pipeline.run(cfg)
        _print_top(cfg, 40, 15)
    elif a.cmd == "serve":
        from .server import serve
        serve(cfg, open_browser=not a.no_browser)
    elif a.cmd == "fetch":
        pipeline.fetch(cfg, connect(cfg), only=[x for x in a.only.split(",") if x] or None)
    elif a.cmd == "score":
        pipeline.score(cfg, connect(cfg))
    elif a.cmd == "rescore":
        con = connect(cfg)
        con.execute("UPDATE jobs SET status='new' WHERE status IN ('filtered','review','skipped')")
        con.execute("UPDATE jobs SET kw_score=NULL")
        con.commit()
        pipeline.score(cfg, con)
    elif a.cmd == "login":
        from .apply import login
        login(cfg, a.site)
    elif a.cmd in ("apply", "finish"):
        from .apply import apply_queued
        con = connect(cfg)
        if a.job_id:
            con.execute("UPDATE jobs SET status='queued' WHERE id=?", (a.job_id,))
            con.commit()
        apply_queued(cfg, con, ids=[a.job_id] if a.job_id else None, hand=a.cmd == "finish")
    elif a.cmd == "list":
        _print_top(cfg, a.min, a.limit)
    elif a.cmd == "tailor":
        from .tailor import tailor
        con = connect(cfg)
        job = pipeline.ensure_description(cfg, con, pipeline.job_row(con, a.job_id))
        folder = tailor(cfg, job, LLM(cfg))
        con.execute("UPDATE jobs SET app_dir=? WHERE id=?", (str(folder), a.job_id))
        con.commit()
        print(f"Written to {folder}")
    elif a.cmd == "add":
        text = open(a.text, encoding="utf-8").read() if a.text else ""
        if not (a.url or text):
            sys.exit("give a URL and/or --text file")
        jid = pipeline.add_url(cfg, connect(cfg), a.url, text, a.title, a.company)
        print(f"Added {jid}. Write the application with:  python -m jobpilot tailor '{jid}'")
    elif a.cmd == "discover":
        from .sources import discover
        added = []
        for name in a.names:
            print(f"{name}:")
            hits = discover(name)
            if not hits:
                print("  no public board found (try the exact slug from their careers page URL)")
            added += hits[:1]
        if added:
            with open(cfg["_path"], "a", encoding="utf-8") as f:
                for h in added:
                    f.write(f'\n[[companies]]\nname = "{h["name"]}"\nats = "{h["ats"]}"\nslug = "{h["slug"]}"\n')
            print(f"Added {len(added)} board(s) to config.toml")
    elif a.cmd == "status":
        con = connect(cfg)
        con.execute("UPDATE jobs SET status=? WHERE id=?", (a.value, a.job_id))
        con.commit()


def _print_top(cfg, min_score, limit):
    con = connect(cfg)
    rows = con.execute(
        "SELECT id, title, company, location, where_ok, types, flags, COALESCE(ai_score, kw_score) s "
        "FROM jobs WHERE status IN ('new','saved') AND COALESCE(ai_score, kw_score) >= ? "
        "ORDER BY s DESC LIMIT ?", (min_score, limit)).fetchall()
    print(f"\nTop matches (score ≥ {min_score}):")
    for r in rows:
        fl = f"  ⚠ {r['flags']}" if r["flags"] and r["flags"] != "short description" else ""
        print(f"  {r['s']:3d}  {r['title'][:55]:55s}  {r['company'][:22]:22s}  {r['where_ok']:7s} {r['types']}{fl}")
        print(f"       id: {r['id']}")
    if not rows:
        print("  (none yet)")


if __name__ == "__main__":
    main()
