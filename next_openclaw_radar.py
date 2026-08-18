#!/usr/bin/env python3
"""
Next OpenClaw Radar
===================

Finds fast-rising open-source projects with an emphasis on:
- AI agents / agent infrastructure
- local AI
- developer tools
- self-hosting / personal computing
- creative automation

It combines GitHub signals with Hacker News discussion and scores:
  35% 7-day GitHub star velocity
  20% recency
  15% absolute GitHub traction
  10% forks
  10% Hacker News discussion
  10% "OpenClaw-like" fit

No database is required. Results are saved to radar.json and radar.csv.

Optional:
  GITHUB_TOKEN=...   increases GitHub API rate limits

Usage:
  pip install requests
  python next_openclaw_radar.py
  python next_openclaw_radar.py --limit 25
  python next_openclaw_radar.py --days 14
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests


GITHUB = "https://api.github.com"
HN = "https://hn.algolia.com/api/v1"

# Deliberately broad: the radar should discover projects rather than hard-code
# today's fashionable repositories.
SEARCH_QUERIES = [
    "AI agent",
    "AI agents",
    "autonomous agent",
    "personal AI",
    "agent skills",
    "agent framework",
    "computer use AI",
    "local AI",
    "LLM agent",
    "AI coding agent",
    "self hosted AI",
    "AI automation",
    "AI workflow",
    "AI browser",
]

# Projects/categories that the user explicitly asked not to include.
EXCLUDE_NAMES = {
    "immich", "home-assistant", "zen-browser", "godot",
    "uv", "syncthing", "ente", "bambu-studio", "comfyui",
    "mcp",
}

KEYWORDS = {
    "agent": 10,
    "agents": 10,
    "autonomous": 9,
    "personal ai": 10,
    "computer use": 10,
    "agentic": 9,
    "skills": 8,
    "skill": 7,
    "memory": 7,
    "browser": 6,
    "local ai": 8,
    "self-host": 7,
    "self hosted": 7,
    "automation": 6,
    "workflow": 5,
    "llm": 4,
    "inference": 4,
    "coding agent": 9,
    "developer tool": 4,
    "multimodal": 5,
}

session = requests.Session()
session.headers.update({
    "Accept": "application/vnd.github+json",
    "User-Agent": "next-openclaw-radar/1.0",
})
if os.getenv("GITHUB_TOKEN"):
    session.headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"


def github_get(path: str, params: dict[str, Any] | None = None) -> requests.Response:
    r = session.get(GITHUB + path, params=params, timeout=20)
    r.raise_for_status()
    return r


def hn_search(query: str, days: int) -> dict[str, Any]:
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    r = session.get(
        f"{HN}/search",
        params={"query": query, "tags": "story", "numericFilters": f"created_at_i>{cutoff}", "hitsPerPage": 100},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def excluded(repo: dict[str, Any]) -> bool:
    full = repo["full_name"].lower()
    name = repo["name"].lower().replace("_", "-")
    return any(x == name or x in full for x in EXCLUDE_NAMES)


def discover(days: int) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}

    # Recent repositories: these are particularly valuable for "next big thing"
    # detection because a 2000-star project created last month is more interesting
    # than a 2000-star project that has existed for eight years.
    for q in SEARCH_QUERIES:
        for sort in ("stars", "updated"):
            params = {
                "q": f"{q} created:>{(datetime.now(timezone.utc)-timedelta(days=365)).date()}",
                "sort": sort,
                "order": "desc",
                "per_page": 30,
            }
            try:
                data = github_get("/search/repositories", params).json()
            except requests.RequestException:
                continue

            for repo in data.get("items", []):
                if excluded(repo):
                    continue
                found[repo["full_name"]] = repo

    # A few mature projects are also worth considering because explosive
    # momentum can happen long after initial creation.
    for q in ("agent", "local ai", "AI automation", "AI coding"):
        try:
            data = github_get(
                "/search/repositories",
                {"q": q, "sort": "stars", "order": "desc", "per_page": 30},
            ).json()
        except requests.RequestException:
            continue
        for repo in data.get("items", []):
            if not excluded(repo):
                found.setdefault(repo["full_name"], repo)

    return found


def star_velocity(repo_full_name: str, days: int) -> int | None:
    """
    Count recent stars using GitHub's timestamped stargazer endpoint.

    This is deliberately conservative. If the API doesn't expose timestamped
    stargazers (or rate limits us), return None and let the scoring model fall
    back to other signals.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    try:
        r = github_get(
            f"/repos/{repo_full_name}/stargazers",
            {"per_page": 100, "page": 1},
        )
        # GitHub normally needs this media type for starred_at timestamps.
        # Re-request explicitly if necessary.
        if r.headers.get("content-type", "").startswith("application/json"):
            data = r.json()
        else:
            return None

        # A timestamp-aware response looks like:
        # {"user": {...}, "starred_at": "..."}
        if not data or "starred_at" not in data[0]:
            r = session.get(
                f"{GITHUB}/repos/{repo_full_name}/stargazers",
                params={"per_page": 100, "page": 1},
                headers={
                    "Accept": "application/vnd.github.star+json",
                    "User-Agent": "next-openclaw-radar/1.0",
                },
                timeout=20,
            )
            r.raise_for_status()
            data = r.json()

        if not data or "starred_at" not in data[0]:
            return None

        # Walk backward from the newest page until stars are older than cutoff.
        # GitHub's pagination is used to jump to the final page first.
        link = r.headers.get("Link", "")
        m = re.search(r'<([^>]+)>;\s*rel="last"', link)
        if not m:
            pages = 1
        else:
            last_url = m.group(1)
            q = re.search(r"[?&]page=(\d+)", last_url)
            pages = int(q.group(1)) if q else 1

        total = 0
        page = pages

        while page >= 1 and page > pages - 20:
            rr = session.get(
                f"{GITHUB}/repos/{repo_full_name}/stargazers",
                params={"per_page": 100, "page": page},
                headers={
                    "Accept": "application/vnd.github.star+json",
                    "User-Agent": "next-openclaw-radar/1.0",
                },
                timeout=20,
            )
            if rr.status_code != 200:
                return None
            stars = rr.json()
            if not stars:
                break

            oldest = None
            for item in stars:
                ts = item.get("starred_at")
                if not ts:
                    return None
                dt = parse_dt(ts)
                oldest = dt if oldest is None or dt < oldest else oldest
                if dt >= cutoff:
                    total += 1

            if oldest and oldest < cutoff:
                break
            page -= 1

        return total

    except (requests.RequestException, ValueError, KeyError):
        return None


