"""Affiliation strings from Crossref/arXiv get reduced to a clean institution."""
import unittest

from app.finder import _clean_institution


class AffiliationCleanupTests(unittest.TestCase):
    def test_picks_university_segment(self):
        raw = "Fakultät für Informatik, Technische Universität München, 80290 München, Germany"
        self.assertEqual(_clean_institution(raw), "Technische Universität München")

    def test_picks_institute_when_no_university(self):
        raw = "IDSIA, Corso Elvezia 36, 6900 Lugano, Switzerland"
        self.assertEqual(_clean_institution(raw), "IDSIA")

    def test_drops_department_prefix(self):
        raw = "Department of Computer Science, Stanford University, CA, USA"
        self.assertEqual(_clean_institution(raw), "Stanford University")

    def test_clean_name_passes_through(self):
        self.assertEqual(_clean_institution("Massachusetts Institute of Technology"),
                         "Massachusetts Institute of Technology")

    def test_empty_and_whitespace(self):
        self.assertEqual(_clean_institution(""), "")
        self.assertEqual(_clean_institution("   "), "")

    def test_no_keyword_drops_postal_country(self):
        raw = "Acme Research, 12345 Springfield, USA"
        self.assertEqual(_clean_institution(raw), "Acme Research")


if __name__ == "__main__":
    unittest.main()
