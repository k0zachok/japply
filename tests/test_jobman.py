"""Fast checks of the logic that decides things (no network, no browser).  Run:  python -m unittest discover tests"""
import json
import tempfile
import unittest
from pathlib import Path

from jobpilot import apply, match, replies, server, sources
from jobpilot.core import connect

CFG = {
    "profile": {"name": "Test Person", "target_roles": ["python developer", "data engineer"],
                "skills": ["python", "sql", "docker"], "german_level": "B1", "english_level": "C1", "max_years_required": 2},
    "search": {"keywords": ["python"], "commute_locations": ["wien", "vienna"], "include_remote": True,
               "remote_regions_ok": ["europe", "austria", "worldwide"], "job_types": ["full_time", "internship"],
               "max_age_days": 30, "exclude_title_words": ["senior", "lead"]},
    "apply": {"enabled": True, "auto_min_score": 75, "review_min_score": 30,
              "answers": {"first_name": "Test", "last_name": "Person", "email": "t@example.com", "birth_date": "2001-02-03",
                          "salutation": "Herr", "country": "Austria"}},
    "llm": {}, "sources": {}, "app": {"db_file": "x.db", "applications_dir": "applications"},
}


def db():
    tmp = tempfile.mkdtemp()
    cfg = json.loads(json.dumps(CFG))
    cfg["app"]["db_file"] = str(Path(tmp) / "t.db")
    cfg["_dir"] = tmp
    return cfg, connect(cfg)


class Options(unittest.TestCase):
    def test_exact_and_partial(self):
        self.assertEqual(apply._pick_option(["Yes", "No"], "yes"), "Yes")
        self.assertEqual(apply._pick_option(["• karriere.at", "• LinkedIn"], "karriere.at"), "• karriere.at")

    def test_german_names(self):
        self.assertEqual(apply._pick_option(["Deutschland", "Österreich"], "Austria"), "Österreich")
        self.assertEqual(apply._pick_option(["Mr", "Ms", "Other"], "Herr"), "Mr")

    def test_no_match(self):
        self.assertIsNone(apply._pick_option(["Red", "Blue"], "Austria"))


class Confirmation(unittest.TestCase):
    def test_intro_is_not_a_confirmation(self):
        before = apply._success_phrases(["Vielen Dank für dein Interesse an einer Mitarbeit. Bitte fülle das Formular aus."])
        after_fail = apply._success_phrases(["Das Formular enthält Fehler. Vielen Dank für dein Interesse an einer Mitarbeit."])
        self.assertFalse(after_fail - before)

    def test_real_confirmation_is_new(self):
        before = apply._success_phrases(["Vielen Dank für dein Interesse an einer Mitarbeit."])
        after = apply._success_phrases(["Vielen Dank für die Bewerbung. Gemeinsam gestalten wir die Stadt."])
        self.assertTrue(after - before)
        self.assertTrue(apply._success_phrases(["You've submitted your application. Good luck!"]))

    def test_error_words(self):
        self.assertTrue(apply.FORM_ERROR.search("Das Formular enthält Fehler."))
        self.assertTrue(apply.FORM_ERROR.search("Please fill in this required field."))
        self.assertFalse(apply.FORM_ERROR.search("Thank you, we received your application."))


class Rules(unittest.TestCase):
    def setUp(self):
        self.cfg, self.con = db()

    def known(self, label, name="", choice=False):
        return apply._known(self.cfg, self.con, label, name, choice)

    def test_basic_fields(self):
        self.assertEqual(self.known("Vorname:*"), "Test")
        self.assertEqual(self.known("E-Mail-Adresse"), "t@example.com")

    def test_birth_date_is_austrian_format(self):
        self.assertEqual(self.known("Geburtsdatum"), "03.02.2001")

    def test_education_dates_are_not_the_start_date(self):
        self.assertIsNone(self.known("Start date year"))

    def test_choices_use_rules_only_for_personal_facts(self):
        self.assertEqual(self.known("Anrede:*", choice=True), "Herr")
        self.assertIsNone(self.known("Are you willing to relocate? salary", choice=True))

    def test_personal_questions_never_go_to_the_ai(self):
        for q in ("Anrede", "Geschlecht", "Date of birth", "Nationality", "Staatsbürgerschaft"):
            self.assertTrue(apply.PERSONAL.search(q), q)
        self.assertFalse(apply.PERSONAL.search("How many years of Python experience do you have?"))


class Buttons(unittest.TestCase):
    def test_apply_start(self):
        for t in ("Apply now", "Jetzt bewerben", "Bewerbungsformular ausfüllen", "Apply without account"):
            self.assertTrue(apply.APPLY_START.search(t), t)
        self.assertFalse(apply.APPLY_START.search("Subscribe to newsletter"))

    def test_bot_wall_text(self):
        self.assertTrue(apply.BLOCKED.search("Performing security verification"))


