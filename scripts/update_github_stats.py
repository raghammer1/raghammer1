#!/usr/bin/env python3
"""Generate GitHub profile cards using public GitHub data and Python's stdlib.

Run: GH_TOKEN=... python3 scripts/update_github_stats.py
The optional token is sent only to api.github.com. The contribution calendar is
always fetched without authentication, so a developer's token cannot accidentally
publish private activity. Its anonymous totals follow the user's public settings.
A failed or incomplete fetch leaves existing assets untouched.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from html import escape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API = "https://api.github.com"
PALETTES = {
    "dark": {"bg": "#0d1117", "fg": "#f1eadf", "muted": "#a3abae", "border": "#303638", "accent": "#e99e70", "track": "#20272c", "heat": ["#20272c", "#5a3e32", "#935e43", "#c78056", "#e99e70"]},
    "light": {"bg": "#faf8f3", "fg": "#242d33", "muted": "#5c666a", "border": "#ddd9ce", "accent": "#a9502e", "track": "#eae6df", "heat": ["#eae6df", "#ebc4aa", "#dc9f78", "#c37148", "#a9502e"]},
}
LANGUAGE_COLORS = ["#e99e70", "#b6b0dc", "#88b6a7", "#90b3ce", "#d7bb77", "#c58d9a", "#a6b87a", "#93999c"]


class StatsError(Exception):
    """A source could not be fetched or safely interpreted."""


class GitHub:
    def __init__(self, token: str | None = None):
        self.token = token

    def get(self, url: str, *, api: bool = False):
        if api and not url.startswith(API + "/"):
            raise StatsError("API requests must use api.github.com")
        headers = {"User-Agent": "raghammer1-profile-stats", "Accept-Language": "en-US"}
        if api:
            headers.update({"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
        else:
            headers["Accept"] = "text/html"
        for attempt in range(3):
            try:
                with urlopen(Request(url, headers=headers), timeout=30) as response:
                    payload = response.read().decode("utf-8")
                return json.loads(payload) if api else payload
            except HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise StatsError(f"GitHub returned HTTP {exc.code}; existing cards were not refreshed") from None
            except (URLError, TimeoutError):
                if attempt == 2:
                    raise StatsError("GitHub could not be reached; existing cards were not refreshed") from None
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise StatsError("GitHub returned an unreadable response") from None
            time.sleep(2 ** attempt)

    def repositories(self, login: str) -> list[dict]:
        repos = []
        page = 1
        while True:
            batch = self.get(f"{API}/users/{login}/repos?" + urlencode({"type": "owner", "per_page": 100, "page": page}), api=True)
            if not isinstance(batch, list):
                raise StatsError("Unexpected repository response")
            # This endpoint is public; still apply explicit filters as defense in depth.
            repos.extend(r for r in batch if r.get("private") is False and r.get("owner", {}).get("login", "").lower() == login.lower())
            if len(batch) < 100:
                break
            page += 1
        return repos

    def search_count(self, query: str) -> int:
        result = self.get(f"{API}/search/issues?" + urlencode({"q": query + " is:public", "per_page": 1}), api=True)
        if result.get("incomplete_results") is not False:
            raise StatsError("GitHub search was incomplete; refusing to publish partial totals")
        count = result.get("total_count")
        if not isinstance(count, int) or count < 0:
            raise StatsError("GitHub search did not return a valid count")
        return count


class CalendarParser(HTMLParser):
    """Read GitHub's public calendar cells and their accessible count labels."""
    def __init__(self):
        super().__init__()
        self.cells = {}
        self.labels = {}
        self.tooltip = None
        self.parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "data-date" in attrs:
            day = attrs["data-date"]
            try:
                date.fromisoformat(day)
            except ValueError:
                raise StatsError("Unexpected date in GitHub's public calendar") from None
            cell_id = attrs.get("id")
            if not cell_id or cell_id in self.cells:
                raise StatsError("Unexpected cell in GitHub's public calendar")
            self.cells[cell_id] = day
        if tag == "tool-tip" and "for" in attrs:
            self.tooltip, self.parts = attrs["for"], []

    def handle_data(self, data):
        if self.tooltip:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "tool-tip" and self.tooltip:
            self.labels[self.tooltip] = "".join(self.parts).strip()
            self.tooltip = None

    def counts(self) -> dict[str, int]:
        counts = {}
        for cell_id, day in self.cells.items():
            label = self.labels.get(cell_id, "")
            match = re.match(r"^(No|[\d,]+) contributions? on ", label)
            if not match or day in counts:
                raise StatsError("GitHub's public calendar format changed; preserving previous cards")
            counts[day] = 0 if match[1] == "No" else int(match[1].replace(",", ""))
        if not counts:
            raise StatsError("GitHub's public calendar contained no day counts")
        return counts