def hn_signal(name: str, days: int) -> tuple[int, int, int]:
    try:
        data = hn_search(name, days)
    except requests.RequestException:
        return 0, 0, 0

    hits = data.get("hits", [])
    points = 0
    comments = 0
    for h in hits:
        points += int(h.get("points") or 0)
        comments += int(h.get("num_comments") or 0)
    return len(hits), points, comments


def fit_score(repo: dict[str, Any]) -> float:
    text = " ".join([
        repo.get("name", ""),
        repo.get("description") or "",
        " ".join(repo.get("topics") or []),
    ]).lower()

    raw = 0
    for kw, weight in KEYWORDS.items():
        if kw in text:
            raw += weight

    # 100 is an approximate ceiling, not a benchmark.
    return min(100.0, raw * 2.2)


def minmax(values: list[float]) -> dict[float, float]:
    if not values:
        return {}
    lo, hi = min(values), max(values)
    if hi == lo:
        return {v: 50.0 for v in values}
    return {v: 100.0 * (v - lo) / (hi - lo) for v in values}


def build_radar(repos: dict[str, dict[str, Any]], days: int) -> list[dict[str, Any]]:
    rows = []
    candidates = list(repos.values())

    # Limit expensive star-history calls to the strongest candidates.
    candidates.sort(key=lambda r: (r["stargazers_count"], r["forks_count"]), reverse=True)
    candidates = candidates[:100]

    for i, repo in enumerate(candidates, 1):
        print(f"[{i:03}/{len(candidates):03}] {repo['full_name']}", flush=True)

        stars_7d = star_velocity(repo["full_name"], days)
        hn_hits, hn_points, hn_comments = hn_signal(repo["name"], days)

        age_days = max(
            1,
            (datetime.now(timezone.utc) - parse_dt(repo["created_at"])).days,
        )
        recency = max(0.0, 100.0 * (1.0 - min(age_days, 730) / 730))

        rows.append({
            "name": repo["full_name"],
            "url": repo["html_url"],
            "description": repo.get("description") or "",
            "language": repo.get("language") or "",
            "stars": repo["stargazers_count"],
            "forks": repo["forks_count"],
            "open_issues": repo["open_issues_count"],
            "created": repo["created_at"],
            "updated": repo["updated_at"],
            "stars_recent": stars_7d,
            "hn_hits": hn_hits,
            "hn_points": hn_points,
            "hn_comments": hn_comments,
            "fit": round(fit_score(repo), 1),
            "recency": round(recency, 1),
        })

    # Normalized components
    velocity_vals = [float(r["stars_recent"] or 0) for r in rows]
    stars_vals = [math.log10(max(1, r["stars"])) for r in rows]
    forks_vals = [math.log10(max(1, r["forks"])) for r in rows]
    hn_vals = [math.log10(1 + r["hn_points"] + 2 * r["hn_comments"]) for r in rows]

    velocity_norm = minmax(velocity_vals)
    stars_norm = minmax(stars_vals)
    forks_norm = minmax(forks_vals)
    hn_norm = minmax(hn_vals)

    for r in rows:
        v = velocity_norm.get(float(r["stars_recent"] or 0), 0)
        s = stars_norm.get(math.log10(max(1, r["stars"])), 0)
        f = forks_norm.get(math.log10(max(1, r["forks"])), 0)
        h = hn_norm.get(math.log10(1 + r["hn_points"] + 2 * r["hn_comments"]), 0)

        # Star velocity is intentionally dominant. This avoids producing a list
        # that merely reproduces "most starred GitHub repos."
        score = (
            0.35 * v +
            0.20 * r["recency"] +
            0.15 * s +
            0.10 * f +
            0.10 * h +
            0.10 * r["fit"]
        )
        r["radar_score"] = round(score, 2)

        if score >= 80:
            r["signal"] = "🚨 breakout"
        elif score >= 65:
            r["signal"] = "🔥 strong"
        elif score >= 50:
            r["signal"] = "📈 rising"
        else:
            r["signal"] = "👀 watch"

    return sorted(rows, key=lambda x: x["radar_score"], reverse=True)