class SiteMemory(unittest.TestCase):
    def test_wall_after_one_block(self):
        s = {"a.com": {"ok": 0, "wall": 1, "soft": 0, "reason": "needs an account"},
             "b.com": {"ok": 2, "wall": 1, "soft": 0, "reason": "x"},
             "c.com": {"ok": 0, "wall": 0, "soft": 1, "reason": "x"}}
        self.assertTrue(apply._known_wall(s, "a.com"))
        self.assertIsNone(apply._known_wall(s, "b.com"))      # has worked before: keep trying
        self.assertIsNone(apply._known_wall(s, "c.com"))      # one soft failure is not enough
        self.assertIsNone(apply._known_wall(s, "new.com"))

    def test_note_kinds(self):
        self.assertTrue(apply.WALL_NOTE.search("x.at needs an account — sign in once"))
        self.assertTrue(apply.WALL_NOTE.search("Adzuna only lists this job and its apply link blocks automated browsers"))
        self.assertFalse(apply.WALL_NOTE.search("needs your answer: Birth date"))


class Matching(unittest.TestCase):
    def test_keyword_score(self):
        hi = match.keyword_score(CFG, "Junior Python Developer", "python sql docker", [])
        lo = match.keyword_score(CFG, "Accountant", "excel", [])
        self.assertGreater(hi, 50)
        self.assertLess(lo, 20)

    def test_location(self):
        self.assertEqual(match.where_ok(CFG, "1160 Wien", False, ""), "vienna")
        self.assertIsNone(match.where_ok(CFG, "Berlin", False, ""))

    def test_excluded_titles(self):
        self.assertTrue(match.excluded(CFG, "Senior Data Engineer"))
        self.assertFalse(match.excluded(CFG, "Data Engineer"))


class Triage(unittest.TestCase):
    def setUp(self):
        self.cfg, self.con = db()

    def add(self, jid, title, company, status, score=None, verdict="apply"):
        self.con.execute("INSERT INTO jobs (id, dedup_key, source, title, company, status, kw_score, ai_score, ai_json, flags) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?)", (jid, jid, "t", title, company, status, 50, score,
                                                        json.dumps({"verdict": verdict}) if score is not None else None, ""))
        self.con.commit()

    def status(self, jid):
        return self.con.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0]

    def test_bands(self):
        self.add("a", "A", "Alpha GmbH", "new", 90, "apply")
        self.add("b", "B", "Beta GmbH", "new", 50, "maybe")
        self.add("c", "C", "Gamma GmbH", "new", 10, "skip")
        match.triage(self.cfg, self.con, True)
        self.assertEqual([self.status(x) for x in "abc"], ["queued", "review", "skipped"])

    def test_duplicate_of_an_applied_job_is_skipped(self):
        self.add("old", "Data Analyst (m/f/x)", "ADMIRAL Technologies", "applied", 80)
        self.add("new", "Data Analyst (m/f/x)", "ADMIRAL", "new", 80, "apply")
        match.triage(self.cfg, self.con, True)
        self.assertEqual(self.status("new"), "skipped")


class Replies(unittest.TestCase):
    JOBS = [{"id": "1", "title": "Data Engineer (m/w/d)", "company": "Wolf Theiss Rechtsanwälte GmbH & Co KG", "status": "applied"},
            {"id": "2", "title": "Praktikant:in Customer Insights", "company": "Magenta Telekom", "status": "applied"}]

    def test_classify(self):
        self.assertEqual(replies.classify("Ihre Bewerbung", "leider müssen wir Ihnen mitteilen, dass ..."), "rejected")
        self.assertEqual(replies.classify("Einladung zum Gespräch", "wir laden Sie herzlich zu einem Kennenlerngespräch ein"), "interview")
        self.assertEqual(replies.classify("Bewerbung eingegangen", "Vielen Dank für Ihre Bewerbung. Wir melden uns."), "confirmed")
        self.assertIsNone(replies.classify("Newsletter", "Our weekly news"))

    def test_confirmation_that_mentions_interviews_is_not_an_invitation(self):
        t = "Thank you for your application. If your profile fits, we will contact you for an interview."
        self.assertEqual(replies.classify("Application received", t), "confirmed")

    def test_match_job(self):
        j = replies.match_job(self.JOBS, "Wolf Theiss Recruiting <jobs@wolftheiss.com>", "Your application", "Data Engineer")
        self.assertEqual(j["id"], "1")
        self.assertIsNone(replies.match_job(self.JOBS, "shop@example.com", "Your order", "Thanks for buying"))


