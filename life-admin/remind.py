#!/usr/bin/env python3
"""life-admin: turn a list of dates into reminders that arrive in time.

Reads a list of things that expire, renew or fall due, works out the next
occurrence of each one, and tells you about the ones entering their warning
window. It writes an always-current AGENDA.md and, when it has a GitHub token,
opens one issue per reminder so the notification lands in your inbox.

A reminder is announced once per warning step per occurrence. Miss a week of
runs and you get one message, not seven.

Standard library only: nothing to pip install, nothing to pin, nothing to rot.
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SAMPLE_CONFIG = HERE / "reminders.json"
LOCAL_CONFIG = HERE / "reminders.local.json"
STATE_PATH = HERE / "state" / "notified.json"
AGENDA_PATH = HERE / "AGENDA.md"

API_ROOT = "https://api.github.com"
USER_AGENT = "daily-automations-life-admin/1.0"

WARN_DEFAULT = [30, 14, 7, 1, 0]
# Once something is overdue, nag again after a week and after a month.
OVERDUE_STEPS = [-7, -30]
EVERY_VALUES = ("none", "weekly", "monthly", "yearly")
KEEP_STATE_DAYS = 400


# ------------------------------------------------------------------- config

def load_config(explicit=None):
    """Return (config, where it came from).

    Order: --config, then the REMINDERS_JSON environment variable (hand it a
    repo secret to keep real dates out of a public repo), then
    reminders.local.json, then the committed sample.
    """
    if explicit:
        path = Path(explicit).expanduser().resolve()
        return _read_json(path), str(path)

    raw = os.environ.get("REMINDERS_JSON", "").strip()
    if raw:
        try:
            return json.loads(raw), "REMINDERS_JSON environment variable"
        except json.JSONDecodeError as exc:
            raise SystemExit("REMINDERS_JSON is not valid JSON: {}".format(exc))

    for path in (LOCAL_CONFIG, SAMPLE_CONFIG):
        if path.exists():
            return _read_json(path), path.name
    raise SystemExit("No reminder list found. Expected {}.".format(SAMPLE_CONFIG))


def _read_json(path):
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise SystemExit("No such reminder list: {}".format(path))
    except json.JSONDecodeError as exc:
        raise SystemExit("{} is not valid JSON: {}".format(path, exc))


def load_state():
    if not STATE_PATH.exists():
        return {"announced": {}, "last_run": None}
    try:
        with STATE_PATH.open(encoding="utf-8") as fh:
            state = json.load(fh)
    except json.JSONDecodeError:
        print("! notified.json is corrupt; starting from an empty state", file=sys.stderr)
        return {"announced": {}, "last_run": None}
    state.setdefault("announced", {})
    state.setdefault("last_run", None)
    return state


def save_state(state, today, live_keys=()):
    """Write state, dropping occurrences long past so the file stays readable.

    Anything still on the list is kept whatever its date. An item that went
    overdue more than KEEP_STATE_DAYS ago is still being tracked, and forgetting
    that it was announced would announce it again on every single run.
    """
    cutoff = today - timedelta(days=KEEP_STATE_DAYS)
    live = set(live_keys)
    pruned = {}
    for key, steps in state["announced"].items():
        if key not in live:
            due = key.rsplit("@", 1)[-1]
            try:
                if date.fromisoformat(due) < cutoff:
                    continue
            except ValueError:
                pass
        pruned[key] = sorted(set(steps), reverse=True)
    state["announced"] = dict(sorted(pruned.items()))
    state["last_run"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STATE_PATH.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


# -------------------------------------------------------------------- dates

def clamp_day(year, month, day):
    """The 31st in a 30-day month means the 30th; February means the 28th or 29th."""
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def next_occurrence(anchor, every, today):
    """The first occurrence on or after today, or the anchor if it is still ahead."""
    if every not in EVERY_VALUES:
        raise ValueError(
            "unknown repeat {!r}; use one of {}".format(every, ", ".join(EVERY_VALUES))
        )
    if every == "none" or anchor >= today:
        return anchor
    if every == "weekly":
        return today + timedelta(days=(anchor.weekday() - today.weekday()) % 7)
    if every == "monthly":
        candidate = clamp_day(today.year, today.month, anchor.day)
        if candidate < today:
            year = today.year + 1 if today.month == 12 else today.year
            month = today.month % 12 + 1
            candidate = clamp_day(year, month, anchor.day)
        return candidate
    candidate = clamp_day(today.year, anchor.month, anchor.day)
    if candidate < today:
        candidate = clamp_day(today.year + 1, anchor.month, anchor.day)
    return candidate


def steps_crossed(days_until, warn):
    """Warning steps this item has reached, widest first."""
    steps = sorted(set(list(warn) + OVERDUE_STEPS), reverse=True)
    return [step for step in steps if days_until <= step]


def when_phrase(days_until):
    if days_until == 0:
        return "today"
    if days_until == 1:
        return "tomorrow"
    if days_until > 1:
        return "in {} days".format(days_until)
    if days_until == -1:
        return "overdue since yesterday"
    return "overdue by {} days".format(abs(days_until))


# ------------------------------------------------------------------ the work

class Reminder(object):
    """One reminder resolved against a single day."""

    def __init__(self, item, index, today, warn_default):
        self.label = str(item.get("label") or "reminder {}".format(index + 1)).strip()
        self.category = str(item.get("category") or "general").strip()
        self.notes = str(item.get("notes") or "").strip()
        self.every = str(item.get("every") or "none").strip().lower()
        self.redact = bool(item.get("redact"))
        self.anchor = date.fromisoformat(str(item["date"]).strip())
        warn = item.get("warn") or warn_default
        self.warn = sorted(set(int(day) for day in warn), reverse=True)
        self.due = next_occurrence(self.anchor, self.every, today)
        self.days_until = (self.due - today).days
        self.key = "{}@{}".format(self._key_name(), self.due.isoformat())

    def _key_name(self):
        """The name this item goes by in state/notified.json, which is public.

        A redacted item must not spell its label out in the state file either,
        so it is keyed by category and a short digest: stable between runs,
        and not a label.
        """
        if self.redact:
            digest = hashlib.sha1(self.label.encode("utf-8")).hexdigest()[:8]
            return "{}-{}".format(_slug(self.category), digest)
        return _slug(self.label)

    @property
    def display(self):
        """What is safe to write down. Redacted items show their category only."""
        if self.redact:
            return "{} item (details kept private)".format(self.category)
        return self.label

    @property
    def repeat_note(self):
        return "one-off" if self.every == "none" else self.every


def _slug(text):
    flat = "".join(char.lower() if char.isalnum() else "-" for char in text)
    return "-".join(part for part in flat.split("-") if part) or "reminder"


def resolve(config, today):
    """Build every reminder we can, plus a note for every one we cannot.

    A single malformed entry must not cost you the other twenty reminders.
    """
    warn_default = config.get("warn_default") or WARN_DEFAULT
    reminders, problems = [], []
    for index, item in enumerate(config.get("reminders", [])):
        try:
            reminders.append(Reminder(item, index, today, warn_default))
        except Exception as exc:  # one bad entry, keep the rest
            name = item.get("label") if isinstance(item, dict) else None
            problems.append("{}: {}".format(name or "entry {}".format(index + 1), exc))
    reminders.sort(key=lambda rem: (rem.due, rem.label))
    return reminders, problems


def due_announcements(reminders, state):
    """Reminders that have entered a warning step not announced yet.

    Returns the tightest step reached for each, and marks the wider ones as
    announced too, so a gap in runs produces one message rather than a pile.
    """
    out = []
    for rem in reminders:
        crossed = steps_crossed(rem.days_until, rem.warn)
        if not crossed:
            continue
        done = set(state["announced"].get(rem.key, []))
        fresh = [step for step in crossed if step not in done]
        if not fresh:
            continue
        state["announced"][rem.key] = sorted(done | set(crossed), reverse=True)
        out.append((rem, min(fresh)))
    return out


# -------------------------------------------------------------------- output

def _row(rem, late=False):
    """One table row. The overdue table's column is How late, so it says just that."""
    when = "{} days".format(abs(rem.days_until)) if late else when_phrase(rem.days_until)
    return "| {} | {} | {} | {} |".format(rem.display, rem.due.isoformat(), when, rem.repeat_note)


