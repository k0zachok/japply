# JobMan — a personal job-search and auto-apply assistant

JobMan collects job postings from job boards and company career sites, lets an AI judge how well each one fits
your CV, writes a tailored cover letter and CV summary, and fills in and submits application forms for the good
matches. Jobs it isn't sure about go to a **Review** tab where you press *Apply* or *Reject*; anything that needs
you (a CAPTCHA, a portal account, a question only you can answer) goes to **Needs you**.

It runs on your own computer. Your CV, answers and the job database stay on your machine.

## What it can do

**Find jobs**
- Pulls postings every day from karriere.at, remote job boards (Himalayas, Jobicy, RemoteOK, Remotive,
  We Work Remotely) and the career sites of the companies you list — Greenhouse, Lever, Ashby, SmartRecruiters,
  Personio, Recruitee, Workable, Workday, d.vinci and onlyfy boards.
- Finds a company's career board for you: `discover "Company Name"`.
- Keeps only jobs in places you can commute to (or remote jobs open to your region), of the types you want
  (full-time, working student, internship, thesis, …), and drops duplicates and expired postings.

**Judge them**
- An AI reads each promising job against your CV and gives a score, what you match, what you're missing, the
  German level and seniority it asks for, and the salary if stated.
- Learns from your own Apply / Reject decisions.
- Works on the free Gemini tier: several jobs per AI call and automatic rotation between models.

**Apply**
- Writes a cover letter for each job in the ad's language, and rewrites the summary of your CV for it — using only
  facts from your CV.
- Opens the application page, fills in the form (multi-step wizards, drop-downs, radio buttons, uploads), answers
  screening questions from your saved answers, and submits.
- Reads verification codes and confirmation links from a separate application mailbox.
- Can create candidate accounts on employer portals that require one (optional).
- Only counts a job as applied when a confirmation appears; otherwise it lands in *Needs you* with a screenshot and
  the reason.
- Never guesses personal facts, never solves CAPTCHAs (it fills the form and lets you click Submit), never touches
  LinkedIn.

**Keep you in the loop**
- A local dashboard: Review / Needs you / Applied / Interview / Offer / Rejected tabs, job details, one-key
  Apply (A) and Reject (R), and downloads of the tailored CV and letter as PDF or Word, named after the company.
- Questions it couldn't answer collect in one place; each answer is reused on every future form.
- Reads your inbox for replies: confirmations are noted, rejections and interview invitations update the job.
- Runs by itself every morning, then shows a notification and a health line (last run, problems, AI quota).

> It was built for a job search in Vienna, Austria. Sources, keywords and the company list in
> `config.example.toml` reflect that — change them to your city and field.

---

## 1. Set it up (about 20 minutes)

**You need:** macOS or Linux (Windows works for everything except the daily schedule), Python 3.11 or newer, Git.

```bash
git clone <this repository's URL> jobman
cd jobman
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
cp config.example.toml config.toml
cp cv.example.md cv.md
```

Then:

1. **Write your CV** in `cv.md` (Markdown, same structure as the example). Only facts from this file are ever
   used in letters and forms, so keep it complete and true.
2. **Edit `config.toml`** — at least:
   - `[profile]`: name, target roles, skills, languages, `situation` and the `availability*` / `highlight*` sentences.
   - `[search]`: keywords, places you can commute to, job types — and, if you're not looking for a tech / data / IT
     job, `relevant_titles` / `other_titles` (which job titles are worth sending to the AI).
   - `[apply.answers]`: the details application forms ask for (name, e-mail, phone, address, work permit,
     salary expectation, …). Leave a field empty if you'd rather be asked.
3. **Get a free AI key** (Google Gemini): https://aistudio.google.com/apikey → *Create API key*.
   Put it in your shell profile (`~/.zshrc` or `~/.bashrc`), never in `config.toml`:
   ```bash
   export JOBPILOT_API_KEY="your-key"
   ```
   Open a new terminal afterwards. On a Mac you can then move it into the Keychain with
   `.venv/bin/python -m jobpilot secrets` and delete the line from your shell profile.

`config.toml`, `cv.md`, the database, generated documents and browser sessions are listed in `.gitignore`, so they
never end up in Git.

## 2. First run — safely

`config.example.toml` starts with **`dry_run = true`**: forms are filled in and screenshotted, but nothing is sent.

