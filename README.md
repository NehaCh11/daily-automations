# daily-automations

Small automations for ordinary life. Renewals, deadlines, the recurring admin
that is easy to forget and annoying to forget. Each one runs on a schedule,
keeps what it remembers as a file you can read, and is executed against the
real service before it lands here.

Its sibling repo, [research-automations](https://github.com/NehaCh11/research-automations),
holds the tools that serve my master's research. The split is by lifespan
rather than topic: research tooling follows the thesis, these follow me.

## Tools

| Tool | What it does | Runs on |
| --- | --- | --- |
| [life-admin](life-admin/) | Watches a list of dates that expire, renew or fall due, and opens a GitHub issue when one enters its warning window | GitHub Actions, daily 06:38 UTC |

## How these are built

- **It has to run.** Every tool is executed for real before it lands, and its
  real output is committed. No untested code.
- **Standard library first.** A dependency is added only when the task cannot be
  done without it. `life-admin` has none.
- **Idempotent.** Running twice does not duplicate work or double-post. A gap in
  runs produces one catch-up message, not a pile.
- **State is visible.** Anything a tool remembers between runs is a file in the
  repo, not a hidden cache. That makes it reviewable, and a bad run fixable with
  an edit.
- **Degrade, don't crash.** One malformed entry or one failed call should cost
  you that item, not the whole run, and the output should say what it missed.

## Layout

```
tool-name/
  README.md       what it does, how to run it, how to configure it
  *.py            the tool
  test_*.py       the logic that would fail quietly, pinned down
  *.json          anything worth changing without editing code
  state/          what it remembers between runs
.github/workflows/
  tool-name.yml   the schedule
```

## Running anything here locally

Python 3.9 or newer, and no setup:

```bash
python life-admin/remind.py --dry-run
```

Every tool supports `--dry-run`, which prints what it would do and writes
nothing.

## A note on personal data

This repo is public, so nothing personal is committed to it, and the committed
configuration is sample data. The thing worth saying out loud: on a public repo
a secret hides the input, not the output. An automation that opens issues or
commits a report publishes whatever it has to say, whoever told it. So tools
here read personal lists from a gitignored file or a repository secret, support
marking an entry as redacted so only its category reaches anything public, and
where I actually run one on my own data it runs from a private copy. The public
one keeps the sample.
