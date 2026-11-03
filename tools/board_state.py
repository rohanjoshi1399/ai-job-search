#!/usr/bin/env python3
"""Reminders, interviews, the weekly goal and stats for the tracker board.

The tracker CSV stays the record of applications. What a CSV row has no column
for - reminders, logged interview rounds, a weekly goal, dismissed nudges -
lives in documents/applications/board_state.json (gitignored personal data; a
file beside the application folders, so nothing that globs those folders
mistakes it for one).

Reminders come from two places:
  custom    added by the user, stored here
  derived   computed on every read from the tracker and the stored interviews:
            follow-up  an `applied` row 10+ days quiet with fewer than two
                       `followed up` markers (the rule /outcome Step 2b uses)
            deadline   3 days before and on the deadline of a row not yet sent
            interview  the day before (prepare) and the day of a logged round
Derived reminders cannot be marked done, only dismissed; logging a follow-up
or an interview naturally moves or clears them.

Used by tools/tracker_board.py (board and stats pages), tools/remind.py
(Windows notifications and the daily digest).
"""

import json
import os
import re
import secrets
import statistics
import tempfile
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

LOCK = threading.Lock()
DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
TIME_RE = re.compile(r"^\d{2}:\d{2}$")
FOLLOWUP_DAYS = 10
STAGES = {"phone_screen": "Phone screen", "technical": "Technical interview", "case": "Case interview",
          "final": "Final round", "other": "Other round"}
OFFER = {"offer", "hired", "offer_declined"}
SUBMITTED = {"applied", "interview", "offer", "hired", "rejected", "no_response", "offer_declined"}


def state_path(root):
    return Path(root) / "documents" / "applications" / "board_state.json"


def load(root):
    path = state_path(root)
    st = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    st.setdefault("version", 1)
    st.setdefault("goal", {"weekly_submitted": 0})
    for key in ("reminders", "interviews", "dismissed"):
        st.setdefault(key, [])
    return st


