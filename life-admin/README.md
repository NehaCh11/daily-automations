# life-admin

A list of dates in, a timely nudge out.

Every renewal, expiry and deadline goes into `reminders.json` once. Each morning
the job works out the next occurrence of each one, and for anything that has
just entered its warning window it opens a GitHub issue, which GitHub emails to
you. It also rewrites [AGENDA.md](AGENDA.md), so there is always one page
showing what is coming and what is already late.

Why issues rather than a calendar or a mail server: an issue needs no account,
no SMTP password and no API key beyond the token the workflow already has, it
arrives in your inbox, and closing it is how you mark the thing done.

## What it handles

- **Repeats**: `none`, `weekly`, `monthly`, `yearly`. A monthly reminder
  anchored on the 31st lands on the 30th in a short month, and a yearly one
  anchored on 29 February lands on the 28th in a common year.
- **Several warnings per item**, so a passport can warn you at 180, 90, 30 and 7
  days while a bin collection warns you the night before.
- **Overdue nags**: after the due date passes, it says so once a week later and
  once a month later, then stops.
- **Catch-up without spam**: each warning step is announced once per occurrence.
  If the job does not run for a fortnight, you get the tightest warning you
  missed, not every warning you missed.
- **Private entries**: `"redact": true` keeps the label out of the agenda and
  out of the issue title. The date is still tracked; the public side says only
  `documents item (details kept private)`.

## Configure

```json
{
  "warn_default": [30, 14, 7, 1, 0],
  "reminders": [
    {
      "label": "Passport expires",
      "date": "2028-04-18",
      "every": "none",
      "category": "documents",
      "warn": [180, 90, 30, 7],
      "notes": "Some countries want six months of validity left."
    }
  ]
}
```

| Field | Meaning |
| --- | --- |
| `label` | What to call it. Required in practice, falls back to `reminder N`. |
| `date` | `YYYY-MM-DD`. For a repeating item this is the anchor: the day of the month, the weekday, or the month and day it falls on. |
| `every` | `none`, `weekly`, `monthly` or `yearly`. Default `none`. |
| `category` | Free text. Used for grouping, and it is all a redacted item reveals. |
| `warn` | Days before the date to speak up. Default `warn_default`. |
| `notes` | One line of context, included in the issue. |
| `redact` | `true` to keep the label out of anything public. |

Where the list is read from, first match wins:

1. `--config PATH`
2. the `REMINDERS_JSON` environment variable, holding the JSON itself. Set it as
   a repository secret and your real dates never sit in the repo.
3. `reminders.local.json`, which is gitignored. This is the one to use locally.
4. `reminders.json`, the committed sample.

## Run it

```bash
# what it would say today, writing nothing
python life-admin/remind.py --dry-run

# write the agenda and the state file, open no issues
python life-admin/remind.py --no-issues

# what will it say on a given day
python life-admin/remind.py --today 2026-12-09 --dry-run

# your own list, kept off the repo
python life-admin/remind.py --config ~/reminders.json
```

Issues are opened only when `GITHUB_TOKEN` and `GITHUB_REPOSITORY` are both in
the environment, which is the case inside GitHub Actions and normally not the
case on your laptop. Locally it just writes the agenda.

## State

`state/notified.json` maps `item@occurrence-date` to the warning steps already
announced:

```json
{
  "announced": {
    "domain-renewal-example-dev@2026-12-22": [30]
  },
  "last_run": "2026-11-25T06:52:00Z"
}
```

Delete a line to be told about it again. Occurrences older than 400 days are
dropped on the next run, so the file stays short.

## When things go wrong

- A malformed entry is skipped, named in the run log and listed at the bottom of
  the agenda. The other reminders still go out.
- If the GitHub API call fails, that reminder is logged and the run continues.
  Its warning step is already marked as announced, so the agenda is the backstop:
  it still shows the item. This is the deliberate trade for never double-posting.
- If `notified.json` is corrupt, the run starts from an empty state and says so.
  You may get one repeat notification.

## Tests

The recurrence and warning-step maths is where this would go wrong quietly, so
it is pinned down:

```bash
python -m unittest discover -s life-admin -v
```

No network, no files touched. The workflow runs these before each real run.