def render_agenda(reminders, problems, today, source):
    overdue = [rem for rem in reminders if rem.days_until < 0]
    soon = [rem for rem in reminders if 0 <= rem.days_until <= 45]
    later = [rem for rem in reminders if rem.days_until > 45]

    lines = [
        "# Agenda",
        "",
        "Everything life-admin is watching, as of {}.".format(today.isoformat()),
        "Written by `remind.py`. Edit `reminders.json`, not this file.",
        "",
    ]

    if overdue:
        lines += ["## Overdue", "", "| What | Was due | How late | Repeats |", "| --- | --- | --- | --- |"]
        lines += [_row(rem, late=True) for rem in overdue]
        lines.append("")

    lines += ["## Next 45 days", "", "| What | Due | When | Repeats |", "| --- | --- | --- | --- |"]
    lines += [_row(rem) for rem in soon] or ["| _nothing due_ | | | |"]
    lines.append("")

    if later:
        lines += ["## Further out", "", "| What | Due | When | Repeats |", "| --- | --- | --- | --- |"]
        lines += [_row(rem) for rem in later]
        lines.append("")

    if problems:
        lines += ["## Entries this run could not read", ""]
        lines += ["- {}".format(problem) for problem in problems]
        lines.append("")

    lines += [
        "---",
        "",
        "{} reminder(s) from {}. Written {}.".format(
            len(reminders), source, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        ),
        "",
    ]
    return "\n".join(lines)