def save(root, st):
    path = state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        json.dump(st, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def clean(text, limit=300):
    """Text safe for the tracker's notes column: no commas, quotes or line breaks (/outcome Step 4)."""
    text = re.sub(r"\s+", " ", str(text).replace(",", " -").replace('"', "").replace("'", "'")).strip()
    return text[:limit]


def parse_day(value):
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def last_touch(rec, today):
    days = [d for d in (parse_day(x) for x in [rec.get("date"), *DATE_RE.findall(rec.get("notes", ""))]) if d and d <= today]
    return max(days) if days else None


def key_of(rec):
    return f"{rec.get('company', '')}|{rec.get('role', '')}"


def _item(kind, rid, day, text, rec=None, time=None, custom=False, extra=None):
    out = {"id": rid, "kind": kind, "date": day.isoformat(), "time": time, "text": text, "custom": custom,
           "company": (rec or {}).get("company"), "role": (rec or {}).get("role"), "stem": (rec or {}).get("stem")}
    out.update(extra or {})
    return out


def derived(records, st, today):
    items = []
    for rec in records:
        status = rec.get("status")
        if status == "applied":
            touched = last_touch(rec, today)
            sent = rec.get("notes", "").count("followed up ")
            if touched and sent < 2:
                due = touched + timedelta(days=FOLLOWUP_DAYS)
                quiet = (today - touched).days
                items.append(_item("followup", f"followup:{key_of(rec)}:{due}", due,
                                   f"Follow up on {rec['company']} ({quiet} days quiet, {sent} follow-up{'s' if sent != 1 else ''} sent)", rec))
        if status in ("drafted", "on_hold"):
            dl = parse_day(rec.get("deadline", ""))
            if dl:
                for offset, label in ((3, "closes in 3 days"), (0, "closes today")):
                    items.append(_item("deadline", f"deadline:{key_of(rec)}:{dl - timedelta(days=offset)}", dl - timedelta(days=offset),
                                       f"{rec['company']} application {label} ({dl})", rec))
    by_key = {key_of(r): r for r in records}
    for iv in st["interviews"]:
        day = parse_day(iv.get("date"))
        if not day:
            continue
        rec = by_key.get(f"{iv.get('company')}|{iv.get('role')}") or {"company": iv.get("company"), "role": iv.get("role"), "stem": iv.get("stem")}
        stage = STAGES.get(iv.get("stage"), "Interview")
        when = f" at {iv['time']}" if iv.get("time") else ""
        items.append(_item("prep", f"prep:{iv['id']}", day - timedelta(days=1),
                           f"Prepare for {rec['company']} {stage.lower()} tomorrow{when}", rec, extra={"interview": iv["id"]}))
        items.append(_item("interview", f"interview:{iv['id']}", day, f"{rec['company']} {stage.lower()} today{when}", rec,
                           time=iv.get("time"), extra={"interview": iv["id"]}))
    dismissed = set(st["dismissed"])
    return [i for i in items if i["id"] not in dismissed]


def all_reminders(records, st, today):
    by_key = {key_of(r): r for r in records}
    custom = []
    for r in st["reminders"]:
        day = parse_day(r.get("date"))
        if day and not r.get("done"):
            rec = by_key.get(f"{r.get('company')}|{r.get('role')}") or {"company": r.get("company"), "role": r.get("role"), "stem": r.get("stem")}
            custom.append(_item("custom", r["id"], day, r.get("text", ""), rec, time=r.get("time"), custom=True))
    return sorted(custom + derived(records, st, today), key=lambda i: (i["date"], i["time"] or ""))


def due(records, st, today, horizon=7):
    buckets = {"overdue": [], "today": [], "upcoming": [], "later": []}
    for item in all_reminders(records, st, today):
        day = date.fromisoformat(item["date"])
        if day < today:
            buckets["overdue"].append(item)
        elif day == today:
            buckets["today"].append(item)
        elif day <= today + timedelta(days=horizon):
            buckets["upcoming"].append(item)
        elif item["custom"] or item["kind"] in ("prep", "interview"):
            buckets["later"].append(item)
    return buckets


def add_reminder(st, payload):
    day = parse_day(payload.get("date"))
    if not day:
        raise ValueError("reminder needs a date (YYYY-MM-DD)")
    time = payload.get("time") or None
    if time and not TIME_RE.match(time):
        raise ValueError("time must be HH:MM")
    text = clean(payload.get("text", ""), 200)
    if not text:
        raise ValueError("reminder needs some text")
    rid = "r" + secrets.token_hex(4)
    st["reminders"].append({"id": rid, "company": payload.get("company"), "role": payload.get("role"), "stem": payload.get("stem"),
                            "date": day.isoformat(), "time": time, "text": text, "done": False,
                            "created": date.today().isoformat()})
    return rid


def update_reminder(st, action, rid):
    if action == "dismiss":
        if rid not in st["dismissed"]:
            st["dismissed"].append(rid)
        return
    match = [r for r in st["reminders"] if r["id"] == rid]
    if not match:
        raise ValueError("unknown reminder")
    if action == "done":
        match[0]["done"] = True
    elif action == "delete":
        st["reminders"].remove(match[0])
    else:
        raise ValueError("unknown reminder action")


def add_interview(st, payload):
    day = parse_day(payload.get("date"))
    if not day:
        raise ValueError("interview needs a date (YYYY-MM-DD)")
    stage = payload.get("stage", "other")
    if stage not in STAGES:
        raise ValueError("unknown interview stage")
    time = payload.get("time") or None
    if time and not TIME_RE.match(time):
        raise ValueError("time must be HH:MM")
    iid = "i" + secrets.token_hex(4)
    st["interviews"].append({"id": iid, "company": payload.get("company"), "role": payload.get("role"), "stem": payload.get("stem"),
                             "date": day.isoformat(), "time": time, "stage": stage, "notes": clean(payload.get("notes", ""), 300)})
    return iid, STAGES[stage], day


# ---------------------------------------------------------------- calendar (.ics)

def _ics_escape(text):
    return str(text).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line):
    out, raw = [], line.encode("utf-8")
    while len(raw) > 74:
        cut = 74
        while cut and (raw[cut] & 0xC0) == 0x80:  # never split a UTF-8 sequence
            cut -= 1
        out.append(raw[:cut].decode("utf-8"))
        raw = b" " + raw[cut:]
    out.append(raw.decode("utf-8"))
    return "\r\n".join(out)


def ics(items, name="Job search reminders"):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//ai-job-search//tracker board//EN", "CALSCALE:GREGORIAN",
             f"X-WR-CALNAME:{_ics_escape(name)}"]
    for it in items:
        day = it["date"].replace("-", "")
        lines += ["BEGIN:VEVENT", f"UID:{_ics_escape(it['id'])}@ai-job-search", f"DTSTAMP:{stamp}"]
        if it.get("time"):
            start = f"{day}T{it['time'].replace(':', '')}00"
            end_dt = datetime.strptime(start, "%Y%m%dT%H%M%S") + timedelta(minutes=45)
            lines += [f"DTSTART:{start}", f"DTEND:{end_dt.strftime('%Y%m%dT%H%M%S')}"]
            trigger = "-PT30M"
        else:
            nxt = (date.fromisoformat(it["date"]) + timedelta(days=1)).strftime("%Y%m%d")
            lines += [f"DTSTART;VALUE=DATE:{day}", f"DTEND;VALUE=DATE:{nxt}"]
            trigger = "PT9H"
        desc = " / ".join(x for x in (it.get("company"), it.get("role")) if x)
        lines += [f"SUMMARY:{_ics_escape(it['text'])}", f"DESCRIPTION:{_ics_escape(desc)}",
                  "BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_ics_escape(it['text'])}",
                  f"TRIGGER{';RELATED=START' if not it.get('time') else ''}:{trigger}", "END:VALARM", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(l) for l in lines) + "\r\n"


