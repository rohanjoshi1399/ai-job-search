#!/usr/bin/env python3
"""List the tracker board's due reminders, or show them as a Windows notification.

Same reminders the board's Due panel shows (tools/board_state.py): custom
reminders, follow-up nudges 10 days after the last update, deadlines, and the
day before and day of logged interviews. Used three ways:

  python tools/remind.py            plain text, e.g. for the /daily-run digest
  python tools/remind.py --json     the buckets as JSON
  python tools/remind.py --toast    a Windows toast when anything is overdue or due today
                                    (built-in WinRT notifications through PowerShell, no extra modules)

Morning notification (register once, from PowerShell in the repo folder; pythonw
runs it without a console window):
  $py = Join-Path (Split-Path (Get-Command python).Source) "pythonw.exe"
  $a = New-ScheduledTaskAction -Execute $py -Argument "tools\\remind.py --toast" -WorkingDirectory (Get-Location)
  $t = New-ScheduledTaskTrigger -Daily -At 8:57am
  Register-ScheduledTask -TaskName "AI Job Search - reminders" -Action $a -Trigger $t `
    -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable)
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parent))
import board_state as bs  # noqa: E402
import tracker_board as tb  # noqa: E402

APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
TOAST_PS = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($env:REMIND_TOAST_XML)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:REMIND_APP_ID).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
"""


def buckets(root=None, csv_path=None, today=None):
    root = Path(root) if root else tb.ROOT
    tb.ROOT = root
    recs = tb.records(csv_path or root / "job_search_tracker.csv")
    return bs.due(recs, bs.load(root), today or date.today())


def plain(due):
    urgent = due["overdue"] + due["today"]
    lines = [f"Reminders: {len(due['overdue'])} overdue, {len(due['today'])} due today, {len(due['upcoming'])} in the next 7 days."]
    for title, key in (("Overdue", "overdue"), ("Today", "today"), ("Next 7 days", "upcoming")):
        if due[key]:
            lines.append(f"\n{title}:")
            lines += [f"- {i['date']}{' ' + i['time'] if i.get('time') else ''}: {i['text']}" for i in due[key]]
    if not urgent and not due["upcoming"]:
        lines.append("Nothing due.")
    return "\n".join(lines)


def toast(due):
    urgent = due["overdue"] + due["today"]
    if not urgent:
        return False
    title = f"{len(urgent)} job search reminder{'s' if len(urgent) != 1 else ''} due"
    body = [i["text"] for i in urgent[:3]]
    if len(urgent) > 3:
        body[-1] += f" (+{len(urgent) - 3} more)"
    texts = "".join(f"<text>{escape(t)}</text>" for t in [title, *body[:2]])
    xml = f'<toast><visual><binding template="ToastGeneric">{texts}<text placement="attribution">Open: python tools/tracker_board.py</text></binding></visual></toast>'
    env = {**os.environ, "REMIND_TOAST_XML": xml, "REMIND_APP_ID": APP_ID}
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", TOAST_PS], env=env, check=True,
                   capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description="Show the tracker board's due reminders.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--toast", action="store_true")
    ap.add_argument("--root", default=None, help="workspace root (default: this repo)")
    ap.add_argument("--today", default=None, help="YYYY-MM-DD, for testing")
    args = ap.parse_args(argv)
    due = buckets(args.root, today=date.fromisoformat(args.today) if args.today else None)
    if args.toast:
        shown = toast(due)
        print("toast shown" if shown else "nothing due; no toast")
    elif args.json:
        print(json.dumps(due, indent=1))
    else:
        print(plain(due))
    return 0


if __name__ == "__main__":
    sys.exit(main())