def issue_title(rem):
    return "[reminder] {} - {}".format(rem.display, when_phrase(rem.days_until))


def issue_body(rem, step):
    step_line = (
        "- Warning step: {} day(s) ahead".format(step)
        if step >= 0
        else "- Overdue nag: {} day(s) late".format(abs(step))
    )
    lines = [
        "**{}** is due **{}** ({}).".format(rem.display, rem.due.isoformat(), when_phrase(rem.days_until)),
        "",
        "- Category: {}".format(rem.category),
        "- Repeats: {}".format(rem.repeat_note),
        step_line,
    ]
    if rem.notes and not rem.redact:
        lines += ["", "> {}".format(rem.notes)]
    lines += [
        "",
        "Close this issue once it is handled. The next warning step, and the next",
        "occurrence if it repeats, will open a new one.",
    ]
    return "\n".join(lines)


def _api(url, token, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method="POST" if data else "GET",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer {}".format(token),
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def open_issue_titles(repo, token):
    try:
        issues = _api("{}/repos/{}/issues?state=open&per_page=100".format(API_ROOT, repo), token)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print("! could not list open issues ({}); duplicate check skipped".format(exc), file=sys.stderr)
        return set()
    return set(str(issue.get("title", "")) for issue in issues if isinstance(issue, dict))


def announce(items, repo, token):
    """Open one issue per reminder. A failure costs that reminder, not the run."""
    existing = open_issue_titles(repo, token)
    sent = 0
    for rem, step in items:
        title = issue_title(rem)
        if title in existing:
            print("  = already open: {}".format(title))
            continue
        try:
            created = _api(
                "{}/repos/{}/issues".format(API_ROOT, repo),
                token,
                {"title": title, "body": issue_body(rem, step)},
            )
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print("! could not open an issue for {}: {}".format(rem.display, exc), file=sys.stderr)
            continue
        print("  + issue #{}: {}".format(created.get("number", "?"), title))
        sent += 1
    return sent


# ---------------------------------------------------------------------- main

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Turn a list of dates into timely reminders.")
    parser.add_argument("--config", help="reminder list to read instead of the usual search")
    parser.add_argument("--today", help="pretend today is this YYYY-MM-DD (for testing)")
    parser.add_argument("--dry-run", action="store_true", help="print what would happen, write nothing")
    parser.add_argument("--no-issues", action="store_true", help="write the agenda, open no issues")
    parser.add_argument(
        "--check",
        action="store_true",
        help="read the list, report anything unreadable, exit non-zero if there is. Writes nothing.",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    today = date.fromisoformat(args.today) if args.today else date.today()

    config, source = load_config(args.config)
    reminders, problems = resolve(config, today)

    print("life-admin {} - {} reminder(s) from {}".format(today.isoformat(), len(reminders), source))
    for problem in problems:
        print("! unreadable entry - {}".format(problem), file=sys.stderr)

    if args.check:
        for rem in reminders:
            print(
                "  ok  {}  {}  ({}, warns at {})".format(
                    rem.due.isoformat(),
                    rem.display,
                    rem.repeat_note,
                    ", ".join(str(day) for day in rem.warn),
                )
            )
        if problems:
            print("{} entry(ies) could not be read; fix them before uploading".format(len(problems)))
            return 1
        print("every entry reads cleanly")
        return 0

    state = load_state()

    announcements = due_announcements(reminders, state)
    for rem, step in announcements:
        print("  * {}: {} (step {})".format(rem.display, when_phrase(rem.days_until), step))
    if not announcements:
        print("  nothing has entered a warning window today")

    agenda = render_agenda(reminders, problems, today, source)

    if args.dry_run:
        print("")
        print("--- agenda (not written) ---")
        print(agenda)
        return 0

    AGENDA_PATH.write_text(agenda, encoding="utf-8", newline="\n")
    print("  agenda written to {}".format(AGENDA_PATH.name))

    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if announcements and not args.no_issues and token and repo:
        announce(announcements, repo, token)
    elif announcements and not args.no_issues:
        print("  no GITHUB_TOKEN/GITHUB_REPOSITORY, so no issues opened; the agenda has it all")

    save_state(state, today, [rem.key for rem in reminders])

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(
                "### life-admin\n\n{} reminder(s) announced, {} watched.\n".format(
                    len(announcements), len(reminders)
                )
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
