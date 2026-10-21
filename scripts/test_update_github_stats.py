"""Behavior tests for public scope, calendar correctness and safe failures."""
import contextlib
from datetime import date, timedelta
from io import BytesIO, StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import update_github_stats as stats


def days(counts, start=date(2026, 10, 15)):
    return [{"date": (start + timedelta(days=i)).isoformat(), "count": count} for i, count in enumerate(counts)]


class ArithmeticTests(unittest.TestCase):
    def test_streak_today_and_yesterday_grace(self):
        self.assertEqual(stats.activity_summary(days([1, 3, 0, 2, 1])), {"total": 7, "active_days": 4, "current_streak": 2, "longest_streak": 2})
        self.assertEqual(stats.activity_summary(days([1, 3, 0, 2, 1, 0]))["current_streak"], 2)
        self.assertEqual(stats.activity_summary(days([1, 3, 0, 2, 1, 0, 0]))["current_streak"], 0)

    def test_longest_is_within_window_and_handles_all_zero(self):
        self.assertEqual(stats.activity_summary(days([2, 1, 4, 0, 1]))["longest_streak"], 3)
        self.assertEqual(stats.activity_summary(days([0]))["current_streak"], 0)
        self.assertEqual(stats.activity_summary(days([0, 0]))["longest_streak"], 0)

    def test_rejects_missing_dates_and_negative_counts(self):
        with self.assertRaises(stats.StatsError):
            stats.activity_summary([{"date": "2026-10-19", "count": 1}, {"date": "2026-10-21", "count": 1}])
        with self.assertRaises(stats.StatsError):
            stats.activity_summary(days([-1]))

    def test_public_original_repository_scope(self):
        repos = [
            {"private": False, "fork": False, "language": "Python", "stargazers_count": 2},
            {"private": False, "fork": False, "language": None, "stargazers_count": 1},
            {"private": False, "fork": True, "language": "JavaScript", "stargazers_count": 500},
            {"private": True, "fork": False, "language": "Java", "stargazers_count": 900},
        ]
        self.assertEqual(stats.repository_summary(repos), {"original_public_repositories": 2, "stars": 3, "languages": [{"name": "Python", "repositories": 1}], "repositories_without_language": 1})


class CalendarTests(unittest.TestCase):
    def test_counts_are_joined_by_id_not_html_order(self):
        parser = stats.CalendarParser()
        parser.feed('<td data-date="2026-10-21" id="b"></td><td data-date="2026-10-20" id="a"></td><tool-tip for="a">1,234 contributions on October 20th.</tool-tip><tool-tip for="b">No contributions on October 21st.</tool-tip>')
        self.assertEqual(parser.counts(), {"2026-10-21": 0, "2026-10-20": 1234})

    def test_changed_markup_fails_instead_of_showing_zero(self):
        for markup in ['<td data-date="2026-10-21" id="a"></td>', '<td data-date="2026-10-21" id="a"></td><tool-tip for="a">Activity is unknown</tool-tip>', '<html>Rate limited</html>']:
            parser = stats.CalendarParser()
            parser.feed(markup)
            with self.assertRaises(stats.StatsError):
                parser.counts()

    def test_calendar_crosses_new_year_and_requires_every_day(self):
        client = stats.GitHub()
        old = '<td data-date="2025-12-31" id="a"></td><tool-tip for="a">2 contributions on December 31st.</tool-tip>'
        new = '<td data-date="2026-01-01" id="a"></td><tool-tip for="a">1 contribution on January 1st.</tool-tip>'
        with patch.object(client, "get", side_effect=[old, new]):
            self.assertEqual(stats.public_calendar(client, "raghammer1", date(2025, 12, 31), date(2026, 1, 1)), [{"date": "2025-12-31", "count": 2}, {"date": "2026-01-01", "count": 1}])
        with patch.object(client, "get", return_value=new):
            with self.assertRaises(stats.StatsError):
                stats.public_calendar(client, "raghammer1", date(2026, 1, 1), date(2026, 1, 2))


class NetworkAndFailureTests(unittest.TestCase):
    def test_calendar_request_never_uses_auth_header(self):
        seen = []
        def fake_open(request, timeout):
            seen.append(dict(request.header_items()))
            return BytesIO(b'{}')
        with patch.object(stats, "urlopen", side_effect=fake_open):
            client = stats.GitHub("test-secret")
            client.get("https://github.com/users/raghammer1/contributions")
            client.get(stats.API + "/users/raghammer1/repos", api=True)
        self.assertNotIn("Authorization", seen[0])
        self.assertEqual(seen[1]["Authorization"], "Bearer test-secret")

    def test_incomplete_search_does_not_publish_partial_count(self):
        client = stats.GitHub()
        with patch.object(client, "get", return_value={"total_count": 17, "incomplete_results": True}):
            with self.assertRaises(stats.StatsError):
                client.search_count("author:raghammer1 is:pr")

    def test_failure_preserves_existing_assets_and_hides_token(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            previous = output / "github-overview.svg"
            previous.write_text("previous working card")
            failure = HTTPError(stats.API, 403, "test-secret", {}, BytesIO())
            stderr = StringIO()
            with patch.object(stats, "urlopen", side_effect=failure), contextlib.redirect_stderr(stderr):
                result = stats.main(["--output", temp])
            failure.close()
            self.assertEqual(result, 1)
            self.assertEqual(previous.read_text(), "previous working card")
            self.assertNotIn("test-secret", stderr.getvalue())
            self.assertEqual(list(output.iterdir()), [previous])

    def test_repo_pagination_filters_private_and_other_owners(self):
        client = stats.GitHub()
        original = {"private": False, "owner": {"login": "raghammer1"}}
        first = [original] * 100
        second = [{"private": True, "owner": {"login": "raghammer1"}}, {"private": False, "owner": {"login": "elsewhere"}}]
        with patch.object(client, "get", side_effect=[first, second]) as request:
            self.assertEqual(len(client.repositories("raghammer1")), 100)
            self.assertIn("page=2", request.call_args.args[0])


class OutputTests(unittest.TestCase):
    def test_complete_outputs_render_for_empty_languages_and_activity(self):
        calendar = days([0] * 365, date(2025, 10, 22))
        data = {"login": "example", "updated_date_utc": "2026-10-21", "repositories": stats.repository_summary([]), "pull_requests": {"authored": 0, "merged": 0}, "activity": {"start": "2025-10-22", "end": "2026-10-21", **stats.activity_summary(calendar), "days": calendar}}
        with tempfile.TemporaryDirectory() as temp:
            stats.write_assets(data, Path(temp))
            self.assertEqual(len(list(Path(temp).glob("*.svg"))), 6)
            self.assertIn("Current streak", (Path(temp) / "github-stats.md").read_text())
            self.assertEqual(json.loads((Path(temp) / "github-stats.json").read_text())["activity"]["total"], 0)


if __name__ == "__main__":
    unittest.main()
