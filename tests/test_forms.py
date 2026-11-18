"""Run the form reader against saved copies of real application forms (tests/forms/*.html).

Each check is what the filler relies on: the right question text for a field, required flags, option labels.
A change that breaks one of these sites shows up here. Needs Playwright's Chromium; skipped without it.
"""
import unittest
from pathlib import Path

from jobpilot.apply import FIELDS_JS

FORMS = Path(__file__).parent / "forms"


def read(name: str) -> list[dict]:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        p = b.new_page(viewport={"width": 1280, "height": 1600})
        p.set_content((FORMS / f"{name}.html").read_text(encoding="utf-8"), wait_until="domcontentloaded")
        fields = p.evaluate(FIELDS_JS)
        b.close()
    return fields


def labels(fields, type_=None):
    return [f["label"] for f in fields if type_ is None or f["type"] == type_]


try:
    import playwright  # noqa: F401
    HAVE = True
except ImportError:
    HAVE = False


@unittest.skipUnless(HAVE, "playwright not installed")
class Cornerstone(unittest.TestCase):          # ÖBB (oebb.csod.com)
    @classmethod
    def setUpClass(cls):
        cls.f = read("oebb_cornerstone")

    def test_text_fields_have_their_own_labels(self):
        ls = labels(self.f)
        for want in ("Vorname*", "Nachname*", "E-Mail*", "Telefon*", "Postleitzahl", "Anschrift Zeile 1"):
            self.assertIn(want, ls)

    def test_captions_above_date_fields(self):
        self.assertTrue(any(l.startswith("Geburtsdatum") for l in labels(self.f, "text")))
        self.assertTrue(any(l.startswith("Verfügbarkeit ab") for l in labels(self.f, "text")))

    def test_radio_groups_and_options(self):
        g = [f for f in self.f if f["type"] == "radio" and f["label"].startswith("Geschlecht")]
        self.assertEqual(sorted(x["option"] for x in g), sorted(["weiblich (w)", "männlich (m)", "divers (d)", "inter (i)"]))
        n = [f for f in self.f if f["type"] == "radio" and f["label"].startswith("Nationalität")]
        self.assertEqual({x["option"] for x in n}, {"Österreich", "Andere Nationalität"})
        self.assertTrue(all(x["required"] for x in g + n))

    def test_custom_checkboxes_found(self):
        src = [f for f in self.f if f["type"] == "checkbox" and "aufmerksam" in f["label"]]
        self.assertGreaterEqual(len(src), 10)
        self.assertTrue(any("Job-Plattform" in f["option"] for f in src))

    def test_uploads(self):
        files = labels(self.f, "file")
        self.assertIn("Lebenslauf hochladen", files)
        self.assertIn("Anschreiben hochladen", files)


@unittest.skipUnless(HAVE, "playwright not installed")
class DVinci(unittest.TestCase):               # Wiener Stadtwerke group
    @classmethod
    def setUpClass(cls):
        cls.f = read("wienenergie_dvinci")

    def test_selects_behind_widgets(self):
        sel = {f["label"]: f for f in self.f if f["type"] == "select"}
        self.assertTrue(any(k.startswith("Anrede") for k in sel))
        src = next(v for k, v in sel.items() if "aufmerksam" in k)
        self.assertTrue(any("karriere.at" in o for o in src["options"]))

    def test_basic_fields(self):
        ls = labels(self.f)
        self.assertTrue(any(l.startswith("Vorname") for l in ls))
        self.assertTrue(any(l.startswith("E-Mail") for l in ls))


@unittest.skipUnless(HAVE, "playwright not installed")
class Onlyfy(unittest.TestCase):               # Magenta, ADMIRAL, Wolf Theiss, …
    @classmethod
    def setUpClass(cls):
        cls.f = read("magenta_onlyfy")

    def test_styled_select_and_fields(self):
        sel = [f for f in self.f if f["type"] == "select"]
        self.assertTrue(any(f.get("btn") and f["label"].lower().startswith(("salutation", "anrede", "title")) for f in sel))
        names = " ".join(f["name"] for f in self.f)
        for want in ("personal_details[first_name]", "personal_details[email]", "cv[cv]"):
            self.assertIn(want, names)


if __name__ == "__main__":
    unittest.main()