```bash
./start.sh
```

This opens the dashboard at http://127.0.0.1:8765. Click **Find new jobs**, wait for it to finish, then look at:

- **Review** — jobs to decide on (A = apply, R = reject).
- **Needs you** — what stopped the script; each job has a note and a screenshot.
- A job's panel — the AI's view (what you match, the gaps), the salary if stated, and buttons to write and download
  the cover letter and CV as PDF or Word.

Check a few filled-in forms (the screenshots) and letters. When you trust it, set `dry_run = false` under `[apply]`.

## 3. Run it every day

On a Mac:
```bash
deploy/mac_daily.sh          # every day at 08:30; deploy/mac_daily.sh off to stop
```
On Linux, `deploy/server_setup.sh` installs everything and a daily systemd timer (made for a small Ubuntu server).

After each run you get a notification, and the dashboard shows when it last ran and whether anything went wrong.
The log is `run.log`.

## 4. Optional, but makes it apply to more jobs

| What | Why | How |
|---|---|---|
| Sign in once to sites that need an account | Applications on those sites go through on their own | `.venv/bin/python -m jobpilot login karriere.at` (a window opens; sign in; close it) |
| A separate application mailbox | Codes and confirmation links that sites e-mail are read automatically | Create a Gmail account, turn on 2-step verification, create an *App password*, set `[apply.mailbox]` and `export JOBPILOT_MAILBOX_PASSWORD="…"` |
| Password for the address you put on forms | Tracks replies (confirmations, invitations, rejections) and sends e-mail applications | Gmail app password → `export JOBPILOT_EMAIL_PASSWORD="…"`; set `[apply.email] enabled = true`. Note: an app password gives full read access to that mailbox |
| Portal sign-up | Creates candidate accounts on employer portals for you | `[apply.signup] enabled = true` and `export JOBPILOT_PORTAL_PASSWORD="…"` (a password you choose) |
| More company boards | Applying through company career sites works best | `.venv/bin/python -m jobpilot discover "Company Name"` |

## 5. Commands

```bash
.venv/bin/python -m jobpilot run              # the daily pass: inbox, fetch, score, apply
.venv/bin/python -m jobpilot serve            # the dashboard (./start.sh does the same)
.venv/bin/python -m jobpilot fetch            # only fetch new jobs
.venv/bin/python -m jobpilot score            # only score / sort them
.venv/bin/python -m jobpilot apply [JOB_ID]   # apply to queued jobs (or one)
.venv/bin/python -m jobpilot finish [JOB_ID]  # fill a form in a visible window; you click Submit (CAPTCHA sites)
.venv/bin/python -m jobpilot login SITE       # sign in to a site once
.venv/bin/python -m jobpilot inbox            # check your inbox for replies
.venv/bin/python -m jobpilot discover "Name"  # find a company's career board
.venv/bin/python -m unittest discover tests   # run the tests
```

## How it decides

- **Fetching:** karriere.at, remote job boards and company career boards (Greenhouse, Lever, Ashby, SmartRecruiters,
  Personio, Recruitee, Workable, Workday, d.vinci, onlyfy). Jobs outside your places/regions or with excluded title
  words (senior, lead, …) are filtered out.
- **Scoring:** a generous pre-filter (job title words, set in `[search]`; tech / data / IT by default) sends anything that looks like a fit to the AI, several jobs per call, so the free
  quota (about 20 calls a day per model; JobMan rotates through `[llm] models`) covers 100+ jobs a day.
- **Sorting:** score ≥ `auto_min_score` and the AI says "apply" → applied automatically; between that and
  `review_min_score` → Review; below → Auto-skipped.
- **Applying:** at most `max_per_run` jobs per run, in `parallel` browsers. Personal facts (birth date, gender,
  nationality, …) are only ever taken from your config, never guessed by the AI. A confirmation must actually appear
  on the page before a job counts as applied.
- **Never automated:** CAPTCHAs and bot checks (use `finish`), and LinkedIn (accounts get banned for automation).

## Good to know

- Read your first letters. The AI is told to use only facts from your CV, but you are the one applying.
- Respect the sites: JobMan uses public feeds and pages at a modest pace. Don't raise the limits much.
- Free AI quotas change; if scoring stops early, the rest simply waits for the next day.