class Names(unittest.TestCase):
    def test_pdf_name(self):
        cfg = {"profile": {"name": "Test Person"}}
        self.assertEqual(server._pdf_name(cfg, {"company": "Magenta Telekom"}, "cv"), "Test_Person_CV_Magenta_Telekom.pdf")
        self.assertEqual(server._pdf_name(cfg, {"company": "Datenpol Gmbh"}, "letter"), "Test_Person_Cover_Letter_Datenpol.pdf")

    def test_slug_candidates(self):
        self.assertIn("bitpanda", sources.slug_candidates("Bitpanda GmbH"))

    def test_unconfigured_sources_are_skipped(self):
        from jobpilot.pipeline import enabled_sources
        cfg = {"sources": {"adzuna": {"enabled": True, "app_id": "", "app_key": ""}, "karriere": {"enabled": True}}}
        names = enabled_sources(cfg)
        self.assertNotIn("adzuna", names)
        self.assertIn("karriere", names)


if __name__ == "__main__":
    unittest.main()


class NewForms(unittest.TestCase):
    def test_nationality_options(self):
        self.assertEqual(apply._pick_option(["Österreich", "Andere Nationalität"], "Italian"), "Andere Nationalität")
        self.assertEqual(apply._pick_option(["Österreich", "Andere Nationalität"], "Österreich"), "Österreich")

    def test_taken_down_postings(self):
        for t in ("Job nicht gefunden | Erste Group", "This position is no longer available", "Die Stelle ist leider nicht mehr verfügbar"):
            self.assertTrue(apply.GONE.search(t), t)
        self.assertFalse(apply.GONE.search("Data Engineer (m/w/d) — jetzt bewerben"))

    def test_date_field_gets_a_date(self):
        cfg, con = db()
        cfg["apply"]["answers"]["earliest_start"] = "As soon as possible"
        v = apply._known(cfg, con, "Verfügbarkeit ab: (TT.MM.JJJJ)", "")
        self.assertRegex(v, r"^\d{2}\.\d{2}\.\d{4}$")
        self.assertEqual(apply._known(cfg, con, "Earliest start", ""), "As soon as possible")

    def test_address_line_2_stays_empty(self):
        cfg, con = db()
        cfg["apply"]["answers"]["address"] = "Teststraße 1"
        self.assertEqual(apply._known(cfg, con, "Anschrift Zeile 1", ""), "Teststraße 1")
        self.assertIsNone(apply._known(cfg, con, "Anschrift Zeile 2", ""))


class Signup(unittest.TestCase):
    def test_off_by_default(self):
        from jobpilot import signup
        self.assertFalse(signup.enabled(CFG))

    def test_register_buttons(self):
        from jobpilot import signup
        for t in ("Create a new applicant profile", "Jetzt registrieren", "Konto erstellen", "Sign up"):
            self.assertTrue(signup.REGISTER.search(t), t)
        self.assertFalse(signup.REGISTER.search("Log in"))


class PrefilterTest(unittest.TestCase):
    def job(self, title, types=""):
        return {"title": title, "kw_score": 0, "types": types, "where_ok": "vienna"}

    def test_default_is_tech(self):
        from jobpilot.match import prefilter, worth_scoring
        rel, other = prefilter({"search": {}})
        self.assertTrue(worth_scoring(self.job("Junior Data Analyst"), rel, other))
        self.assertFalse(worth_scoring(self.job("Marketing Manager"), rel, other))

    def test_own_field(self):
        from jobpilot.match import prefilter, worth_scoring
        rel, other = prefilter({"search": {"relevant_titles": ["marketing", "brand"], "other_titles": ["software"]}})
        self.assertTrue(worth_scoring(self.job("Marketing Manager"), rel, other))
        self.assertFalse(worth_scoring(self.job("Junior Data Analyst"), rel, other))
        self.assertFalse(worth_scoring(self.job("Software Praktikum", "intern"), rel, other))
        self.assertTrue(worth_scoring(self.job("Praktikum Kommunikation", "intern"), rel, other))

    def test_empty_other_list_excludes_nothing(self):
        from jobpilot.match import prefilter, worth_scoring
        rel, other = prefilter({"search": {"relevant_titles": [], "other_titles": []}})
        self.assertTrue(worth_scoring(self.job("Verkäufer Praktikum", "intern"), rel, other))
        self.assertFalse(worth_scoring(self.job("Verkäufer"), rel, other))


class SalaryTest(unittest.TestCase):
    def test_lines(self):
        from jobpilot.insights import salary_line as s
        self.assertEqual(s("Due to legal reasons we are obliged to disclose the minimum salary, which is EUR 3,143 gross per month."), "from €3,143 gross / month")
        self.assertEqual(s("Bruttomonatsgehalt ab EUR 2.064,00 (14x)"), "from €2,064 gross / month")
        self.assertEqual(s("3.000 bis 3.800 € brutto monatlich"), "€3,000 – €3,800 gross / month")
        self.assertEqual(s("40-55k EUR per year"), "€40,000 – €55,000 / year")
        self.assertEqual(s("Jahresbruttogehalt ab EUR53.000,- mit Überzahlung"), "from €53,000 gross / year")
        self.assertEqual(s("Einzahlungen ab EUR 100,-/Monat in die Pensionskasse"), "")
        self.assertEqual(s("We work across Europe with 2000 people"), "")
