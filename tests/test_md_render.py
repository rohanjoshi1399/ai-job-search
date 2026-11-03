"""Tests for tools/md_render.py: the escape-first renderer behind the tracker board."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from md_render import inline, render  # noqa: E402


class EscapingTests(unittest.TestCase):
    def test_raw_html_is_escaped(self):
        out = render("<script>alert(1)</script> and <img src=x onerror=y>")
        self.assertNotIn("<script>", out)
        self.assertNotIn("<img", out)
        self.assertIn("&lt;script&gt;", out)

    def test_only_http_links_become_anchors(self):
        out = render("[click](javascript:alert(1)) and [ok](https://example.com/a)")
        self.assertNotIn('href="javascript', out)
        self.assertIn('href="https://example.com/a"', out)
        self.assertIn('rel="noopener noreferrer nofollow"', out)

    def test_link_url_quotes_are_escaped(self):
        out = inline('see https://example.com/"onmouseover="x')
        self.assertNotIn('"onmouseover="', out)

    def test_no_links_mode_for_untrusted_text(self):
        out = render("apply at https://evil.example/x and [here](https://evil.example/y)", autolink=False)
        self.assertNotIn("<a ", out)
        self.assertIn("https://evil.example/x", out)

    def test_code_spans_and_fences_are_literal(self):
        out = render("`**not bold**`\n\n```text\n<b>raw</b> **x**\n```")
        self.assertIn("<code>**not bold**</code>", out)
        self.assertIn("&lt;b&gt;raw&lt;/b&gt; **x**", out)


class StructureTests(unittest.TestCase):
    def test_table_with_copy_column(self):
        md = "| Field | Paste or select | NOTE |\n|---|---|---|\n| Email | a@b.com | |\n| City | **FILL IN** | x |"
        out = render(md)
        self.assertIn("<table>", out)
        self.assertEqual(out.count('class="copy"'), 1)
        self.assertIn('<span class="copytext">a@b.com</span>', out)

    def test_headings_lists_rules_and_checkboxes(self):
        out = render("# T\n## S\n- a\n  - nested\n- [ ] todo\n\n1. one\n2. two\n\n---\ntext")
        for tag in ("<h1>T</h1>", "<h2>S</h2>", "<ul>", "<ol>", "<hr>", 'type="checkbox"', "<p>text</p>"):
            self.assertIn(tag, out)
        self.assertEqual(out.count("<ul>"), 2)

    def test_fence_gets_copy_button(self):
        out = render("```text\nHi [Name],\n```")
        self.assertIn('class="copy"', out)
        self.assertIn("Hi [Name],", out)


if __name__ == "__main__":
    unittest.main()
