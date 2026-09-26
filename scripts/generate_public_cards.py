#!/usr/bin/env python3
"""Render profile cards using only GitHub's publicly visible data."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from html import unescape
from pathlib import Path
import json
import os
import re
import subprocess
import tempfile
import urllib.parse
import urllib.request
from xml.sax.saxutils import escape


USER = "IanKonlog"
ROOT = Path(__file__).resolve().parents[1]
API = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN", "")
COLORS = {
    "TypeScript": "#3178c6",
    "Python": "#3572a5",
    "JavaScript": "#f1e05a",
    "HTML": "#e34c26",
    "CSS": "#563d7c",
    "Shell": "#89e051",
    "Dockerfile": "#384d54",
}
SOURCE_SUFFIXES = {
    ".bash", ".c", ".cc", ".cpp", ".cs", ".css", ".go", ".h", ".hpp",
    ".html", ".java", ".js", ".jsx", ".kt", ".kts", ".php", ".py",
    ".rb", ".rs", ".scss", ".sh", ".sql", ".svelte", ".swift",
    ".ts", ".tsx", ".vue",
}
SKIPPED_DIRECTORIES = {
    ".git", ".next", ".venv", "__pycache__", "build", "coverage", "dist",
    "generated", "node_modules", "out", "target", "vendor",
}


def request_json(path: str) -> object:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "IanKonlog-profile-cards"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    with urllib.request.urlopen(urllib.request.Request(f"{API}/{path}", headers=headers), timeout=20) as response:
        return json.load(response)


def public_repositories() -> list[dict]:
    repositories = []
    for page in range(1, 20):
        batch = request_json(f"users/{USER}/repos?type=public&per_page=100&page={page}")
        if not isinstance(batch, list):
            raise ValueError("GitHub did not return a public repository list")
        repositories.extend(repo for repo in batch if repo["private"] is False and repo["owner"]["login"] == USER)
        if len(batch) < 100:
            return repositories
    raise ValueError("Public repository list exceeded the expected page limit")


def starred_count() -> int:
    count = 0
    for page in range(1, 20):
        batch = request_json(f"users/{USER}/starred?per_page=100&page={page}")
        if not isinstance(batch, list):
            raise ValueError("GitHub did not return a public starred repository list")
        count += sum(repo["private"] is False for repo in batch)
        if len(batch) < 100:
            return count
    raise ValueError("Starred repository list exceeded the expected page limit")


def public_issue_count(kind: str) -> int:
    query = urllib.parse.urlencode({"q": f"author:{USER} type:{kind}", "per_page": 1})
    result = request_json(f"search/issues?{query}")
    return int(result["total_count"])


def annual_contributions() -> int:
    url = f"https://github.com/users/{USER}/contributions"
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        html = response.read().decode("utf-8")
    match = re.search(
        r'id="js-contribution-activity-description"[^>]*>\s*([\d,]+)\s+contributions',
        html,
        re.IGNORECASE,
    )
    if not match:
        raise ValueError("Could not read the public annual contribution total")
    return int(unescape(match.group(1)).replace(",", ""))


def is_source_file(name: str) -> bool:
    path = Path(name)
    if any(part.lower() in SKIPPED_DIRECTORIES for part in path.parts):
        return False
    if path.name.endswith((".min.js", ".min.css", ".generated.ts", ".generated.js")):
        return False
    return path.suffix.lower() in SOURCE_SUFFIXES or path.name in {"Dockerfile", "Makefile"}


def public_code_lines(repositories: list[dict]) -> tuple[int, int, int]:
    """Count current nonblank source lines and historical source-line changes."""
    current = added = removed = 0
    with tempfile.TemporaryDirectory(prefix="public-profile-code-") as directory:
        for repo in repositories:
            name = repo["name"]
            location = Path(directory) / name
            url = f"https://github.com/{USER}/{urllib.parse.quote(name)}.git"
            subprocess.run(["git", "clone", "--quiet", "--no-tags", url, str(location)], check=True, timeout=180)

            tracked = subprocess.check_output(["git", "-C", str(location), "ls-files", "-z"])
            for raw_name in tracked.split(b"\0"):
                if not raw_name:
                    continue
                filename = os.fsdecode(raw_name)
                if not is_source_file(filename):
                    continue
                source = location / filename
                if source.is_symlink() or not source.is_file() or source.stat().st_size > 1_000_000:
                    continue
                data = source.read_bytes()
                if b"\0" in data:
                    continue
                current += sum(bool(line.strip()) for line in data.splitlines())

            history = subprocess.check_output(
                ["git", "-C", str(location), "log", "--no-merges", "--numstat", "--format="],
                text=True, errors="replace", timeout=120,
            )
            for line in history.splitlines():
                fields = line.split("\t", 2)
                if len(fields) != 3 or not fields[0].isdigit() or not fields[1].isdigit():
                    continue
                filename = fields[2].rsplit(" => ", 1)[-1].replace("}", "")
                if is_source_file(filename):
                    added += int(fields[0])
                    removed += int(fields[1])
    return current, added, removed


def svg_styles() -> str:
    return """<style>
      text { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif; fill: #57606a; }
      .surface { fill: #f6f8fa; stroke: #d0d7de; }
      .title { fill: #24292f; font-size: 22px; font-weight: 700; }
      .heading { fill: #0969da; font-size: 16px; font-weight: 600; }
      .value { fill: #24292f; font-weight: 700; }
      .row { font-size: 14px; }
      .note { font-size: 12px; }
      .rule { stroke: #d0d7de; }
      @media (prefers-color-scheme: dark) {
        text { fill: #8b949e; }
        .surface { fill: #0d1117; stroke: #30363d; }
        .title, .value { fill: #e6edf3; }
        .heading { fill: #58a6ff; }
        .rule { stroke: #30363d; }
      }
    </style>"""


def render_stats(
    user: dict, repos: list[dict], stars_given: int, contributions: int,
    prs: int, issues: int, code_lines: tuple[int, int, int],
) -> str:
    original = [repo for repo in repos if not repo["fork"]]
    years = date.today().year - datetime.fromisoformat(user["created_at"].replace("Z", "+00:00")).year
    anniversary = date.fromisoformat(user["created_at"][:10])
    if (date.today().month, date.today().day) < (anniversary.month, anniversary.day):
        years -= 1
    def row(x: int, y: int, value: str, label: str) -> str:
        return f'<text class="row" x="{x}" y="{y}"><tspan class="value">{escape(value)}</tspan> {escape(label)}</text>'

    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="480" height="414" viewBox="0 0 480 414" role="img" aria-label="Public GitHub profile statistics">',
        svg_styles(),
        '<rect class="surface" x="0.5" y="0.5" width="479" height="413" rx="10" />',
        '<text class="title" x="18" y="30">Roger Ian Konlog</text>',
        f'<text class="note" x="18" y="49">On GitHub for {years} years · Public profile data</text>',
        '<line class="rule" x1="18" y1="62" x2="462" y2="62" />',
        '<text class="heading" x="18" y="88">Activity</text>',
        row(18, 115, f"{contributions:,}", "contributions past year"),
        row(18, 139, f"{prs:,}", "public pull requests"),
        row(18, 163, f"{issues:,}", "public issues"),
        '<text class="heading" x="250" y="88">Community</text>',
        row(250, 115, f"{user['followers']:,}", "followers"),
        row(250, 139, f"{user['following']:,}", "following"),
        row(250, 163, f"{stars_given:,}", "repositories starred"),
        '<line class="rule" x1="18" y1="181" x2="462" y2="181" />',
        '<text class="heading" x="18" y="208">Public repositories</text>',
        row(18, 235, f"{len(repos):,}", "repositories"),
        row(250, 235, f"{len(original):,}", "original projects"),
        row(18, 259, f"{len(repos) - len(original):,}", "forks"),
        row(250, 259, f"{sum(repo['stargazers_count'] for repo in original):,}", "stars earned"),
        '<line class="rule" x1="18" y1="278" x2="462" y2="278" />',
        '<text class="heading" x="18" y="305">Lines of code</text>',
        row(18, 332, f"{code_lines[0]:,}", "nonblank source lines"),
        row(18, 356, f"{code_lines[1]:,}", "lines added"),
        row(250, 356, f"{code_lines[2]:,}", "lines removed"),
        '<line class="rule" x1="18" y1="373" x2="462" y2="373" />',
        '<text class="note" x="18" y="393">Source lines are current; changes span Git history.</text>',
        '<text class="note" x="18" y="407">Only original public repositories are counted.</text>',
    ]
    parts.append('</svg>')
    return "\n".join(parts) + "\n"


def render_languages(languages: Counter[str], project_count: int) -> str:
    total = sum(languages.values())
    if total <= 0:
        raise ValueError("No languages found in original public repositories")
    ranking = languages.most_common(6)
    other = total - sum(size for _, size in ranking)
    if other:
        ranking.append(("Other", other))
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="480" height="203" viewBox="0 0 480 203" role="img" aria-label="Languages in original public repositories">',
        svg_styles(),
        '<rect class="surface" x="0.5" y="0.5" width="479" height="202" rx="10" />',
        '<text class="heading" x="18" y="27">Languages in public projects</text>',
        f'<text class="note" x="18" y="46">By code size across {project_count} original repositories</text>',
        '<defs><clipPath id="bar"><rect x="18" y="61" width="444" height="14" rx="7" /></clipPath></defs>',
        '<g clip-path="url(#bar)">',
    ]
    cursor = 18.0
    for name, size in ranking:
        width = 444 * size / total
        color = COLORS.get(name, "#8b949e")
        parts.append(f'<rect x="{cursor:.2f}" y="61" width="{width + 0.01:.2f}" height="14" fill="{color}" />')
        cursor += width
    parts.append('</g>')
    for index, (name, size) in enumerate(ranking):
        col, legend_row = index % 2, index // 2
        x, y = 18 + col * 225, 101 + legend_row * 23
        percent = size * 100 / total
        display = f"{percent:.1f}%" if percent < 1 else f"{percent:.0f}%"
        color = COLORS.get(name, "#8b949e")
        parts.append(f'<circle cx="{x + 6}" cy="{y - 5}" r="5" fill="{color}" />')
        parts.append(f'<text class="row" x="{x + 18}" y="{y}">{escape(name)} {display}</text>')
    parts.append('<text class="note" x="18" y="196">Private repositories and forks excluded.</text>')
    parts.append('</svg>')
    return "\n".join(parts) + "\n"


def main() -> None:
    user = request_json(f"users/{USER}")
    repos = public_repositories()
    original = [repo for repo in repos if not repo["fork"]]
    languages: Counter[str] = Counter()
    for repo in original:
        language_bytes = request_json(f"repos/{USER}/{urllib.parse.quote(repo['name'])}/languages")
        languages.update(language_bytes)
    stats = render_stats(
        user, repos, starred_count(), annual_contributions(),
        public_issue_count("pr"), public_issue_count("issue"),
        public_code_lines(original),
    )
    (ROOT / "github-metrics.svg").write_text(stats, encoding="utf-8")
    (ROOT / "languages.svg").write_text(render_languages(languages, len(original)), encoding="utf-8")


if __name__ == "__main__":
    main()
