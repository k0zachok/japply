# JobPilot — your local, free job search assistant

JobPilot collects fresh jobs in **Vienna, remote, and student/part-time roles** from many sources. It scores each
one against your CV and writes a tailored cover letter and CV for the ones you pick. Everything runs on your
own computer. Clear matches are applied to automatically; you only decide the questionable ones.

## 1. Install (5 minutes)

1. Install **Python 3.11 or newer** (python.org; on Windows tick "Add to PATH").
2. Unzip this folder, open a terminal in it and run:
   ```
   pip install -r requirements.txt
   ```
   (Optional. Without it, everything works but letters are .txt instead of .docx.)
3. **Get a free Adzuna key.** Adzuna is the biggest source for Vienna. Sign up at
   https://developer.adzuna.com, then paste `app_id` and `app_key` into `config.toml` under `[sources.adzuna]`.
4. **Pick a free AI model.** Choose one of these:
   - **Local, fully private:** install Ollama (https://ollama.com), then run `ollama pull qwen2.5:7b`.
     `config.toml` is already set up for it. You need about 8 GB of RAM.
   - **Cloud free tier (faster on a weak laptop):** get a Google Gemini API key at
     https://aistudio.google.com and use the Gemini example in the `[llm]` section.
     List several models under `models = [...]` (strongest first) to stay on the free tier: when one
     model's daily quota runs out JobPilot switches to the next. Keep the key in the `JOBPILOT_API_KEY`
     environment variable, not in the file.
   - **No AI:** set `provider = "none"`. You get keyword matching and template letters.

## 2. Use it

```
python -m jobpilot serve        # or double-click start.bat (Windows) / start.sh (Mac/Linux)
```
Your browser opens the dashboard. Click **Find new jobs**, then work through the **Inbox**:

- **Open posting** takes you to the real ad.
- **Write cover letter + CV** creates a folder in `applications/` with `cover_letter.docx`, `cv_tailored.docx`,
  the job text, and a checklist.
- Mark jobs **Applied → Interview → Offer / Rejected** to track your pipeline.
- **+ Add job** saves any job you found yourself. Paste a karriere.at, StepStone, willhaben or company link.
  For LinkedIn, paste the job text too.

Use the filters at the top to show only Vienna, Remote, Werkstudent, Internship, Thesis and so on, and to hide
jobs that need fluent German.

Command-line equivalents: `python -m jobpilot run`, `list`, `tailor <id>`, `add <url>`.

## Auto-apply

Each run sorts scored jobs into three piles (thresholds in `config.toml`, section `[apply]`):

- **Auto-apply:** the AI says "apply", score ≥ `auto_min_score`, no red flags. JobPilot writes the letter and CV,
  opens the employer's form in a hidden browser, fills it and submits.
- **Review:** in between. Open the **Review** tab and press **A** (apply for me) or **R** (reject).
- **Auto-skipped:** below `review_min_score` or the AI says "skip".

Jobs it can't finish land in **Needs you** with the reason and the documents ready: sites that need an account
(join.com, LinkedIn, Workday…), CAPTCHAs, or a form question it can't answer from your CV. Those questions appear
under **Questions**; answer once and the answer is reused on every later form. Then press "Retry auto-apply".

**Documents:** the letter and CV are written only after the form is confirmed reachable, in one AI call. Only
the CV's summary is rewritten per job; everything else stays word for word from `cv.md`. For German ads your CV
is translated once into `cv.de.md` and reused (read it once and correct it). Each job's folder in `applications/`
holds the PDFs, a screenshot of every filled form step, and `form_problem.png` if it stopped.

**Sites that need an account** (join.com and similar): run `python -m jobpilot login join.com` once, sign in in
the window that opens and close it. The session is kept in `browser_profile/` (no password is stored) and those
sites are then applied to automatically. If a site logs you out, run the command again.

**Verification e-mails:** fill `[apply.mailbox]` with the address you apply with (best: a separate one just for
applications). JobPilot reads it read-only to enter the codes or open the confirmation links sites send.
CAPTCHAs are never solved; those jobs go to Needs you.

Setup: `pip install -r requirements.txt && playwright install chromium`, fill `[apply.answers]`, and set an AI
model in `[llm]` (without one nothing is auto-applied and letters are plain templates).
`dry_run = true` (the default) fills forms and saves `form_filled.png` without submitting. Check a few, then set
`dry_run = false`. Works best on direct employer forms (Ashby, Greenhouse, Lever, Recruitee, Workable, Personio):
add companies with `python -m jobpilot discover`. Command line: `python -m jobpilot apply [JOB_ID]`.

## 3. Where the jobs come from

| Source | Covers | Key? | Notes |
|---|---|---|---|
| **Adzuna (Austria)** | Vienna, all fields | free key | Aggregates hundreds of Austrian boards and company sites; searched with your keywords within 30 km of Wien |
| **Arbeitnow** | DACH incl. Vienna + remote | no | Pulled straight from company applicant tracking systems; lots of Werkstudent roles |
| **Company career boards** | the companies you choose | no | Greenhouse, Lever, Ashby, SmartRecruiters, Personio, Workable and Recruitee public APIs: freshest data, direct from the employer |
| **Himalayas, Jobicy, RemoteOK, Remotive, We Work Remotely** | remote | no | Filtered to roles open to Europe, Austria or worldwide |
| **Your job-alert e-mails** | karriere.at, LinkedIn, StepStone, AMS "alle jobs", willhaben, jobs.at, Indeed | Gmail app password | See below |

### Add Vienna companies directly (recommended)
```
python -m jobpilot discover Bitpanda GoStudent Storyblok refurbed Adverity Anyline PlanRadar Dynatrace "MOSTLY AI" Prewave TTTech Cloudflight craftworks byrd kununu hokify Frequentis
```
JobPilot checks each name against every supported ATS and adds the boards it finds to `config.toml`. Add any
company whose careers page you like. If a name isn't found, open their careers page and copy the slug from the
URL (for example `xyz.jobs.personio.de` → `ats = "personio"`, `slug = "xyz"`).

### karriere.at, LinkedIn, StepStone, AMS
These sites have no public API and don't allow scraping, so JobPilot reads the **job-alert e-mails** they send you:
1. On each site, create job alerts (Wien + Python / Data / Werkstudent …).
2. Gmail → Google Account → Security → 2-Step Verification → **App passwords** → create one.
3. In `config.toml` under `[sources.email_alerts]`, set `enabled = true` and paste the app password.

JobPilot opens your inbox read-only, picks out the job links and adds them to the dashboard. The full job text is
fetched when you click "Write cover letter + CV".

## 4. Keep it updated automatically
**On this Mac a daily run is installed:** every day at 08:30 (or when the Mac next wakes) macOS runs
`python -m jobpilot run` and appends the output to `run.log`. Turn it off with
`launchctl bootout gui/$(id -u)/com.jobpilot.daily`; the file is `~/Library/LaunchAgents/com.jobpilot.daily.plist`.
One run uses at most 40 scoring calls and 8 applications, sized to the free Gemini quota; if the quota runs out
anyway, the remaining jobs simply wait for the next day.

Run `python -m jobpilot run` on a schedule, for example every morning:
- **Windows:** Task Scheduler → Create Basic Task → Daily → Program `python`, arguments `-m jobpilot run`,
  "Start in" = this folder.
- **Mac/Linux:** `crontab -e` → `0 8 * * * cd /path/to/jobpilot && python3 -m jobpilot run`

## 5. Tune it
Everything is in `config.toml`: keywords (German and English), job types, commute towns, excluded title words
(senior, lead …), max job age, and how many jobs the AI scores per run. After editing filters, run
`python -m jobpilot rescore`.

Your data stays in `jobpilot.db` (SQLite) and `applications/`. Nothing is uploaded anywhere, except the job text
and CV sent to the AI provider, and only if you chose a cloud model.

## Respecting the sources
JobPilot only uses official public APIs and feeds, at a gentle pace. Some sources ask for a link back or
attribution (Arbeitnow, RemoteOK, Jobicy, Himalayas, Remotive). That matters if you ever publish what you
collect; for personal use it doesn't apply. Remotive asks for no more than about 4 requests a day, and Jobicy
for only a few, so don't schedule `run` more often than a few times a day.

## Forms the script may not submit (CAPTCHA)

Some sites (e.g. Greenhouse) block an automated Submit. Those jobs land under **Needs you**. Run

    python -m jobpilot finish [JOB_ID]

A browser window opens with the form filled in; check it and click Submit yourself. The job moves to
**Applied** once the confirmation page appears. Without a JOB_ID it goes through every such job in turn.

## First-time setup from GitHub

`config.toml` and your CV are kept out of the repository (personal data). After cloning:

    cp config.example.toml config.toml     # then fill in [profile] and [apply.answers]
    # put your CV next to it as cv.md (or a PDF, see cv_file)