# ---------------------------------------------------------------- stats

def _week(d):
    return d - timedelta(days=d.weekday())


def submitted(rec):
    if rec.get("status") in SUBMITTED:
        return True
    return rec.get("status") == "withdrawn" and bool(re.search(r"\bapplied \d{4}-\d{2}-\d{2}", rec.get("notes", "")))


def interviewed(rec, st, root):
    if rec.get("status") in {"interview"} | OFFER:
        return True
    if any(iv.get("company") == rec.get("company") and iv.get("role") == rec.get("role") for iv in st["interviews"]):
        return True
    if re.search(r"\binterview \d{4}-\d{2}-\d{2}", rec.get("notes", "")):
        return True
    stem = rec.get("stem")
    outcome = Path(root) / "documents" / "applications" / stem / "outcome.md" if stem else None
    return bool(outcome and outcome.exists() and re.search(r"- \[x\] (Phone screen|Technical|Case|Final)", outcome.read_text(encoding="utf-8")))


def stats(records, st, root, today):
    subs = [r for r in records if submitted(r)]
    flows = {}

    def flow(a, b):
        flows[(a, b)] = flows.get((a, b), 0) + 1

    response_days, offers, interviews_n, responded = [], 0, 0, 0
    for r in subs:
        s, iv = r["status"], interviewed(r, st, root)
        if iv:
            interviews_n += 1
            flow("Applied", "Interviewed")
            if s in OFFER:
                offers += 1
                flow("Interviewed", "Offer")
                flow("Offer", {"hired": "Accepted", "offer_declined": "Declined"}.get(s, "Offer pending"))
            elif s == "interview":
                flow("Interviewed", "In progress")
            else:
                flow("Interviewed", {"withdrawn": "Withdrew after interview"}.get(s, "Rejected after interview"))
        else:
            flow("Applied", {"applied": "Waiting", "no_response": "No response", "rejected": "Rejected",
                             "withdrawn": "Withdrew"}.get(s, "Waiting"))
        if iv or s in ("rejected",) or s in OFFER:
            responded += 1
            start = parse_day(r.get("date"))
            marks = [parse_day(d) for word, d in re.findall(r"\b(rejected|interview|offer|hired) (\d{4}-\d{2}-\d{2})", r.get("notes", ""))]
            marks += [parse_day(i["date"]) for i in st["interviews"] if i.get("company") == r.get("company") and i.get("role") == r.get("role")]
            marks = sorted(m for m in marks if m and start and m >= start)
            if marks:
                response_days.append((marks[0] - start).days)
    n = len(subs)
    pct = lambda x: round(100 * x / n) if n else 0
    goal = int(st["goal"].get("weekly_submitted") or 0)
    weeks = {}
    for r in subs:
        d = parse_day(r.get("date"))
        if d:
            weeks[_week(d)] = weeks.get(_week(d), 0) + 1
    this_week = _week(today)
    series = [{"week": (this_week - timedelta(weeks=k)).isoformat(), "count": weeks.get(this_week - timedelta(weeks=k), 0)}
              for k in range(11, -1, -1)]
    streak = 0
    if goal:
        w = this_week if weeks.get(this_week, 0) >= goal else this_week - timedelta(weeks=1)
        while weeks.get(w, 0) >= goal:
            streak += 1
            w -= timedelta(weeks=1)
    by_type = {}
    for r in subs:
        t = r.get("role_type") or "Other"
        row = by_type.setdefault(t, {"type": t, "submitted": 0, "interviewed": 0})
        row["submitted"] += 1
        row["interviewed"] += int(interviewed(r, st, root))
    links = [{"source": a, "target": b, "value": v} for (a, b), v in flows.items()]
    order = ["Applied", "Interviewed", "Offer"]
    sankeymatic = "\n".join(f"{a} [{v}] {b}" for (a, b), v in sorted(flows.items(), key=lambda kv: (order.index(kv[0][0]), -kv[1])))
    return {
        "today": today.isoformat(),
        "kpis": {"submitted": n, "to_submit": sum(1 for r in records if r.get("status") == "drafted"),
                 "active": sum(1 for r in records if r.get("status") in ("applied", "interview", "offer")),
                 "response_rate": pct(responded), "interview_rate": pct(interviews_n), "offer_rate": pct(offers),
                 "median_days_to_response": statistics.median(response_days) if response_days else None,
                 "interviews": interviews_n, "offers": offers},
        "goal": {"weekly_submitted": goal, "this_week": weeks.get(this_week, 0), "streak": streak},
        "weekly": series,
        "by_role_type": sorted(by_type.values(), key=lambda x: -x["submitted"]),
        "sankey": links,
        "sankeymatic": sankeymatic,
    }
