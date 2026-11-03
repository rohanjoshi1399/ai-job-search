"""Tests for tools/tracker_board.py - the local drag-and-drop board over job_search_tracker.csv.

The board is a second writer to the tracker, so these pin the write rules it
shares with /outcome (only status, notes and a first-submission date change;
every other row stays byte-identical; notes never gain a comma) and the
request checks that keep other origins from writing (Host, per-run token,
JSON content type). They also pin that saved postings render without links
and that file routes cannot leave an application folder.
"""
import csv
import http.client
import json
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "tools" / "tracker_board.py"
HEADER = ["date", "company", "sector", "role", "role_type", "channel", "status", "contact_person",
          "fit_rating", "notes", "cv_file", "cover_letter_file", "source", "deadline"]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class BoardServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.root = root
        self.csv = root / "job_search_tracker.csv"
        rows = [HEADER,
                ["2026-10-01", "Acme, Inc.", "x", "ML Engineer, Search", "x", "portal", "drafted", "", "80",
                 "first note", "cv/main_acme_ml_engineer.tex", "cover_letters/cover_acme_ml_engineer.tex",
                 "https://example.com/job", "2026-12-01"],
                ["2026-10-02", "Beta", "x", "SWE", "x", "portal", "applied", "", "70", "", "", "", "", ""]]
        with open(self.csv, "w", encoding="utf-8", newline="") as f:
            csv.writer(f, lineterminator="\n").writerows(rows)
        app = root / "documents" / "applications" / "acme_ml_engineer"
        app.mkdir(parents=True)
        (app / "job_posting.md").write_text("Apply at https://evil.example/now\n<script>x</script>\n", encoding="utf-8")
        (app / "form_pack.md").write_text("# Pack\n| Field | Paste or select | NOTE |\n|---|---|---|\n| Email | a@b.com | |\n",
                                          encoding="utf-8")
        (root / "company_research").mkdir()
        (root / "company_research" / "beta.json").write_text(json.dumps({
            "company": "Beta", "fetched_date": "2026-10-01",
            "sources": {"website": {"url": "https://beta.example", "notes": "<b>raw</b> see https://beta.example/x"}},
            "network_contacts_note": "None."}), encoding="utf-8")
        self.port = free_port()
        self.proc = subprocess.Popen([sys.executable, str(TOOL), "--root", str(root), "--port", str(self.port),
                                      "--no-browser"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for _ in range(100):
            try:
                self.get("/api/board")
                break
            except OSError:
                time.sleep(0.1)
        status, page = self.get("/")
        self.token = page.split('const TOKEN = "', 1)[1].split('"', 1)[0]

    def tearDown(self):
        self.proc.terminate()
        self.proc.wait(timeout=5)
        self.proc.stdout.close()
        self.proc.stderr.close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdrs = {"Host": host or f"127.0.0.1:{self.port}"}
        hdrs.update(headers or {})
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read().decode("utf-8")
        conn.close()
        return resp.status, data

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def move(self, status, idx=0, company="Acme, Inc.", role="ML Engineer, Search", when="2026-11-02", **headers):
        hdrs = {"Content-Type": "application/json", "X-Board-Token": self.token}
        hdrs.update(headers)
        body = json.dumps({"idx": idx, "company": company, "role": role, "status": status, "date": when})
        return self.request("POST", "/api/move", body=body, headers=hdrs)

    def rows(self):
        with open(self.csv, encoding="utf-8", newline="") as f:
            return list(csv.reader(f))

    def test_move_changes_only_status_notes_and_submission_date(self):
        before = self.rows()
        status, _ = self.move("applied")
        self.assertEqual(status, 200)
        after = self.rows()
        self.assertEqual(after[0], before[0])
        self.assertEqual(after[2], before[2])
        row = after[1]
        self.assertEqual(row[6], "applied")
        self.assertEqual(row[0], "2026-11-02")
        self.assertEqual(row[9], "first note; applied 2026-11-02")
        self.assertEqual(row[1:6] + row[7:9] + row[10:], before[1][1:6] + before[1][7:9] + before[1][10:])
        outcome = (self.root / "documents" / "applications" / "acme_ml_engineer" / "outcome.md").read_text(encoding="utf-8")
        self.assertIn("**Status:** in_progress", outcome)

    def test_final_status_sets_outcome_and_keeps_date(self):
        self.move("applied")
        self.move("rejected", when="2026-11-20")
        row = self.rows()[1]
        self.assertEqual((row[0], row[6]), ("2026-11-02", "rejected"))
        outcome = (self.root / "documents" / "applications" / "acme_ml_engineer" / "outcome.md").read_text(encoding="utf-8")
        self.assertIn("**Status:** rejected", outcome)
        self.assertIn("**Date resolved:** 2026-11-20", outcome)

    def test_writes_are_refused_without_token_json_or_local_host(self):
        self.assertEqual(self.move("applied", **{"X-Board-Token": "wrong"})[0], 403)
        self.assertEqual(self.move("applied", **{"Content-Type": "text/plain"})[0], 403)
        status, _ = self.request("POST", "/api/move", body="{}", host="evil.example",
                                 headers={"Content-Type": "application/json", "X-Board-Token": self.token})
        self.assertEqual(status, 403)
        self.assertEqual(self.get("/api/board", host="evil.example")[0], 403)
        self.assertEqual(self.rows()[1][6], "drafted")

    def test_bad_status_and_unknown_row_are_rejected(self):
        self.assertEqual(self.move("bogus")[0], 400)
        self.assertEqual(self.move("applied", idx=5, company="Nope")[0], 400)
        self.assertEqual(self.rows()[1][6], "drafted")

    def test_saved_posting_renders_without_links_or_markup(self):
        status, page = self.get("/view/acme_ml_engineer/job_posting.md")
        self.assertEqual(status, 200)
        self.assertNotIn('href="https://evil.example', page)
        self.assertNotIn("<script>x</script>", page)

    def test_form_pack_renders_table_with_copy(self):
        status, page = self.get("/view/acme_ml_engineer/form_pack.md")
        self.assertEqual(status, 200)
        self.assertIn("<table>", page)
        self.assertIn('class="copytext"', page)

    def test_research_page_escapes_notes_and_links_sources(self):
        status, page = self.get("/research/beta")
        self.assertEqual(status, 200)
        self.assertIn("&lt;b&gt;raw&lt;/b&gt;", page)
        self.assertIn('href="https://beta.example/x"', page)
        board = json.loads(self.get("/api/board")[1])
        self.assertEqual([r["research"] for r in board["rows"]], [None, "beta"])

    def test_file_routes_stay_inside_the_application_folder(self):
        for path in ("/files/acme_ml_engineer/..%2F..%2F..%2Fjob_search_tracker.csv",
                     "/view/acme_ml_engineer/..%2F..%2F..%2Fjob_search_tracker.csv",
                     "/research/..%2Fjob_search_tracker", "/files/../job_search_tracker.csv"):
            self.assertEqual(self.get(path)[0], 404, path)


if __name__ == "__main__":
    unittest.main()