def save(rows: list[dict[str, Any]], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "radar.json").write_text(
        json.dumps(rows, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    fields = [
        "radar_score", "signal", "name", "stars", "stars_recent", "forks",
        "hn_hits", "hn_points", "hn_comments", "fit", "recency",
        "language", "url", "description",
    ]
    with (out_dir / "radar.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def print_report(rows: list[dict[str, Any]], limit: int) -> None:
    print("\n" + "=" * 100)
    print("NEXT OPENCLAW RADAR")
    print("=" * 100)

    for n, r in enumerate(rows[:limit], 1):
        recent = "?" if r["stars_recent"] is None else f"+{r['stars_recent']}"
        print(
            f"{n:2}. {r['signal']} {r['radar_score']:5.1f}  "
            f"{r['name']:<42} ⭐ {r['stars']:,}  {recent}/{DAYS}d  "
            f"HN:{r['hn_hits']}"
        )
        print(f"    {r['description'][:130]}")
        print(f"    {r['url']}")
    print("=" * 100)
    print("Files: radar.json, radar.csv")


def main() -> None:
    global DAYS

    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--out", default="openclaw-radar")
    args = parser.parse_args()
    DAYS = args.days

    print("Discovering repositories...")
    repos = discover(args.days)
    print(f"Found {len(repos)} candidate repositories.")

    rows = build_radar(repos, args.days)
    save(rows, Path(args.out))
    print_report(rows, args.limit)


if __name__ == "__main__":
    main()
