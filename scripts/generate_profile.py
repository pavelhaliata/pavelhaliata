#!/usr/bin/env python3
"""Generate self-contained GitHub profile SVGs using the standard library."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html import escape
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
WIDTH, HEIGHT = 985, 530
TEXT_X, TEXT_Y, ROW_HEIGHT, CELL = 390, 30, 20, 8.4
LINE_COLUMNS = 66
THEMES = {
    "dark": {"background": "#161b22", "text": "#c9d1d9", "key": "#ffa657", "value": "#a5d6ff", "muted": "#707c8c", "added": "#3fb950", "deleted": "#f85149"},
    "light": {"background": "#f6f8fa", "text": "#24292f", "key": "#a65316", "value": "#0550ae", "muted": "#6e7781", "added": "#1a7f37", "deleted": "#cf222e"},
}


def github_api(path):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "pavelhaliata-profile-card"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request("https://api.github.com" + path, headers=headers)
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        raise RuntimeError(f"GitHub API returned HTTP {error.code}; existing cards were kept.") from None
    except (URLError, TimeoutError):
        raise RuntimeError("GitHub API is unavailable; existing cards were kept.") from None


def email_hash(email):
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


def parse_commit_log(output, author_hashes):
    """Read authored commits and text additions/deletions; ignore binary files."""
    commits = {}
    active = None
    for line in output.splitlines():
        if line.startswith("@"):
            sha, email = line[1:].split("\t", 1)
            active = sha if email_hash(email) in author_hashes else None
            if active:
                commits[active] = {"additions": 0, "deletions": 0}
        elif active and "\t" in line:
            added, deleted, *_ = line.split("\t", 2)
            if added.isdigit() and deleted.isdigit():
                commits[active]["additions"] += int(added)
                commits[active]["deletions"] += int(deleted)
    return commits


def repository_commits(full_name, author_hashes):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", full_name):
        raise ValueError("Invalid public repository name.")
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    with tempfile.TemporaryDirectory(prefix="profile-stats-") as temporary:
        checkout = Path(temporary) / "repository.git"
        result = subprocess.run(
            ["git", "-c", "credential.helper=", "clone", "--bare", "--quiet", "https://github.com/" + full_name + ".git", str(checkout)],
            capture_output=True, text=True, env=env, timeout=120,
        )
        if result.returncode:
            raise RuntimeError(f"Could not read public history for {full_name}; existing cards were kept.")
        head = subprocess.run(["git", "--git-dir", str(checkout), "rev-parse", "--verify", "HEAD"], capture_output=True, text=True)
        if head.returncode:
            return {}
        log = subprocess.run(
            ["git", "--git-dir", str(checkout), "log", "HEAD", "--no-renames", "--no-ext-diff", "--no-textconv", "--numstat", "--format=@%H%x09%ae"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        if log.returncode:
            raise RuntimeError(f"Could not count public history for {full_name}; existing cards were kept.")
        return parse_commit_log(log.stdout, author_hashes)


def collect_contributions(username, owned_repos):
    cache_path = ROOT / "assets/contribution-cache.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    if cache.get("username", "").lower() != username.lower():
        cache = {}
    identities = set(cache.get("author_email_hashes", []))
    discovered = set(cache.get("repositories", {}))
    search_query = urlencode({"q": "author:" + username, "per_page": 100, "sort": "committer-date", "order": "desc"})
    first = github_api("/search/commits?" + search_query)
    if first.get("incomplete_results"):
        raise RuntimeError("GitHub returned incomplete commit discovery; existing cards were kept.")
    pages = [first]
    for page in range(2, min(10, (first["total_count"] + 99) // 100) + 1):
        data = github_api("/search/commits?" + search_query + "&page=" + str(page))
        if data.get("incomplete_results"):
            raise RuntimeError("GitHub returned incomplete commit discovery; existing cards were kept.")
        pages.append(data)
    for page in pages:
        for item in page["items"]:
            author = item.get("author") or {}
            if author.get("login", "").lower() == username.lower():
                identities.add(email_hash(item["commit"]["author"]["email"]))
                discovered.add(item["repository"]["full_name"])
    if not identities and first["total_count"]:
        raise RuntimeError("Could not identify the account's commit authors; existing cards were kept.")
    metadata = {repo["full_name"]: repo for repo in owned_repos}
    discovered.update(metadata)
    for full_name in sorted(discovered - metadata.keys()):
        metadata[full_name] = github_api("/repos/" + full_name)
    same_identities = identities == set(cache.get("author_email_hashes", []))
    records = {}
    pending = []
    for name, repo in sorted(metadata.items()):
        previous = cache.get("repositories", {}).get(name)
        fingerprint = [repo["pushed_at"], repo["default_branch"]]
        if same_identities and previous and previous["fingerprint"] == fingerprint:
            records[name] = previous
        else:
            pending.append((name, fingerprint))
    def calculate(item):
        name, fingerprint = item
        return name, {"fingerprint": fingerprint, "commits": repository_commits(name, identities)}
    with ThreadPoolExecutor(max_workers=3) as pool:
        for name, record in pool.map(calculate, pending):
            records[name] = record
    # The same commit can occur in a fork or a second repository: count it once.
    unique = {}
    for name in sorted(records):
        unique.update(records[name]["commits"])
    additions = sum(value["additions"] for value in unique.values())
    deletions = sum(value["deletions"] for value in unique.values())
    updated_cache = {"username": username, "author_email_hashes": sorted(identities), "repositories": records}
    cache_path.write_text(json.dumps(updated_cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"commits": len(unique), "contributed_repos": sum(bool(record["commits"]) for record in records.values()), "loc_added": additions, "loc_deleted": deletions, "loc_total": additions - deletions}


def fetch_stats(username):
    user = github_api("/users/" + username)
    repos = []
    page = 1
    while True:
        batch = github_api("/users/" + username + "/repos?" + urlencode({"per_page": 100, "page": page, "type": "owner"}))
        repos.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    stats = {
        "username": user["login"],
        "public_repos": user["public_repos"],
        "stars": sum(repo["stargazers_count"] for repo in repos if not repo["fork"]),
        "followers": user["followers"],
        "following": user["following"],
        "created_at": user["created_at"],
        "updated": datetime.now(timezone.utc).date().isoformat(),
    }
    stats.update(collect_contributions(username, repos))
    return stats


def span(text, color):
    return f'<tspan fill="{color}">{escape(str(text))}</tspan>'


def text_line(y, pieces, palette, x=TEXT_X, cell=CELL, font_size=14):
    plain = "".join(str(text) for text, _ in pieces)
    if len(plain) > LINE_COLUMNS and x == TEXT_X:
        raise ValueError(f"Line is too long for the card: {plain!r}")
    if not plain.strip():
        return ""
    parts = "".join(span(text, palette[color]) for text, color in pieces)
    return (f'<text x="{x}" y="{y}" font-size="{font_size}" '
            f'textLength="{len(plain) * cell:.2f}" lengthAdjust="spacingAndGlyphs">{parts}</text>')


def field(label, value, width=LINE_COLUMNS, prefix=". "):
    value = str(value)
    lead = prefix + label + ":"
    dots = width - len(lead) - len(value) - 2
    if dots < 2:
        raise ValueError(f"Shorten the {label!r} value in profile.json.")
    return [(prefix, "muted"), (label, "key"), (": ", "text"), ("." * dots + " ", "muted"), (value, "value")]


def formatted(value):
    return f"{value:,}" if isinstance(value, int) else "—"


def statistics_lines(stats):
    repos = formatted(stats["public_repos"])
    contributed = formatted(stats.get("contributed_repos"))
    suffix = f"{repos} {{Contributed: {contributed}}}"
    dots = 42 - len(". Repos: ") - len(suffix) - 1
    first = [(". ", "muted"), ("Repos", "key"), (": ", "text"), ("." * max(2, dots) + " ", "muted"), (repos, "value"), (" {", "text"), ("Contributed", "key"), (": ", "text"), (contributed, "value"), ("}", "text")]
    first += [(" | ", "text")] + field("Stars", formatted(stats["stars"]), width=21, prefix="")
    second = field("Commits", formatted(stats.get("commits")), width=42)
    second += [(" | ", "text")] + field("Followers", formatted(stats["followers"]), width=21, prefix="")
    total, added, deleted = (formatted(stats.get(key)) for key in ("loc_total", "loc_added", "loc_deleted"))
    suffix = f"{total} ({added}++, {deleted}--)"
    label = "Lines of Code on GitHub"
    dots = LINE_COLUMNS - len(". " + label + ": ") - len(suffix) - 1
    third = [(". ", "muted"), (label, "key"), (": ", "text"), ("." * max(0, dots) + " ", "muted"), (total, "value"), (" (", "text"), (added + "++", "added"), (", ", "text"), (deleted + "--", "deleted"), (")", "text")]
    return [first, second, third]


def heading(label):
    prefix = "- " + label + " "
    return [(prefix, "text"), ("─" * (LINE_COLUMNS - len(prefix)), "muted")]


def render_svg(config, stats, portrait, theme, layout):
    palette = THEMES[theme]
    terminal = config["terminal"] + " "
    lines = [
        [(terminal, "text"), ("─" * (LINE_COLUMNS - len(terminal)), "muted")],
        field("Name", config["name"]),
        field("Role", config["role"]),
        field("Stack", config["stack"]),
        field("State", config["state"]),
    ]
    styling = [item.strip() for item in config["styling"].split(",")]
    lines.append(field("Styling", ", ".join(styling[:4])))
    if len(styling) > 4:
        lines.append(field("UI", ", ".join(styling[4:])))
    positioned = [(TEXT_Y + index * ROW_HEIGHT, line) for index, line in enumerate(lines)]
    contacts = config.get("contacts", [])
    if len(contacts) > 5:
        raise ValueError("This layout supports up to five contacts.")
    positioned.append((250, heading("Contact")))
    positioned.extend((270 + index * ROW_HEIGHT, field(contact["label"], contact["value"])) for index, contact in enumerate(contacts))
    positioned.append((410, heading("GitHub Stats")))
    positioned.extend((430 + index * ROW_HEIGHT, line) for index, line in enumerate(statistics_lines(stats)))
    positioned.append((510, [("Updated " + stats["updated"], "muted")]))
    title = f'{config["name"]} — {config["role"]} — {config["stack"]}'
    description = (
        f'Text portrait of {config["name"]}. '
        f'{stats["public_repos"]} public repositories, {stats["stars"]} stars, '
        f'{stats["followers"]} followers. Profile: github.com/{config["username"]}. '
        f'Updated {stats["updated"]}.'
    )
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-labelledby="title desc">',
        f'<title id="title">{escape(title)}</title><desc id="desc">{escape(description)}</desc>',
        '<style>text {font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace; white-space: pre; font-weight: 400;}</style>',
        f'<rect width="{WIDTH}" height="{HEIGHT}" fill="{palette["background"]}" rx="15"/>',
        '<g xml:space="preserve">',
    ]
    for index, line in enumerate(portrait):
        if not line.strip():
            continue
        y = layout["y"] + index * layout["row_height"]
        # Position every symbol so font fallback cannot distort the face.
        positions = " ".join(f'{layout["x"] + column * layout["cell_width"]:.2f}' for column in range(len(line)))
        font_size = layout["font_size"]
        parts.append(f'<text x="{positions}" y="{y}" font-size="{font_size}" fill="{palette["text"]}">{escape(line)}</text>')
    for y, line in positioned:
        parts.append(text_line(y, line, palette))
    parts.extend(["</g>", "</svg>"])
    return "\n".join(parts) + "\n"


def contact_href(contact):
    if contact.get("href"):
        return contact["href"]
    value = contact["value"].strip()
    return "mailto:" + value if contact["label"].lower() == "email" else value


def render_readme(config):
    username = config["username"]
    links = [f'[{contact["label"]}]({contact_href(contact)})' for contact in config.get("contacts", [])]
    return (
        f'<a href="https://github.com/{username}/{username}">\n'
        '  <picture>\n'
        '    <source media="(prefers-color-scheme: dark)" srcset="./dark_mode.svg">\n'
        f'    <img alt="{escape(config["name"] + " — " + config["role"] + ". " + config["stack"] + ". Text portrait and public GitHub statistics.", quote=True)}" src="./light_mode.svg" width="985" height="530">\n'
        '  </picture>\n'
        '</a>\n\n'
        + ' · '.join(links) + '\n'
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="Render using assets/stats.json without network calls")
    parser.add_argument("--date", help="Override the update date using YYYY-MM-DD")
    args = parser.parse_args()
    config = json.loads((ROOT / "profile.json").read_text(encoding="utf-8"))
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", config["username"]):
        raise ValueError("Invalid GitHub username in profile.json.")
    for contact in config.get("contacts", []):
        if urlparse(contact_href(contact)).scheme not in ("https", "mailto"):
            raise ValueError("Contact links must use https or mailto.")
    layout = json.loads((ROOT / "assets/portrait-layout.json").read_text(encoding="utf-8"))
    portrait = (ROOT / "assets/portrait.txt").read_text(encoding="utf-8").splitlines()
    if len(portrait) > layout["rows"] or any(len(line) > layout["columns"] for line in portrait):
        raise ValueError("The portrait exceeds the dimensions in portrait-layout.json.")
    if layout["x"] + layout["columns"] * layout["cell_width"] > TEXT_X - 20:
        raise ValueError("The portrait overlaps the profile text.")
    if layout["y"] + (len(portrait)-1) * layout["row_height"] > HEIGHT - 15:
        raise ValueError("The portrait exceeds the card height.")
    portraits = {}
    for theme in THEMES:
        portraits[theme] = portrait
    stats_path = ROOT / "assets/stats.json"
    stats = json.loads(stats_path.read_text(encoding="utf-8")) if args.offline else fetch_stats(config["username"])
    if stats["username"].lower() != config["username"].lower():
        raise ValueError("Cached statistics belong to a different user. Run without --offline.")
    if args.date:
        datetime.strptime(args.date, "%Y-%m-%d")
        stats["updated"] = args.date
    outputs = {f"{theme}_mode.svg": render_svg(config, stats, portraits[theme], theme, layout) for theme in THEMES}
    outputs["README.md"] = render_readme(config)
    outputs["assets/stats.json"] = json.dumps(stats, ensure_ascii=False, indent=2) + "\n"
    for name, content in outputs.items():
        (ROOT / name).write_text(content, encoding="utf-8")
    print(f'Generated dark and light profile cards for {config["username"]}.')


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, KeyError, subprocess.TimeoutExpired, FileNotFoundError) as error:
        raise SystemExit(str(error)) from None