def public_calendar(client: GitHub, login: str, start: date, end: date) -> list[dict]:
    counts = {}
    # GitHub serves one calendar year per request, even for a cross-year range.
    for year in range(start.year, end.year + 1):
        url = f"https://github.com/users/{login}/contributions?" + urlencode({"from": f"{year}-01-01", "to": f"{year}-12-31"})
        parser = CalendarParser()
        parser.feed(client.get(url))  # Deliberately never sends authentication.
        counts.update(parser.counts())
    days = []
    for offset in range((end - start).days + 1):
        day = (start + timedelta(days=offset)).isoformat()
        if day not in counts:
            raise StatsError(f"Public calendar is missing {day}; preserving previous cards")
        days.append({"date": day, "count": counts[day]})
    return days


def activity_summary(days: list[dict]) -> dict:
    if not days:
        raise StatsError("Cannot summarize an empty calendar")
    previous = None
    longest = run = total = active = 0
    for entry in days:
        current = date.fromisoformat(entry["date"])
        count = entry["count"]
        if previous and current != previous + timedelta(days=1):
            raise StatsError("Contribution dates must be consecutive")
        if not isinstance(count, int) or count < 0:
            raise StatsError("Contribution counts must be nonnegative integers")
        total += count
        active += count > 0
        run = run + 1 if count else 0
        longest = max(longest, run)
        previous = current
    # An unfinished UTC day with no activity does not break yesterday's streak.
    end = len(days) - 1 if days[-1]["count"] else len(days) - 2
    current_streak = 0
    while end >= 0 and days[end]["count"]:
        current_streak += 1
        end -= 1
    return {"total": total, "active_days": active, "current_streak": current_streak, "longest_streak": longest}


def repository_summary(repos: list[dict]) -> dict:
    originals = [r for r in repos if r.get("private") is False and r.get("fork") is False]
    languages = Counter(r["language"] for r in originals if r.get("language"))
    return {
        "original_public_repositories": len(originals),
        "stars": sum(r["stargazers_count"] for r in originals),
        "languages": [{"name": name, "repositories": count} for name, count in sorted(languages.items(), key=lambda item: (-item[1], item[0]))],
        "repositories_without_language": sum(not r.get("language") for r in originals),
    }


def collect(client: GitHub, login: str, today: date) -> dict:
    start = today - timedelta(days=364)
    repositories = repository_summary(client.repositories(login))
    days = public_calendar(client, login, start, today)
    authored = client.search_count(f"author:{login} is:pr")
    merged = client.search_count(f"author:{login} is:pr is:merged")
    if merged > authored:
        raise StatsError("GitHub search totals changed during refresh; try again")
    return {
        "schema_version": 1, "login": login, "updated_date_utc": today.isoformat(),
        "sources": {"repositories": f"{API}/users/{login}/repos", "pull_requests": f"https://github.com/search?q=author%3A{login}+is%3Apr+is%3Apublic&type=pullrequests", "calendar": f"https://github.com/users/{login}/contributions"},
        "scope": {
            "repositories": "Owned public repositories, excluding forks. Includes archived repositories.",
            "pull_requests": "All-time authored pull requests in public repositories.",
            "languages": "Number of original public repositories by GitHub's primary language; no-language repositories excluded. Not lines of code or proficiency.",
            "activity": "Counts visible on the unauthenticated GitHub profile, including anonymous private counts only if publicly shared. No private repository details are read.",
            "streaks": "Consecutive nonzero GitHub calendar dates within the 365-day window. Today is determined in UTC; yesterday's streak is retained while today has zero contributions. Longest is within this window, not all-time.",
        },
        "repositories": repositories, "pull_requests": {"authored": authored, "merged": merged},
        "activity": {"start": start.isoformat(), "end": today.isoformat(), **activity_summary(days), "days": days},
    }


def txt(x, y, content, *, size=22, fill="fg", weight=400, anchor="start", mono=False):
    family = "ui-monospace, SFMono-Regular, Consolas, monospace" if mono else "-apple-system, BlinkMacSystemFont, Segoe UI, Arial, sans-serif"
    return f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" font-weight="{weight}" fill="{{{fill}}}" text-anchor="{anchor}">{escape(str(content))}</text>'


