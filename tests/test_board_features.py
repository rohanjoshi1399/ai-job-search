"""Tests for the tracker board's reminders, interviews, notes, weekly goal, calendar
files and stats (tools/board_state.py, the /api routes in tools/tracker_board.py,
and tools/remind.py). They pin that notes stay comma-free for the tracker, that
logging an interview moves the row and ticks outcome.md, that derived reminders
follow /outcome's 10-day follow-up rule, and that stats count a withdrawal that
was never submitted as not submitted."""
import csv
import json
import subprocess
import sys
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO / "tests"))
import board_state as bs  # noqa: E402
import tracker_board as tb  # noqa: E402
from test_tracker_board import HEADER, BoardServerTests  # noqa: E402


def rec(company, role, status, day, notes="", deadline="", role_type="SWE"):
    return {"company": company, "role": role, "status": status, "date": day, "notes": notes,
            "deadline": deadline, "role_type": role_type, "stem": None}


class StateLogicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_followup_after_ten_quiet_days_and_stops_after_two(self):
        st = bs.load(Path(self.tmp.name))
        due = bs.due([rec("A", "R", "applied", "2026-10-01")], st, date(2026, 10, 12))
        self.assertEqual([i["kind"] for i in due["overdue"]], ["followup"])
        quiet = rec("A", "R", "applied", "2026-10-01", "followed up 2026-10-11; followed up 2026-10-20")
        self.assertEqual(bs.all_reminders([quiet], st, date(2026, 11, 30)), [])

    def test_deadline_and_interview_reminders(self):
        st = {"version": 1, "goal": {}, "reminders": [], "dismissed": [],
              "interviews": [{"id": "i1", "company": "A", "role": "R", "date": "2026-10-20", "time": "10:00", "stage": "technical"}]}
        items = bs.all_reminders([rec("A", "R", "drafted", "2026-10-01", deadline="2026-10-15")], st, date(2026, 10, 1))
        kinds = [(i["kind"], i["date"]) for i in items]
        self.assertIn(("deadline", "2026-10-12"), kinds)
        self.assertIn(("deadline", "2026-10-15"), kinds)
        self.assertIn(("prep", "2026-10-19"), kinds)
        self.assertIn(("interview", "2026-10-20"), kinds)

    def test_clean_strips_commas_quotes_and_newlines(self):
        self.assertEqual(bs.clean('Spoke to Jane, "recruiter"\nnext week'), "Spoke to Jane - recruiter next week")

    def test_ics_is_valid_and_escaped(self):
        out = bs.ics([{"id": "r1", "date": "2026-11-09", "time": None, "text": "Follow up, Acme; now", "company": "Acme", "role": "SWE"}])
        self.assertTrue(out.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertIn("DTSTART;VALUE=DATE:20261109", out)
        self.assertIn("SUMMARY:Follow up\\, Acme\\; now", out)
        self.assertTrue(all(len(line.encode()) <= 75 for line in out.split("\r\n")))

    def test_stats_funnel_and_unsubmitted_withdrawal(self):
        root = Path(self.tmp.name)
        st = bs.load(root)
        st["goal"]["weekly_submitted"] = 1
        rows = [rec("A", "R", "applied", "2026-10-26"), rec("B", "R", "rejected", "2026-10-20", "rejected 2026-10-30"),
                rec("C", "R", "interview", "2026-10-21", "interview 2026-10-25"), rec("D", "R", "hired", "2026-10-05"),
                rec("E", "R", "withdrawn", "2026-10-01", "posting removed - not submitted"), rec("F", "R", "drafted", "2026-10-02")]
        s = bs.stats(rows, st, root, date(2026, 10, 28))
        self.assertEqual(s["kpis"]["submitted"], 4)
        self.assertEqual(s["kpis"]["interview_rate"], 50)
        self.assertEqual(s["kpis"]["response_rate"], 75)
        self.assertEqual(s["kpis"]["median_days_to_response"], 7)  # 10 days (B) and 4 days (C)
        flows = {(l["source"], l["target"]): l["value"] for l in s["sankey"]}
        self.assertEqual(flows[("Applied", "Interviewed")], 2)
        self.assertEqual(flows[("Offer", "Accepted")], 1)
        self.assertIn("Applied [2] Interviewed", s["sankeymatic"])
        self.assertEqual(s["goal"]["this_week"], 1)
        self.assertGreaterEqual(s["goal"]["streak"], 1)


class ShortlistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.addCleanup(setattr, tb, "ROOT", tb.ROOT)
        tb.ROOT = root
        job = {"status": "ranked", "rank_score": 80, "url": "https://jobs.test/x"}
        seen = {"a": {**job, "company": "Open Co", "title": "ML Engineer", "location": "Seattle"},
                "b": {**job, "company": "Late Co", "title": "ML Engineer", "gaps": ["TIMING FAIL: starts January"]},
                "c": {**job, "company": "Far Co", "title": "ML Engineer", "location_verdict": "FAIL"},
                "d": {**job, "company": "Skipped Co, Inc.", "title": "ML Engineer"},
                "e": {**job, "company": "Old Co", "title": "ML Engineer", "location": "FAIL"},
                "f": {**job, "company": "Tracked Co", "title": "ML Engineer", "status": "ranked"}}
        (root / "job_scraper").mkdir()
        (root / "job_scraper" / "seen_jobs.json").write_text(json.dumps({"seen": seen}), encoding="utf-8")
        (root / "documents" / "cv").mkdir(parents=True)
        (root / "documents" / "cv" / "daily_run.json").write_text(
            json.dumps({"skip_companies": [{"name": "Skipped Co", "why": "no sponsorship"}, "Nobody", 7]}), encoding="utf-8")

    def test_ruled_out_jobs_are_flagged_and_sorted_last(self):
        out = {j["company"]: j for j in tb.shortlist([("Tracked Co", "ML Engineer")])}
        self.assertNotIn("Tracked Co", out)
        self.assertEqual((out["Open Co"]["excluded"], out["Open Co"]["skipped"], out["Open Co"]["location"]), (None, None, "Seattle"))
        self.assertEqual(out["Late Co"]["excluded"], "Ruled out: start date")
        self.assertEqual(out["Far Co"]["excluded"], "Ruled out: location")
        self.assertEqual(out["Old Co"]["excluded"], "Ruled out: location")
        self.assertEqual(out["Old Co"]["location"], "")
        self.assertEqual((out["Skipped Co, Inc."]["excluded"], out["Skipped Co, Inc."]["skipped"]), (None, "no sponsorship"))
        self.assertEqual(tb.shortlist([])[0]["company"], "Open Co")

    def test_missing_or_broken_skip_list_is_ignored(self):
        (tb.ROOT / "documents" / "cv" / "daily_run.json").write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(tb.skip_list(), [])
        (tb.ROOT / "documents" / "cv" / "daily_run.json").unlink()
        self.assertEqual(tb.skip_list(), [])


class FeatureServerTests(BoardServerTests):
    def outcome(self):
        return (self.root / "documents" / "applications" / "acme_ml_engineer" / "outcome.md")

    def post(self, path, body, **headers):
        hdrs = {"Content-Type": "application/json", "X-Board-Token": self.token, **headers}
        return self.request("POST", path, body=json.dumps(body), headers=hdrs)

    ref = {"idx": 0, "company": "Acme, Inc.", "role": "ML Engineer, Search"}

    def test_note_is_comma_free_and_followup_marker(self):
        self.assertEqual(self.post("/api/note", {**self.ref, "text": "Called Jane, recruiter", "date": "2026-11-02"})[0], 200)
        self.assertEqual(self.post("/api/note", {**self.ref, "followed_up": True, "date": "2026-11-03"})[0], 200)
        notes = self.rows()[1][9]
        self.assertEqual(notes, "first note; Called Jane - recruiter 2026-11-02; followed up 2026-11-03")
        self.assertEqual(len(self.rows()[1]), len(HEADER))

    def test_contact_sets_only_that_column(self):
        before = self.rows()[1]
        self.post("/api/contact", {**self.ref, "contact": "Jane Doe, Recruiter"})
        after = self.rows()[1]
        self.assertEqual(after[7], "Jane Doe - Recruiter")
        self.assertEqual(after[:7] + after[8:], before[:7] + before[8:])

    def test_interview_moves_row_and_ticks_outcome(self):
        self.move("applied")
        status, body = self.post("/api/interview", {**self.ref, "date": "2026-11-12", "time": "10:30", "stage": "technical"})
        self.assertEqual(status, 200)
        self.assertEqual(self.rows()[1][6], "interview")
        self.assertIn("- [x] Technical interview (2026-11-12)", self.outcome().read_text(encoding="utf-8"))
        board = json.loads(body)
        self.assertEqual(len(board["interviews"]), 1)
        iid = board["interviews"][0]["id"]
        self.assertEqual(self.get(f"/ics/interview%3A{iid}.ics")[0], 200)

    def test_reminder_lifecycle_and_goal(self):
        status, body = self.post("/api/reminder", {"action": "add", **self.ref, "date": "2026-01-01", "text": "Check portal"})
        self.assertEqual(status, 200)
        rid = json.loads(body)["reminders"][0]["id"]
        self.assertIn(rid, [i["id"] for i in json.loads(body)["due"]["overdue"]])
        status, cal = self.get(f"/ics/{rid}.ics")
        self.assertEqual(status, 200)
        self.assertIn("SUMMARY:Check portal", cal)
        self.assertEqual(json.loads(self.post("/api/reminder", {"action": "done", "id": rid})[1])["reminders"], [])
        self.assertEqual(json.loads(self.post("/api/goal", {"weekly_submitted": 4})[1])["goal"]["weekly_submitted"], 4)
        self.assertEqual(self.post("/api/goal", {"weekly_submitted": 500})[0], 400)

    def test_new_routes_need_token_and_json(self):
        for path in ("/api/note", "/api/contact", "/api/interview", "/api/reminder", "/api/goal"):
            self.assertEqual(self.post(path, {}, **{"X-Board-Token": "nope"})[0], 403, path)
            self.assertEqual(self.post(path, {}, **{"Content-Type": "text/plain"})[0], 403, path)

    def test_stats_routes(self):
        self.assertEqual(self.get("/stats")[0], 200)
        stats = json.loads(self.get("/api/stats")[1])
        self.assertEqual(stats["kpis"]["submitted"], 1)
        self.assertEqual(stats["sankeymatic"], "Applied [1] Waiting")

    def test_remind_cli_reads_the_same_state(self):
        self.post("/api/reminder", {"action": "add", **self.ref, "date": "2026-01-01", "text": "Check portal"})
        out = subprocess.run([sys.executable, str(REPO / "tools" / "remind.py"), "--root", str(self.root), "--json"],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("Check portal", [i["text"] for i in json.loads(out.stdout)["overdue"]])


if __name__ == "__main__":
    unittest.main()
