"""Tests for the tracker board's reminders, interviews, notes, weekly goal, calendar
files and stats (tools/board_state.py, the /api routes in tools/tracker_board.py,
and tools/remind.py). They pin that notes stay comma-free for the tracker, that
logging an interview moves the row and ticks outcome.md, that derived reminders
follow /outcome's 10-day follow-up rule, and that stats count a withdrawal that
was never submitted as not submitted."""
import csv
import json
import re
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


class OutcomeParityTests(unittest.TestCase):
    """The board is a second writer, so pin its vocabulary to /outcome's own text: if /outcome changes a
    status name, a stage label or the follow-up rule, this fails instead of the two drifting apart."""

    def setUp(self):
        self.spec = (REPO / ".claude" / "commands" / "outcome.md").read_text(encoding="utf-8")

    def test_status_vocabulary_matches_outcome(self):
        line = next(l for l in self.spec.splitlines() if l.startswith("`drafted` |"))
        canonical = set(re.findall(r"`([a-z_]+)`", line))
        self.assertEqual(canonical, tb.STATUSES - {"on_hold"})  # on_hold is the board's one local addition
        self.assertEqual(set(tb.LEGACY), {"no response", "offer declined"})
        self.assertTrue("Readers must also accept the legacy space spellings `no response` and `offer declined`" in self.spec,
                        "outcome.md's legacy spellings changed")

    def test_stage_labels_and_followup_rule_match_outcome(self):
        for label in [v for k, v in bs.STAGES.items() if k != "other"] + ["Offer received"]:
            self.assertTrue(re.search(rf"^- \[[ x]\] {re.escape(label)}\b", self.spec, re.M), f"outcome.md has no '{label}' stage")
        self.assertTrue("default threshold of **10 days**" in self.spec, "outcome.md's follow-up threshold changed")
        self.assertEqual(bs.FOLLOWUP_DAYS, 10)
        self.assertTrue("fewer than **two** logged follow-ups" in self.spec, "outcome.md's follow-up cap changed")


class RunLauncherTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(setattr, tb, "ROOT", tb.ROOT)
        self.addCleanup(setattr, tb, "launch", tb.launch)
        tb.ROOT = Path(tmp.name)
        (tb.ROOT / "tools").mkdir()
        (tb.ROOT / "tools" / "scheduled_run.ps1").write_text("# stub", encoding="utf-8")
        tb.LAST_START["t"] = 0.0
        self.calls = []
        tb.launch = self.calls.append

    @unittest.skipUnless(sys.platform == "win32", "the run button is Windows-only")
    def test_find_starts_the_dry_run_once(self):
        tb.start_run({"mode": "find"})
        self.assertEqual(len(self.calls), 1)
        argv = self.calls[0]
        self.assertEqual(argv[0], "powershell")
        self.assertIn("-DryRun", argv)
        self.assertEqual(argv[argv.index("-MaxBudgetUsd") + 1], "8")
        self.assertTrue(argv[argv.index("-File") + 1].endswith("scheduled_run.ps1"))
        with self.assertRaisesRegex(ValueError, "already going"):
            tb.start_run({"mode": "find"})  # started seconds ago; the script has not taken the lock yet
        self.assertEqual(len(self.calls), 1)

    def test_unknown_mode_is_refused(self):
        with self.assertRaisesRegex(ValueError, "unknown run mode"):
            tb.start_run({"mode": "full"})
        self.assertEqual(self.calls, [])


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

    def app_dir(self):
        return self.root / "documents" / "applications" / "acme_ml_engineer"

    def test_submitting_archives_the_sent_cv_and_letter_once(self):
        (self.root / "cv").mkdir()
        (self.root / "cover_letters").mkdir()
        (self.root / "cv" / "main_acme_ml_engineer.tex").write_text("CV as sent", encoding="utf-8")
        (self.root / "cover_letters" / "cover_acme_ml_engineer.tex").write_text("Letter as sent", encoding="utf-8")
        self.assertEqual(self.move("applied")[0], 200)
        self.assertEqual((self.app_dir() / "cv_draft.tex").read_text(encoding="utf-8"), "CV as sent")
        self.assertEqual((self.app_dir() / "cover_letter.tex").read_text(encoding="utf-8"), "Letter as sent")
        self.assertIn("Archived the submitted cv_draft.tex and cover_letter.tex", self.outcome().read_text(encoding="utf-8"))
        (self.root / "cv" / "main_acme_ml_engineer.tex").write_text("edited after sending", encoding="utf-8")
        self.move("interview", when="2026-11-05")
        self.assertEqual((self.app_dir() / "cv_draft.tex").read_text(encoding="utf-8"), "CV as sent")

    def test_withdrawing_an_unsent_draft_archives_nothing(self):
        (self.root / "cv").mkdir()
        (self.root / "cv" / "main_acme_ml_engineer.tex").write_text("never sent", encoding="utf-8")
        self.move("withdrawn")
        self.assertFalse((self.app_dir() / "cv_draft.tex").exists())
        self.assertFalse(self.outcome().exists())

    def test_offer_ticks_offer_received_and_withdrawal_keeps_status(self):
        self.move("applied")
        self.move("offer", when="2026-11-20")
        text = self.outcome().read_text(encoding="utf-8")
        self.assertIn("- [x] Offer received (2026-11-20)", text)
        self.assertIn("**Status:** in_progress", text)
        self.move("rejected", when="2026-11-21")
        self.move("withdrawn", when="2026-11-22")
        text = self.outcome().read_text(encoding="utf-8")
        self.assertIn("**Status:** rejected", text)
        self.assertIn("**Date resolved:** 2026-11-21", text)
        self.assertIn("- 2026-11-22: Withdrawn by the candidate (tracker board).", text)

    def test_a_row_changed_on_disk_is_not_overwritten(self):
        rev = json.loads(self.get("/api/board")[1])["rows"][0]["rev"]
        rows = self.rows()
        rows[1][9] = "first note; rejected 2026-11-04"  # another command (e.g. /outcome) updated the row
        rows[1][6] = "rejected"
        with open(self.csv, "w", encoding="utf-8", newline="") as f:
            csv.writer(f, lineterminator="\n").writerows(rows)
        status, body = self.post("/api/move", {**self.ref, "rev": rev, "status": "applied", "date": "2026-11-05"})
        self.assertEqual(status, 409, body)
        self.assertTrue(json.loads(body)["stale"])
        self.assertEqual(self.rows()[1][6], "rejected")
        fresh = json.loads(self.get("/api/board")[1])["rows"][0]["rev"]
        self.assertEqual(self.post("/api/move", {**self.ref, "rev": fresh, "status": "applied", "date": "2026-11-05"})[0], 200)

    def test_ranked_actions_queue_hide_and_rule_out(self):
        (self.root / "job_scraper").mkdir()
        (self.root / "job_scraper" / "seen_jobs.json").write_text(json.dumps({"seen": {
            "zeta_ml": {"status": "ranked", "company": "Zeta Labs", "title": "ML Engineer", "rank_score": 70, "url": "https://jobs.test/z",
                        "strengths": ["RAG <b>match</b>"], "gaps": ["no Go"]},
            "old_job": {"status": "skipped", "company": "Old", "title": "X", "url": "https://jobs.test/o"}}}), encoding="utf-8")
        job = json.loads(self.post("/api/ranked", {"action": "queue", "key": "zeta_ml"})[1])["shortlist"][0]
        self.assertTrue(job["queued"])
        self.assertEqual(job["strengths"], ["RAG <b>match</b>"])  # stored as data; the page renders it as text
        self.assertEqual(self.post("/api/ranked", {"action": "queue", "key": "old_job"})[0], 400)
        job = json.loads(self.post("/api/ranked", {"action": "dismiss", "key": "zeta_ml", "why": "on-call, heavy",
                                                    "rule_out_company": True})[1])["shortlist"][0]
        self.assertFalse(job["queued"])
        self.assertEqual(job["dismissed"], "on-call - heavy")
        skips = json.loads((self.root / "documents" / "cv" / "daily_run.json").read_text(encoding="utf-8"))["skip_companies"]
        self.assertEqual(skips, [{"name": "Zeta Labs", "why": "on-call - heavy"}])
        self.post("/api/ranked", {"action": "dismiss", "key": "zeta_ml", "rule_out_company": True})
        self.assertEqual(len(json.loads((self.root / "documents" / "cv" / "daily_run.json").read_text(encoding="utf-8"))["skip_companies"]), 1)
        self.assertIsNone(json.loads(self.post("/api/ranked", {"action": "restore", "key": "zeta_ml"})[1])["shortlist"][0]["dismissed"])

    def test_run_status_digest_and_refusal_without_the_runner(self):
        daily = self.root / "reports" / "daily"
        daily.mkdir(parents=True)
        (daily / "2026-11-03.md").write_text("# Digest\n\n- Ranked 10 [link](https://jobs.test/a) <script>x</script>\n", encoding="utf-8")
        (daily / "run-2026-11-03-0647.json").write_text('noise\n{"subtype": "success", "is_error": false, "total_cost_usd": 5.5, "duration_ms": 900000}',
                                                       encoding="utf-8")
        run = json.loads(self.get("/api/run")[1])
        self.assertEqual((run["digest"], run["last"]["cost"], run["last"]["minutes"], run["last"]["ok"]), ("2026-11-03.md", 5.5, 15, True))
        self.assertFalse(run["available"])  # this root has no tools/scheduled_run.ps1
        status, page = self.get("/digest/2026-11-03.md")
        self.assertEqual(status, 200)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<script>x", page)
        self.assertEqual(self.get("/digest/..%2F..%2Fjob_search_tracker.csv")[0], 404)
        status, body = self.post("/api/run", {"mode": "find"})
        self.assertEqual(status, 400)
        self.assertIn("needs Windows and tools/scheduled_run.ps1", body)
        (daily / ".lock").write_text("123", encoding="utf-8")
        self.assertTrue(json.loads(self.get("/api/run")[1])["running"])

    def test_new_routes_need_token_and_json(self):
        for path in ("/api/note", "/api/contact", "/api/interview", "/api/reminder", "/api/goal", "/api/ranked", "/api/run"):
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