def panel(title, subtitle, body, height, theme, description):
    colors = PALETTES[theme]
    content = f'''<svg xmlns="http://www.w3.org/2000/svg" width="800" height="{height}" viewBox="0 0 800 {height}" role="img" aria-labelledby="title desc">
<title id="title">{escape(title)}</title><desc id="desc">{escape(description)}</desc>
<rect x="1" y="1" width="798" height="{height - 2}" rx="18" fill="{{bg}}" stroke="{{border}}"/>
<path d="M36 30v30" stroke="{{accent}}" stroke-width="3"/>
{txt(52, 48, title, size=28, weight=650)}
{txt(36, 84, subtitle, size=17, fill="muted")}
{body}
</svg>'''
    # Replace color markers only; no arbitrary formatting of source strings.
    for name, color in colors.items():
        if isinstance(color, str):
            content = content.replace("{" + name + "}", color)
    return content + "\n"


def overview_card(data, theme):
    repos, prs = data["repositories"], data["pull_requests"]
    values = [(36, 150, repos["original_public_repositories"], "Original public repos"), (430, 150, prs["authored"], "Public pull requests"), (36, 245, repos["stars"], "Stars on original repos"), (430, 245, prs["merged"], "Public PRs merged")]
    body = '<path d="M400 112v160M36 203h728" stroke="{border}"/>'
    for x, y, value, label in values:
        body += txt(x, y, f"{value:,}", size=46, weight=650, fill="accent", mono=True)
        body += txt(x, y + 29, label, size=21)
    body += txt(36, 303, f'Public snapshot · updated {data["updated_date_utc"]} UTC', size=15, fill="muted")
    description = "; ".join(f"{value} {label.lower()}" for _, _, value, label in values)
    return panel("GitHub, by the numbers", "Public work across my repositories and pull requests.", body, 320, theme, description)


def languages_card(data, theme):
    rows = data["repositories"]["languages"]
    # Keep the panel readable if future repositories introduce many languages.
    rows = rows if len(rows) <= 8 else rows[:7] + [{"name": "Other", "repositories": sum(r["repositories"] for r in rows[7:])}]
    total = sum(r["repositories"] for r in rows)
    body = '<defs><clipPath id="bar"><rect x="36" y="111" width="728" height="16" rx="8"/></clipPath></defs><rect x="36" y="111" width="728" height="16" rx="8" fill="{track}"/><g clip-path="url(#bar)">'
    x = 36
    for i, row in enumerate(rows):
        width = 728 * row["repositories"] / total
        body += f'<rect x="{x:.2f}" y="111" width="{width:.2f}" height="16" fill="{LANGUAGE_COLORS[i]}"/>'
        x += width
    body += "</g>"
    for i, row in enumerate(rows):
        column, line = divmod(i, 4)
        x, y = 36 + column * 394, 170 + line * 40
        body += f'<circle cx="{x + 6}" cy="{y - 7}" r="6" fill="{LANGUAGE_COLORS[i]}"/>'
        body += txt(x + 23, y, row["name"], size=22)
        body += txt(x + 328, y, str(row["repositories"]), size=23, mono=True, anchor="end", weight=600)
    if not rows:
        body += txt(36, 180, "No primary languages reported yet.", size=22)
    omitted = data["repositories"]["repositories_without_language"]
    body += txt(36, 326, f'{total} repos with a detected language · {omitted} without one excluded', size=16, fill="muted")
    body += txt(36, 348, "Repository counts, not code volume or proficiency.", size=15, fill="muted")
    description = "; ".join(f'{r["name"]}: {r["repositories"]} repositories' for r in rows)
    return panel("The language mix", "Primary languages · original public repositories", body, 360, theme, description)


def activity_card(data, theme):
    activity = data["activity"]
    colors = PALETTES[theme]["heat"]
    body = ""
    metrics = [(36, activity["total"], "Contributions"), (292, activity["current_streak"], "Current streak · days"), (548, activity["longest_streak"], "Best streak · days")]
    for x, value, label in metrics:
        body += txt(x, 145, f"{value:,}", size=44, weight=650, fill="accent", mono=True)
        body += txt(x, 176, label, size=20)
    body += txt(36, 201, "Streaks within this window · today has a one-day grace period", size=15, fill="muted")
    days = activity["days"]
    first = date.fromisoformat(days[0]["date"])
    # GitHub's calendar uses Sunday as the first row of the week.
    offset = (first.weekday() + 1) % 7
    maximum = max(day["count"] for day in days)
    previous_month = None
    month_labels = []
    for i, entry in enumerate(days):
        current = date.fromisoformat(entry["date"])
        column, row = divmod(i + offset, 7)
        x, y = 36 + column * 13.7, 240 + row * 12
        if current.month != previous_month and (column < 51 or i == 0):
            month_labels.append((round(x, 2), current.strftime("%b")))
            previous_month = current.month
        count = entry["count"]
        level = 0 if count == 0 else min(4, 1 + int((count - 1) * 4 / max(1, maximum)))
        body += f'<rect x="{x:.1f}" y="{y}" width="10.7" height="9" rx="2" fill="{colors[level]}"><title>{escape(entry["date"])}: {count} contributions</title></rect>'
    for i, (x, month) in enumerate(month_labels):
        # Omit a short partial first month rather than overlap the next label.
        if i + 1 < len(month_labels) and month_labels[i + 1][0] - x < 36:
            continue
        body += txt(x, 230, month, size=12, fill="muted")
    body += txt(36, 348, f'{activity["start"]} → {activity["end"]} · {activity["active_days"]} active days', size=16, fill="muted")
    body += txt(36, 370, "Counts shown on GitHub · GitHub calendar dates · UTC window", size=14, fill="muted")
    desc = f'{activity["total"]} contributions shown on GitHub in 365 days; current streak {activity["current_streak"]} days; longest within window {activity["longest_streak"]} days. Counts are from the public profile calendar.'
    return panel("A year of showing up", "Public profile activity · rolling 365 days", body, 380, theme, desc)


