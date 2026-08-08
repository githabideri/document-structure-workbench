"""Tests for the readable page-text rendering helper (views.page_text_to_plain).

Visual-OCR transcriptions sometimes embed the table as raw HTML in the
``page_text`` artifact. The document view must render that as readable plain
text instead of dumping the literal markup into the "Complete page text" block.
"""
from django.test import SimpleTestCase

from workbench.views import page_text_to_plain

RAW_TABLE = (
    "<table border=1 style='margin: auto; word-wrap: break-word;'>"
    "<tr><td style='text-align: center;'>Nr.</td><td>Name :</td></tr>"
    "<tr><td>2801</td><td>Guschlbauer Barbara</td></tr>"
    "</table>"
)


class PageTextToPlainTests(SimpleTestCase):
    def test_plain_text_untouched(self):
        self.assertEqual(page_text_to_plain("just text\nsecond line"), "just text\nsecond line")

    def test_unescapes_entities_without_html(self):
        self.assertEqual(page_text_to_plain("a &amp; b"), "a & b")

    def test_strips_table_markup_to_readable_rows(self):
        out = page_text_to_plain(RAW_TABLE)
        self.assertNotIn("<table", out)
        self.assertNotIn("border=", out)
        self.assertIn("Nr. Name :", out)
        self.assertIn("2801 Guschlbauer Barbara", out)
        # Each table row becomes its own line.
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        self.assertEqual(lines[0], "Nr. Name :")
        self.assertEqual(lines[1], "2801 Guschlbauer Barbara")

    def test_mixed_text_and_table(self):
        out = page_text_to_plain("header text\n" + RAW_TABLE)
        self.assertNotIn("<table", out)
        self.assertIn("header text", out)
        self.assertIn("2801 Guschlbauer Barbara", out)

    def test_empty(self):
        self.assertEqual(page_text_to_plain(""), "")
