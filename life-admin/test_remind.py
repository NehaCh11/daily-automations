#!/usr/bin/env python3
"""Tests for the date and announcement logic in remind.py.

The recurrence and warning-step maths is where this tool would quietly go
wrong: a monthly reminder anchored on the 31st, a yearly one anchored on
29 February, or a gap in runs turning into a pile of duplicate notifications.
Those cases are here. Run with: python -m unittest discover life-admin

Nothing here touches the network or the state file on disk.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import remind  # noqa: E402


class NextOccurrence(unittest.TestCase):
    def test_one_off_keeps_its_date_even_when_past(self):
        anchor = date(2026, 5, 1)
        self.assertEqual(remind.next_occurrence(anchor, "none", date(2026, 11, 25)), anchor)

    def test_future_anchor_is_left_alone(self):
        anchor = date(2027, 3, 1)
        for every in remind.EVERY_VALUES:
            self.assertEqual(remind.next_occurrence(anchor, every, date(2026, 11, 25)), anchor)

    def test_monthly_picks_this_month_when_the_day_is_still_ahead(self):
        self.assertEqual(
            remind.next_occurrence(date(2026, 1, 28), "monthly", date(2026, 11, 25)),
            date(2026, 11, 28),
        )

    def test_monthly_rolls_into_next_month_once_the_day_has_passed(self):
        self.assertEqual(
            remind.next_occurrence(date(2026, 1, 5), "monthly", date(2026, 11, 25)),
            date(2026, 12, 5),
        )

    def test_monthly_rolls_over_the_year_boundary(self):
        self.assertEqual(
            remind.next_occurrence(date(2026, 1, 5), "monthly", date(2026, 12, 10)),
            date(2027, 1, 5),
        )

    def test_monthly_on_the_31st_clamps_to_a_short_month(self):
        self.assertEqual(
            remind.next_occurrence(date(2026, 1, 31), "monthly", date(2026, 11, 25)),
            date(2026, 11, 30),
        )
        self.assertEqual(
            remind.next_occurrence(date(2026, 1, 31), "monthly", date(2027, 2, 1)),
            date(2027, 2, 28),
        )

    def test_yearly_on_leap_day_clamps_and_finds_the_next_leap_year(self):
        self.assertEqual(
            remind.next_occurrence(date(2024, 2, 29), "yearly", date(2027, 3, 1)),
            date(2028, 2, 29),
        )
        self.assertEqual(
            remind.next_occurrence(date(2024, 2, 29), "yearly", date(2027, 1, 1)),
            date(2027, 2, 28),
        )

    def test_weekly_finds_the_next_matching_weekday(self):
        wednesday = date(2026, 11, 4)
        self.assertEqual(wednesday.weekday(), 2)
        due = remind.next_occurrence(wednesday, "weekly", date(2026, 11, 26))
        self.assertEqual(due, date(2026, 12, 2))
        self.assertEqual(due.weekday(), 2)

    def test_weekly_on_the_day_itself_is_today(self):
        self.assertEqual(
            remind.next_occurrence(date(2026, 11, 4), "weekly", date(2026, 11, 25)),
            date(2026, 11, 25),
        )

    def test_unknown_repeat_is_rejected(self):
        with self.assertRaises(ValueError):
            remind.next_occurrence(date(2026, 1, 1), "fortnightly", date(2026, 11, 25))


class WarningSteps(unittest.TestCase):
    def test_nothing_fires_outside_the_widest_step(self):
        self.assertEqual(remind.steps_crossed(45, [30, 14, 7, 1, 0]), [])

    def test_crossed_steps_come_back_widest_first(self):
        self.assertEqual(remind.steps_crossed(10, [30, 14, 7, 1, 0]), [30, 14])

    def test_due_today_crosses_every_step(self):
        self.assertEqual(remind.steps_crossed(0, [30, 7, 0]), [30, 7, 0])

    def test_overdue_adds_a_weekly_nag_then_a_monthly_one(self):
        self.assertEqual(remind.steps_crossed(-3, [7, 0]), [7, 0])
        self.assertIn(-7, remind.steps_crossed(-8, [7, 0]))
        self.assertNotIn(-30, remind.steps_crossed(-8, [7, 0]))
        self.assertIn(-30, remind.steps_crossed(-31, [7, 0]))


class Announcements(unittest.TestCase):
    config = {
        "reminders": [
            {"label": "Domain renewal", "date": "2026-12-22", "every": "yearly", "warn": [30, 7, 0]}
        ]
    }

    def announce_on(self, today, state):
        reminders, problems = remind.resolve(self.config, today)
        self.assertEqual(problems, [])
        return remind.due_announcements(reminders, state)

    def test_a_step_is_announced_once(self):
        state = {"announced": {}}
        first = self.announce_on(date(2026, 11, 25), state)
        self.assertEqual([step for _, step in first], [30])
        self.assertEqual(self.announce_on(date(2026, 11, 26), state), [])

    def test_a_gap_in_runs_produces_one_message_not_a_pile(self):
        state = {"announced": {}}
        # First run of the month happens with four days to go: the 30 and 7 day
        # steps are both behind us, and only the tightest one is announced.
        caught_up = self.announce_on(date(2026, 12, 18), state)
        self.assertEqual([step for _, step in caught_up], [7])
        self.assertEqual(state["announced"]["domain-renewal@2026-12-22"], [30, 7])

    def test_the_next_occurrence_is_tracked_separately(self):
        state = {"announced": {}}
        self.announce_on(date(2026, 12, 22), state)
        again = self.announce_on(date(2027, 11, 25), state)
        self.assertEqual([rem.due for rem, _ in again], [date(2027, 12, 22)])


class BadInput(unittest.TestCase):
    def test_one_unreadable_entry_does_not_cost_the_others(self):
        config = {
            "reminders": [
                {"label": "Fine", "date": "2026-12-01"},
                {"label": "No date at all"},
                {"label": "Nonsense date", "date": "the fourth of never"},
                {"label": "Bad repeat", "date": "2026-12-01", "every": "fortnightly"},
                {"label": "Also fine", "date": "2026-12-02"},
            ]
        }
        reminders, problems = remind.resolve(config, date(2026, 11, 25))
        self.assertEqual([rem.label for rem in reminders], ["Fine", "Also fine"])
        self.assertEqual(len(problems), 3)
        self.assertTrue(any("No date at all" in problem for problem in problems))


class Rendering(unittest.TestCase):
    def test_redacted_items_show_a_category_and_no_label(self):
        item = {"label": "Visa paperwork", "date": "2027-01-15", "category": "documents", "redact": True}
        rem = remind.Reminder(item, 0, date(2026, 11, 25), remind.WARN_DEFAULT)
        self.assertNotIn("Visa", rem.display)
        self.assertIn("documents", rem.display)
        self.assertNotIn("Visa", remind.issue_title(rem))
        self.assertNotIn("Visa", remind.issue_body(rem, 30))

    def test_a_redacted_label_stays_out_of_the_state_key_too(self):
        item = {"label": "Visa paperwork", "date": "2027-01-15", "category": "documents", "redact": True}
        rem = remind.Reminder(item, 0, date(2026, 11, 25), remind.WARN_DEFAULT)
        self.assertNotIn("visa", rem.key)
        self.assertNotIn("paperwork", rem.key)
        self.assertTrue(rem.key.startswith("documents-"))
        # Same item, later run: the key has to be the same or it re-announces.
        again = remind.Reminder(item, 0, date(2026, 12, 1), remind.WARN_DEFAULT)
        self.assertEqual(rem.key, again.key)

    def test_notes_reach_the_issue_body_when_nothing_is_redacted(self):
        item = {"label": "Laptop warranty", "date": "2026-12-09", "notes": "Raise the keyboard fault."}
        rem = remind.Reminder(item, 0, date(2026, 11, 25), remind.WARN_DEFAULT)
        self.assertIn("Raise the keyboard fault.", remind.issue_body(rem, 14))

    def test_the_agenda_sorts_overdue_soon_and_later_into_sections(self):
        config = {
            "reminders": [
                {"label": "Late thing", "date": "2026-11-01"},
                {"label": "Soon thing", "date": "2026-12-01"},
                {"label": "Distant thing", "date": "2027-06-01"},
            ]
        }
        reminders, problems = remind.resolve(config, date(2026, 11, 25))
        agenda = remind.render_agenda(reminders, problems, date(2026, 11, 25), "test")
        self.assertLess(agenda.index("Late thing"), agenda.index("Soon thing"))
        self.assertLess(agenda.index("Soon thing"), agenda.index("Distant thing"))
        self.assertIn("## Overdue", agenda)
        self.assertIn("## Further out", agenda)

    def test_an_empty_window_still_renders_a_table(self):
        agenda = remind.render_agenda([], [], date(2026, 11, 25), "test")
        self.assertIn("_nothing due_", agenda)

    def test_phrasing_reads_like_a_person_wrote_it(self):
        self.assertEqual(remind.when_phrase(0), "today")
        self.assertEqual(remind.when_phrase(1), "tomorrow")
        self.assertEqual(remind.when_phrase(9), "in 9 days")
        self.assertEqual(remind.when_phrase(-1), "overdue since yesterday")
        self.assertEqual(remind.when_phrase(-4), "overdue by 4 days")


class ConfigSources(unittest.TestCase):
    """Where the list comes from, which is how real dates stay out of the repo."""

    def setUp(self):
        self.addCleanup(os.environ.pop, "REMINDERS_JSON", None)

    def test_the_environment_variable_beats_the_committed_sample(self):
        os.environ["REMINDERS_JSON"] = json.dumps(
            {"reminders": [{"label": "From the secret", "date": "2026-12-01"}]}
        )
        config, source = remind.load_config(None)
        self.assertIn("REMINDERS_JSON", source)
        self.assertEqual(config["reminders"][0]["label"], "From the secret")

    def test_an_empty_environment_variable_falls_through_to_a_file(self):
        os.environ["REMINDERS_JSON"] = "   "
        _, source = remind.load_config(None)
        self.assertNotIn("REMINDERS_JSON", source)

    def test_broken_json_in_the_secret_fails_loudly(self):
        os.environ["REMINDERS_JSON"] = "{not json"
        with self.assertRaises(SystemExit) as caught:
            remind.load_config(None)
        self.assertIn("REMINDERS_JSON", str(caught.exception))


class CheckFlag(unittest.TestCase):
    """--check is what you run before uploading a list you cannot see run."""

    def run_check(self, config):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "reminders.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = remind.main(["--config", str(path), "--check", "--today", "2026-11-25"])
            return code, out.getvalue()

    def test_a_clean_list_passes_and_shows_each_due_date(self):
        code, out = self.run_check(
            {"reminders": [{"label": "Passport expires", "date": "2028-04-18", "warn": [180, 30]}]}
        )
        self.assertEqual(code, 0)
        self.assertIn("2028-04-18", out)
        self.assertIn("warns at 180, 30", out)
        self.assertIn("every entry reads cleanly", out)

    def test_a_broken_entry_fails_the_check(self):
        code, out = self.run_check(
            {"reminders": [{"label": "Fine", "date": "2026-12-01"}, {"label": "Broken", "date": "nope"}]}
        )
        self.assertEqual(code, 1)
        self.assertIn("could not be read", out)

    def test_check_writes_nothing(self):
        before = remind.STATE_PATH.read_bytes() if remind.STATE_PATH.exists() else None
        agenda_before = remind.AGENDA_PATH.read_bytes() if remind.AGENDA_PATH.exists() else None
        self.run_check({"reminders": [{"label": "Fine", "date": "2026-12-01"}]})
        after = remind.STATE_PATH.read_bytes() if remind.STATE_PATH.exists() else None
        agenda_after = remind.AGENDA_PATH.read_bytes() if remind.AGENDA_PATH.exists() else None
        self.assertEqual(before, after)
        self.assertEqual(agenda_before, agenda_after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