def markdown_snapshot(data: dict) -> str:
    repos, prs, activity = data["repositories"], data["pull_requests"], data["activity"]
    language_rows = "\n".join(f'| {r["name"]} | {r["repositories"]} |' for r in repos["languages"])
    return f'''# GitHub stats for @{data["login"]}

Updated **{data["updated_date_utc"]} UTC**. These are generated snapshots, refreshed daily after the workflow is merged and enabled.

## Overview

| Metric | Count |
| :--- | ---: |
| Original public repositories | {repos["original_public_repositories"]:,} |
| Stars on original public repositories | {repos["stars"]:,} |
| Authored public pull requests, all time | {prs["authored"]:,} |
| Merged public pull requests, all time | {prs["merged"]:,} |

Repository totals include archived repositories and exclude forks. PR totals count the user's authored pull requests in any public repository.

## Primary languages

One count per original public repository, using GitHub's detected primary language. This measures repository counts, not code volume or proficiency.

| Language | Repositories |
| :--- | ---: |
{language_rows}

{repos["repositories_without_language"]} repositories without a detected primary language are excluded from this table.

## Public profile activity

Window: **{activity["start"]} to {activity["end"]}**, inclusive (365 days).

| Metric | Count |
| :--- | ---: |
| Contributions shown on GitHub | {activity["total"]:,} |
| Active days | {activity["active_days"]:,} |
| Current streak, days | {activity["current_streak"]:,} |
| Longest streak within this window, days | {activity["longest_streak"]:,} |

The calendar is fetched without authentication. It includes anonymous private contribution totals only if the user has chosen to share them publicly. No private repository details are fetched. These are contributions, not just commits.

Streaks use GitHub's calendar dates. The current day and window are determined in UTC; a day without contributions does not end yesterday's streak until that day finishes. The longest streak is measured only within this 365-day window.

Sources: [public repositories](https://github.com/{data["login"]}?tab=repositories), [public pull requests](https://github.com/search?q=author%3A{data["login"]}+is%3Apr+is%3Apublic&type=pullrequests), [public contribution calendar](https://github.com/{data["login"]}). [Daily counts and full data](github-stats.json).
'''


def write_assets(data: dict, output: Path):
    # Render and validate everything before touching the existing assets.
    import xml.etree.ElementTree as ET
    files = {"github-stats.json": json.dumps(data, indent=2, ensure_ascii=False) + "\n", "github-stats.md": markdown_snapshot(data)}
    for theme in PALETTES:
        suffix = "" if theme == "dark" else "-light"
        for name, render in [("overview", overview_card), ("languages", languages_card), ("activity", activity_card)]:
            svg = render(data, theme)
            ET.fromstring(svg)
            files[f"github-{name}{suffix}.svg"] = svg
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".github-stats-", dir=output) as temp:
        for name, content in files.items():
            Path(temp, name).write_text(content, encoding="utf-8")
        for name in files:
            Path(temp, name).replace(output / name)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--login", default="raghammer1")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "assets")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", args.login):
        parser.error("Invalid GitHub username")
    try:
        client = GitHub(os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"))
        data = collect(client, args.login, datetime.now(timezone.utc).date())
        write_assets(data, args.output)
    except (StatsError, KeyError, TypeError, ValueError) as exc:
        # Never log request headers, tokens, API response bodies or raw private data.
        reason = str(exc) if isinstance(exc, StatsError) else "GitHub data did not match the expected schema"
        print(f"Stats refresh failed: {reason}", file=sys.stderr)
        return 1
    print(f'Updated public GitHub cards for {args.login} as of {data["updated_date_utc"]} UTC')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
