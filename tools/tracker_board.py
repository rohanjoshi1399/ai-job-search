#!/usr/bin/env python3
"""Local drag-and-drop board for job_search_tracker.csv.

Serves a kanban page on 127.0.0.1 that reads the tracker on every load and
writes status moves straight back to it, so the CSV stays the single source
of truth that /rank, /scrape, /outcome and /html-report already read. The
"Ranked, not started" column lists ranked jobs from job_scraper/seen_jobs.json
that have no tracker row yet, with /rank's stored strengths and gaps; a job
can be queued for the next unattended run, marked not interested, or copied as
an /apply command.

A move follows /outcome's write rules (Steps 3 and 4):
  - only `status`, `notes` and (when a draft is first submitted) `date` change;
    every other field of the row is preserved, and no other row is touched
  - the appended note is dated and contains no commas
  - moving a row off `drafted` stamps `date` with the submission date
  - once an application is submitted, the submitted CV and cover letter are
    copied (never moved) into documents/applications/<stem>/ as cv_draft.tex
    and cover_letter.tex, unless an archived copy is already there
  - `outcome.md` there gets a dated note, is created as an in_progress stub on
    first submission, ticks "Offer received" on an offer, and has its Status set
    on a final resolution (rejected / no_response / offer_declined / hired).
    The format has no withdrawn status, so a withdrawal is noted and Status is
    left as it was
  - every write names the row version the page showed (`rev`); a row that
    another command changed since the page loaded is refused, not overwritten
Fetching a posting that was never archived stays /outcome's job.

"Find & rank" starts tools/scheduled_run.ps1 -DryRun, the same unattended run
Task Scheduler starts each weekday, in scrape-and-rank-only mode: the same tool
allowlist, lock file and spending cap. /api/run reports whether a run is going,
what the last one cost, and the newest digest (served at /digest/<name>).

Markdown files in an application folder open rendered at /view/<stem>/<file>
(saved postings are third-party text, so they render without links), and the
company research cache opens at /research/<slug> for interview prep.

Reminders, logged interviews and the weekly goal live in
documents/applications/board_state.json (tools/board_state.py): /api/note,
/api/contact, /api/interview, /api/reminder and /api/goal write them, /ics/...
serves calendar files, and /stats shows the funnel people post (Sankey) with
response and interview rates. Notes and the contact column are the only other
tracker fields the board writes; note text is stripped of commas, quotes and
line breaks as /outcome Step 4 requires.

Interview details, feedback and follow-ups still belong to /outcome.

Usage:
  python tools/tracker_board.py [--port 8765] [--csv PATH] [--root DIR] [--no-browser]

Stop with Ctrl+C. The server only listens on 127.0.0.1, rejects other Host
headers, and requires a per-run token on writes.
"""

import argparse
import csv
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from md_render import inline, render  # noqa: E402
import board_state as bs  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HTML = Path(__file__).with_name("tracker_board.html")
STATS_HTML = Path(__file__).with_name("tracker_stats.html")
HEADER = ["date", "company", "sector", "role", "role_type", "channel", "status", "contact_person",
          "fit_rating", "notes", "cv_file", "cover_letter_file", "source", "deadline"]
STATUSES = {"drafted", "on_hold", "applied", "interview", "offer", "hired",
            "rejected", "no_response", "offer_declined", "withdrawn"}
LEGACY = {"no response": "no_response", "offer declined": "offer_declined"}
ARCHIVE_FINAL = {"rejected", "no_response", "offer_declined", "hired"}
NOT_SUBMITTED = {"drafted", "on_hold"}
OFFER_STAGE = {"offer", "hired", "offer_declined"}
DIGEST_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(-\d{4})?\.md$")
RUN_LOCK_HOURS = 3  # tools/scheduled_run.ps1 treats a lock older than this as stale
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
STEM_RE = re.compile(r"^[a-z0-9_]+$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9.&'-]*$")
RESEARCH_TTL_DAYS = 30
LOCK = threading.Lock()


class StaleRow(ValueError):
    """The row changed on disk after the page loaded it."""


def row_rev(row):
    return hashlib.sha1("\x1f".join(row).encode("utf-8")).hexdigest()[:12]


def read_rows(csv_path):
    with open(csv_path, encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        raise ValueError("tracker is empty")
    header = rows[0]
    if header[: len(HEADER) - 1] != HEADER[:-1]:
        raise ValueError(f"unexpected tracker header: {header}")
    return header, rows[1:]


def write_rows(csv_path, header, rows):
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows([header] + rows)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(csv_path)), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(buf.getvalue())
    os.replace(tmp, csv_path)


def stem_of(row):
    m = re.match(r"^cv/main_(.+)\.[A-Za-z]+$", row[10] if len(row) > 10 else "")
    return m.group(1) if m and STEM_RE.match(m.group(1)) else None


def as_record(idx, header, row):
    padded = row + [""] * (len(HEADER) - len(row))
    rec = dict(zip(HEADER, padded))
    rec["status"] = LEGACY.get(rec["status"], rec["status"])
    rec["idx"] = idx
    rec["rev"] = row_rev(row)
    stem = stem_of(row)
    rec["stem"] = stem
    folder = ROOT / "documents" / "applications" / stem if stem else None
    rec["files"] = sorted(p.name for p in folder.iterdir() if p.is_file()) if folder and folder.is_dir() else []
    spec = folder / "pack_spec.json" if folder else None
    if spec and spec.is_file():
        try:
            rec["pay"] = json.loads(spec.read_text(encoding="utf-8")).get("pay")
        except ValueError:
            rec["pay"] = None
    slug = research_slug(rec["company"])
    rec["research"] = slug if slug and (ROOT / "company_research" / f"{slug}.json").is_file() else None
    return rec


def research_slug(company):
    """Cache filename rule from 04-job-evaluation.md: lowercase, trim, spaces to hyphens."""
    slug = company.strip().lower().replace(" ", "-")
    return slug if SLUG_RE.match(slug) else None


def norm_company(name):
    words = re.sub(r"[^a-z0-9 ]", " ", str(name).lower()).split()
    return "".join(w for w in words if w not in {"inc", "llc", "corp", "corporation", "co", "ltd"})


def title_words(title):
    return set(re.sub(r"[^a-z0-9 ]", " ", str(title).lower()).split()) - {"the", "and", "of", "for", "a"}


def same_company(a, b):
    return bool(a and b) and (a.startswith(b) or b.startswith(a))


def skip_list():
    """Companies ruled out by hand, as (normalized name, why), from the optional
    skip_companies list in documents/cv/daily_run.json."""
    try:
        items = json.loads((ROOT / "documents" / "cv" / "daily_run.json").read_text(encoding="utf-8")).get("skip_companies")
    except (OSError, ValueError, AttributeError):
        return []
    out = []
    for s in items if isinstance(items, list) else []:
        name, why = (s, "") if isinstance(s, str) else (s.get("name", ""), s.get("why", "")) if isinstance(s, dict) else ("", "")
        if norm_company(name):
            out.append((norm_company(name), str(why)))
    return out


def shortlist(tracked, st=None):
    """Ranked jobs with no tracker row. Company names differ between the scraper and the
    tracker ("PlayerZero, Inc." vs "PlayerZero"), so matching is by normalized company
    prefix plus title overlap; another role at a tracked company stays visible, flagged.
    `excluded` marks a job the ranking ruled out, `skipped` carries the reason when the
    company is on the skip list, and `dismissed` the reason the user gave on the board.
    The board hides all three unless asked. `queued` marks a job the user asked the next
    unattended run to draft. /rank's strengths, gaps and gate verdicts ride along as
    stored, for the details panel: they are untrusted text, rendered as plain text."""
    path = ROOT / "job_scraper" / "seen_jobs.json"
    if not path.exists():
        return []
    seen = json.loads(path.read_text(encoding="utf-8")).get("seen", {})
    tracked = [(norm_company(c), title_words(r)) for c, r in tracked]
    skips = skip_list()
    st = st or bs.load(ROOT)
    queued = {q["key"] for q in st["queue"]}
    dismissed = {d["key"]: d.get("why") or "not interested" for d in st["dismissed_jobs"]}
    out = []
    for key, v in seen.items():
        if v.get("status") != "ranked":
            continue
        co, words = norm_company(v.get("company", "")), title_words(v.get("title", ""))
        same_co = [tw for tc, tw in tracked if same_company(co, tc)]
        if any(len(words & tw) / max(len(words | tw), 1) >= 0.6 for tw in same_co):
            continue
        gaps = " ".join(v.get("gaps") or [])
        excluded = next((label for flag, label in (("ELIGIBILITY FAIL", "Ruled out: eligibility"),
                                                   ("TIMING FAIL", "Ruled out: start date"))
                         if flag in gaps), None)
        if not excluded and v.get("location_verdict", v.get("location")) == "FAIL":
            excluded = "Ruled out: location"
        if not excluded and v.get("language_gate") == "FAIL":
            excluded = "Ruled out: language"
        skipped = next((why or "on the skip list" for name, why in skips if same_company(co, name)), None)
        location = v.get("location") if v.get("location") not in ("PASS", "FAIL", "FLAG") else ""
        out.append({"key": key, "title": v.get("title", ""), "company": v.get("company", ""),
                    "location": location or "", "url": v.get("url", ""),
                    "score": v.get("rank_score"), "verdict": v.get("rank_verdict"), "rank_date": v.get("rank_date"),
                    "deadline": v.get("deadline"), "posted_date": v.get("posted_date"), "portal": v.get("portal"),
                    "strengths": [str(s) for s in v.get("strengths") or []][:3],
                    "gaps": [str(g) for g in v.get("gaps") or []][:3],
                    "location_verdict": v.get("location_verdict", v.get("location") if not location else None),
                    "language_gate": v.get("language_gate"), "language_note": v.get("language_note"),
                    "guard": v.get("guard"),
                    "excluded": excluded, "skipped": skipped, "dismissed": dismissed.get(key),
                    "queued": key in queued, "other_role": bool(same_co)})
    out.sort(key=lambda j: (bool(j["excluded"] or j["skipped"] or j["dismissed"]), not j["queued"], -(j["score"] or 0)))
    return out


def records(csv_path):
    header, rows = read_rows(csv_path)
    return [as_record(i, header, r) for i, r in enumerate(rows)]


def board(csv_path):
    recs = records(csv_path)
    st = bs.load(ROOT)
    today = date.today()
    return {"today": today.isoformat(), "rows": recs,
            "shortlist": shortlist(((r["company"], r["role"]) for r in recs), st),
            "due": bs.due(recs, st, today),
            "interviews": st["interviews"],
            "reminders": [r for r in st["reminders"] if not r.get("done")],
            "goal": bs.stats(recs, st, ROOT, today)["goal"],
            "resolved": sum(1 for r in recs if r["status"] in ARCHIVE_FINAL),
            "run": run_status()}


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>
:root {{ --bg: #f4f5f7; --card: #fff; --ink: #1d2330; --muted: #5d6677; --line: #d5d9e0; --accent: #2f5bd3;
  --chip: #eef1f6; --warn: #a15c00; --warn-bg: #fff1dc; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg: #14171d; --card: #232834; --ink: #e7eaf0; --muted: #9aa3b4;
  --line: #333a48; --accent: #7aa2ff; --chip: #2b3140; --warn: #f0b45c; --warn-bg: #3a2c14; }} }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--ink); font: 15px/1.55 "Segoe UI", system-ui, sans-serif; }}
main {{ max-width: 960px; margin: 0 auto; padding: 20px 16px 48px; }}
.top {{ display: flex; gap: 12px; align-items: baseline; flex-wrap: wrap; margin-bottom: 8px; color: var(--muted); font-size: 13px; }}
.top a, main a {{ color: var(--accent); }}
.doc {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 8px 24px 24px; overflow-wrap: anywhere; }}
h1 {{ font-size: 22px; }} h2 {{ font-size: 17px; margin-top: 28px; border-bottom: 1px solid var(--line); padding-bottom: 4px; }} h3 {{ font-size: 15px; }}
.table {{ overflow-x: auto; }} table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
th, td {{ border: 1px solid var(--line); padding: 6px 8px; text-align: left; vertical-align: top; }}
th {{ background: var(--chip); }}
code {{ background: var(--chip); border-radius: 4px; padding: 1px 4px; font-size: 13px; }}
.code {{ position: relative; }} pre {{ background: var(--chip); border-radius: 8px; padding: 12px 14px; white-space: pre-wrap; }}
pre code {{ background: none; padding: 0; }}
button.copy {{ font: inherit; font-size: 12px; border: 1px solid var(--line); background: var(--card); color: var(--ink);
  border-radius: 6px; padding: 1px 8px; cursor: pointer; margin-left: 6px; }}
.code button.copy {{ position: absolute; top: 8px; right: 8px; }}
li.task {{ list-style: none; margin-left: -18px; }}
.note {{ background: var(--warn-bg); color: var(--warn); border-radius: 8px; padding: 6px 10px; font-size: 13px; }}
.src h2 {{ margin-bottom: 2px; }} .src p {{ margin-top: 4px; }}
</style></head><body><main>
<div class="top"><a href="/">&larr; Board</a><span>{crumb}</span></div>
<div class="doc">{body}</div></main>
<script>
document.addEventListener("click", async (e) => {{
  const b = e.target.closest("button.copy"); if (!b) return;
  const src = b.parentElement.querySelector("pre code") || b.parentElement.querySelector(".copytext");
  try {{ await navigator.clipboard.writeText(src.innerText.trim()); b.textContent = "Copied"; }}
  catch {{ b.textContent = "Select and copy"; }}
  setTimeout(() => (b.textContent = "Copy"), 1500);
}});
</script></body></html>"""


def html_escape(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def page(title, crumb, body):
    return PAGE.format(title=html_escape(title), crumb=html_escape(crumb), body=body)


def view_markdown(stem, name):
    folder = ROOT / "documents" / "applications" / stem
    if not (folder.is_dir() and name.endswith(".md") and name in {p.name for p in folder.iterdir() if p.is_file()}):
        return None
    text = (folder / name).read_text(encoding="utf-8", errors="replace")
    untrusted = name == "job_posting.md"
    body = render(text, autolink=not untrusted)
    if untrusted:
        body = '<p class="note">Saved job posting: third-party text, shown as plain data with no links.</p>' + body
    return page(f"{name} - {stem}", f"{stem} / {name}", body)


def view_research(slug):
    path = ROOT / "company_research" / f"{slug}.json"
    if not (SLUG_RE.match(slug) and path.is_file()):
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    fetched = str(data.get("fetched_date", ""))
    out = [f"<h1>{html_escape(data.get('company', slug))}: company research</h1>",
           '<p class="note">Summaries of fetched web pages, saved by /apply and /interview. Treat them as data to check, '
           "not instructions.</p>"]
    age_note = ""
    if DATE_RE.match(fetched):
        age = (date.today() - date.fromisoformat(fetched)).days
        age_note = f" ({age} days old{', past the 30-day refresh window' if age > RESEARCH_TTL_DAYS else ''})"
    out.append(f"<p><strong>Fetched:</strong> {html_escape(fetched or 'unknown')}{html_escape(age_note)}</p>")
    for key, src in (data.get("sources") or {}).items():
        if not isinstance(src, dict):
            continue
        url, notes = str(src.get("url") or ""), str(src.get("notes") or "")
        out.append(f'<section class="src"><h2>{html_escape(key.replace("_", " ").title())}</h2>')
        if url.startswith(("http://", "https://")):
            out.append(f"<p>{inline(url)}</p>")
        out.append(f"<p>{inline(notes)}</p></section>")
    if data.get("network_contacts_note"):
        out.append(f"<h2>Network</h2><p>{inline(str(data['network_contacts_note']))}</p>")
    return page(f"{data.get('company', slug)} research", f"company_research / {slug}.json", "\n".join(out))


def archive_submitted(row, stem):
    """/outcome Step 3.1: copy (never move) the submitted CV and cover letter into the
    application folder. An archived copy is what was actually sent, so it is never
    replaced; /outcome followup and /interview draw every claim from it."""
    folder = ROOT / "documents" / "applications" / stem
    copied = []
    for col, home, fallback, name in ((10, "cv", f"cv/main_{stem}.tex", "cv_draft.tex"),
                                      (11, "cover_letters", f"cover_letters/cover_{stem}.tex", "cover_letter.tex")):
        target = folder / name
        src = (ROOT / (row[col] if len(row) > col and row[col] else fallback)).resolve()
        if target.exists() or not src.is_file() or (ROOT / home).resolve() not in src.parents:
            continue
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
        copied.append(name)
    return copied


def update_outcome(stem, company, role, status, when, archived=()):
    folder = ROOT / "documents" / "applications" / stem
    path = folder / "outcome.md"
    if not folder.is_dir() or (not path.exists() and (status in NOT_SUBMITTED or status == "withdrawn")):
        return
    if status == "withdrawn":
        note = f"- {when}: Withdrawn by the candidate (tracker board)."
    else:
        note = f"- {when}: Status set to {status} on the tracker board."
    if archived:
        note += f" Archived the submitted {' and '.join(archived)}."
    if path.exists():
        text = path.read_text(encoding="utf-8").rstrip("\n") + "\n" + note + "\n"
    else:
        text = (f"# Outcome: {company} — {role}\n\n**Status:** in_progress\n\n"
                "## Interview stages reached\n- [ ] Phone screen\n- [ ] Technical interview\n"
                "- [ ] Case interview\n- [ ] Final round\n- [ ] Offer received\n\n"
                f"## Notes\n{note}\n")
    if status in OFFER_STAGE:
        text = text.replace("- [ ] Offer received", f"- [x] Offer received ({when})", 1)
    if status != "withdrawn":  # the format has no withdrawn status: leave Status as it was
        text = re.sub(r"\n\n\*\*Date resolved:\*\*[^\n]*", "", text)
        final = status if status in ARCHIVE_FINAL else "in_progress"
        text = re.sub(r"\*\*Status:\*\*[^\n]*", f"**Status:** {final}", text, count=1)
        if final != "in_progress":
            text = text.replace(f"**Status:** {final}\n", f"**Status:** {final}\n\n**Date resolved:** {when}\n", 1)
    path.write_text(text, encoding="utf-8")


def find_row(rows, payload):
    idx = payload.get("idx")
    key = (payload.get("company"), payload.get("role"))
    if not (isinstance(idx, int) and 0 <= idx < len(rows) and (rows[idx][1], rows[idx][3]) == key):
        matches = [i for i, r in enumerate(rows) if (r[1], r[3]) == key]
        if len(matches) != 1:
            raise ValueError("row not found; reload the board")
        idx = matches[0]
    if payload.get("rev") and payload["rev"] != row_rev(rows[idx]):
        raise StaleRow(f"{key[0]} changed on disk since the board loaded (another command updated it). "
                       "The board has reloaded; check the card and try again.")
    return idx


def edit_row(csv_path, payload, change):
    """Apply change(row) to one padded row under the lock and write the tracker atomically."""
    with LOCK:
        header, rows = read_rows(csv_path)
        idx = find_row(rows, payload)
        row = rows[idx] + [""] * (len(HEADER) - len(rows[idx]))
        change(row)
        rows[idx] = row[: max(len(rows[idx]), len(header))]
        write_rows(csv_path, header, rows)
        return row


def add_note(csv_path, payload):
    when = payload.get("date") or date.today().isoformat()
    if not DATE_RE.match(when):
        raise ValueError("date must be YYYY-MM-DD")
    entry = f"followed up {when}" if payload.get("followed_up") else f"{bs.clean(payload.get('text', ''), 200)} {when}"
    if not entry.strip() or entry.strip() == when:
        raise ValueError("note is empty")

    def change(row):
        row[9] = f"{row[9]}; {entry}" if row[9] else entry
    row = edit_row(csv_path, payload, change)
    stem = stem_of(row)
    outcome = ROOT / "documents" / "applications" / stem / "outcome.md" if stem else None
    if outcome and outcome.exists():
        text = outcome.read_text(encoding="utf-8").rstrip("\n") + f"\n- {when}: {entry.rsplit(' ', 1)[0]} (tracker board).\n"
        outcome.write_text(text, encoding="utf-8")


def set_contact(csv_path, payload):
    contact = bs.clean(payload.get("contact", ""), 80)

    def change(row):
        row[7] = contact
    edit_row(csv_path, payload, change)


def log_interview(csv_path, payload):
    header, rows = read_rows(csv_path)
    row = rows[find_row(rows, payload)]  # a stale row is refused before anything is saved
    with bs.LOCK:
        st = bs.load(ROOT)
        _, label, day = bs.add_interview(st, payload)
        bs.save(ROOT, st)
    if LEGACY.get(row[6], row[6]) in ("drafted", "on_hold", "applied"):
        move(csv_path, {**payload, "status": "interview", "date": min(day, date.today()).isoformat()})
    stem = stem_of(row)
    outcome = ROOT / "documents" / "applications" / stem / "outcome.md" if stem else None
    if outcome and outcome.exists():
        text = outcome.read_text(encoding="utf-8")
        box = f"- [ ] {label}"
        if box in text:
            text = text.replace(box, f"- [x] {label} ({day})", 1)
        text = text.rstrip("\n") + f"\n- {date.today()}: {label} logged for {day} (tracker board).\n"
        outcome.write_text(text, encoding="utf-8")


def change_state(payload, kind):
    with bs.LOCK:
        st = bs.load(ROOT)
        if kind == "reminder":
            action = payload.get("action")
            if action == "add":
                bs.add_reminder(st, payload)
            else:
                bs.update_reminder(st, action, payload.get("id"))
        elif kind == "interview_delete":
            st["interviews"] = [i for i in st["interviews"] if i["id"] != payload.get("id")]
        elif kind == "goal":
            goal = int(payload.get("weekly_submitted", 0))
            if not 0 <= goal <= 100:
                raise ValueError("weekly goal must be 0-100")
            st["goal"]["weekly_submitted"] = goal
        bs.save(ROOT, st)


def move(csv_path, payload):
    status = payload.get("status")
    if status not in STATUSES:
        raise ValueError(f"unknown status: {status!r}")
    when = payload.get("date") or date.today().isoformat()
    if not DATE_RE.match(when):
        raise ValueError("date must be YYYY-MM-DD")
    with LOCK:
        header, rows = read_rows(csv_path)
        idx = find_row(rows, payload)
        row = rows[idx] + [""] * (len(HEADER) - len(rows[idx]))
        old = LEGACY.get(row[6], row[6])
        if old == status:
            return
        row[6] = status
        if old in NOT_SUBMITTED and status not in NOT_SUBMITTED | {"withdrawn"}:
            row[0] = when
        entry = f"{status} {when}"
        row[9] = f"{row[9]}; {entry}" if row[9] else entry
        rows[idx] = row[: max(len(rows[idx]), len(header))]
        write_rows(csv_path, header, rows)
        stem = stem_of(row)
    if stem:
        sent = status not in NOT_SUBMITTED and not (status == "withdrawn" and old in NOT_SUBMITTED)
        archived = archive_submitted(row, stem) if sent else []
        update_outcome(stem, row[1], row[3], status, when, archived)


def rank_action(payload):
    """Queue, unqueue, dismiss or restore a ranked job; a dismissal can also rule out the company."""
    key, action = payload.get("key"), payload.get("action")
    path = ROOT / "job_scraper" / "seen_jobs.json"
    job = (json.loads(path.read_text(encoding="utf-8")).get("seen", {}) if path.exists() else {}).get(key)
    if not isinstance(job, dict) or job.get("status") != "ranked":
        raise ValueError("not a ranked job; reload the board")
    with bs.LOCK:
        st = bs.load(ROOT)
        bs.set_ranked(st, action, key, payload.get("why", ""))
        bs.save(ROOT, st)
    if action == "dismiss" and payload.get("rule_out_company"):
        rule_out_company(str(job.get("company", "")), payload.get("why", ""))


def rule_out_company(company, why):
    """Add the company to documents/cv/daily_run.json's skip list, which the unattended run
    (tools/daily_select.py) and this board both read. An existing entry is left as it is."""
    if not norm_company(company):
        raise ValueError("company name missing")
    path = ROOT / "documents" / "cv" / "daily_run.json"
    cfg = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    skips = cfg.setdefault("skip_companies", [])
    names = [s if isinstance(s, str) else s.get("name", "") for s in skips if isinstance(s, (str, dict))]
    if any(same_company(norm_company(company), norm_company(n)) for n in names):
        return
    skips.append({"name": company.strip(), "why": bs.clean(why, 160) or f"ruled out on the tracker board {date.today()}"})
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(cfg, indent=1, ensure_ascii=False))
    os.replace(tmp, path)


LAST_START = {"t": 0.0}


def run_status():
    """The unattended run as the board sees it: tools/scheduled_run.ps1's lock file, the newest
    run summary (reports/daily/run-*.json, claude --output-format json) and the newest digest."""
    daily = ROOT / "reports" / "daily"
    out = {"available": sys.platform == "win32" and (ROOT / "tools" / "scheduled_run.ps1").is_file(),
           "running": False, "started": None, "last": None, "digest": None}
    lock = daily / ".lock"
    if lock.is_file() and time.time() - lock.stat().st_mtime < RUN_LOCK_HOURS * 3600:
        out["running"] = True
        out["started"] = datetime.fromtimestamp(lock.stat().st_mtime).isoformat(timespec="minutes")
    elif time.time() - LAST_START["t"] < 90:  # started from here; the script has not taken the lock yet
        out["running"] = True
        out["started"] = datetime.fromtimestamp(LAST_START["t"]).isoformat(timespec="minutes")
    if daily.is_dir():
        runs = sorted(daily.glob("run-*.json"), key=lambda p: p.stat().st_mtime)
        if runs:
            p = runs[-1]
            last = {"when": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="minutes"), "ok": None}
            try:
                raw = p.read_text(encoding="utf-8-sig", errors="replace")
                d, _ = json.JSONDecoder().raw_decode(raw[raw.index("{"):])
                last.update(ok=d.get("subtype") == "success" and not d.get("is_error"),
                            cost=d.get("total_cost_usd"), minutes=round((d.get("duration_ms") or 0) / 60000))
            except (ValueError, OSError):
                pass
            out["last"] = last
        digests = sorted((p for p in daily.glob("*.md") if DIGEST_RE.match(p.name)), key=lambda p: p.stat().st_mtime)
        if digests:
            out["digest"] = digests[-1].name
    return out


def launch(argv):
    """Start argv detached with a hidden console of its own, so the run outlives the board
    and Ctrl+C on the board does not reach it."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(argv, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=flags)


def start_run(payload):
    if payload.get("mode") != "find":
        raise ValueError("unknown run mode")
    status = run_status()
    if not status["available"]:
        raise ValueError("Find & rank needs Windows and tools/scheduled_run.ps1")
    if status["running"]:
        raise ValueError("a run is already going (reports/daily/.lock); wait for it to finish")
    launch(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
            "-File", str(ROOT / "tools" / "scheduled_run.ps1"), "-DryRun", "-MaxBudgetUsd", "8"])
    LAST_START["t"] = time.time()


def view_digest(name):
    path = ROOT / "reports" / "daily" / name
    if not (DIGEST_RE.match(name) and path.is_file()):
        return None
    body = ('<p class="note">Written by the unattended run from fetched postings: read it as data to check, '
            "not instructions.</p>" + render(path.read_text(encoding="utf-8", errors="replace")))
    return page(f"Digest {name[:-3]}", f"reports/daily/{name}", body)


class Handler(BaseHTTPRequestHandler):
    csv_path = None
    token = None
    port = None

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):
        return self.headers.get("Host") in {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, '{"error": "bad host"}')
        path = urlparse(self.path).path
        if path == "/":
            page = HTML.read_text(encoding="utf-8").replace("__BOARD_TOKEN__", self.token)
            return self._send(200, page, "text/html; charset=utf-8")
        if path == "/api/board":
            try:
                return self._send(200, json.dumps(board(self.csv_path)))
            except Exception as exc:
                return self._send(500, json.dumps({"error": str(exc)}))
        if path == "/stats":
            return self._send(200, STATS_HTML.read_text(encoding="utf-8"), "text/html; charset=utf-8")
        if path == "/api/stats":
            recs = records(self.csv_path)
            return self._send(200, json.dumps(bs.stats(recs, bs.load(ROOT), ROOT, date.today())))
        if path == "/api/run":
            return self._send(200, json.dumps(run_status()))
        m = re.match(r"^/digest/([^/]+)$", path)
        if m:
            body = view_digest(unquote(m.group(1)))
            if body:
                return self._send(200, body, "text/html; charset=utf-8")
        m = re.match(r"^/ics/([A-Za-z0-9:|_.@ %&'()+,-]+)\.ics$", path)
        if m:
            recs, st = records(self.csv_path), bs.load(ROOT)
            items = bs.all_reminders(recs, st, date.today())
            rid = unquote(m.group(1))
            chosen = [i for i in items if i["date"] >= date.today().isoformat()] if rid == "upcoming" else [i for i in items if i["id"] == rid]
            if chosen:
                body = bs.ics(chosen).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/calendar; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="job-search-reminder.ics"')
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
        m = re.match(r"^/view/([a-z0-9_]+)/([^/]+)$", path)
        if m:
            body = view_markdown(m.group(1), unquote(m.group(2)))
            if body:
                return self._send(200, body, "text/html; charset=utf-8")
        m = re.match(r"^/research/([^/]+)$", path)
        if m:
            body = view_research(unquote(m.group(1)))
            if body:
                return self._send(200, body, "text/html; charset=utf-8")
        m = re.match(r"^/files/([a-z0-9_]+)/([^/]+)$", path)
        if m:
            stem, name = m.group(1), unquote(m.group(2))
            folder = ROOT / "documents" / "applications" / stem
            if folder.is_dir() and name in {p.name for p in folder.iterdir() if p.is_file()}:
                ctype = "application/pdf" if name.lower().endswith(".pdf") else "text/plain; charset=utf-8"
                return self._send(200, (folder / name).read_bytes(), ctype)
        return self._send(404, '{"error": "not found"}')

    def do_POST(self):
        # Read the body before any refusal: closing with unread bytes makes Windows reset the
        # connection, and the client then loses the 403. Bounded, since nothing here is large.
        try:
            length = max(0, min(int(self.headers.get("Content-Length", "0")), 1_000_000))
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length else b""
        if (not self._host_ok() or self.headers.get("X-Board-Token") != self.token
                or self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json"):
            return self._send(403, '{"error": "forbidden"}')
        routes = {
            "/api/move": lambda p: move(self.csv_path, p),
            "/api/note": lambda p: add_note(self.csv_path, p),
            "/api/contact": lambda p: set_contact(self.csv_path, p),
            "/api/interview": lambda p: (change_state(p, "interview_delete") if p.get("action") == "delete"
                                         else log_interview(self.csv_path, p)),
            "/api/reminder": lambda p: change_state(p, "reminder"),
            "/api/goal": lambda p: change_state(p, "goal"),
            "/api/ranked": rank_action,
            "/api/run": start_run,
        }
        handler = routes.get(urlparse(self.path).path)
        if handler is None:
            return self._send(404, '{"error": "not found"}')
        try:
            handler(json.loads(raw or b"{}"))
            return self._send(200, json.dumps(board(self.csv_path)))
        except StaleRow as exc:
            return self._send(409, json.dumps({"error": str(exc), "stale": True}))
        except Exception as exc:
            return self._send(400, json.dumps({"error": str(exc)}))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--csv", default=None, help="tracker CSV (default: <root>/job_search_tracker.csv)")
    ap.add_argument("--root", default=None, help="workspace root (default: the repo this tool lives in)")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    global ROOT
    if args.root:
        ROOT = Path(args.root).resolve()
    args.csv = args.csv or str(ROOT / "job_search_tracker.csv")
    if not Path(args.csv).exists():
        sys.exit(f"tracker not found: {args.csv}")
    Handler.csv_path, Handler.token, Handler.port = args.csv, secrets.token_urlsafe(24), args.port
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Tracker board on {url} (editing {args.csv}). Ctrl+C to stop.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
