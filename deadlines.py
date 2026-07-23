# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "httpx",
#   "beautifulsoup4",
# ]
# ///
"""Extract submission deadlines from a researchr conference/track page.

Examples:
  uv run deadlines.py https://conf.researchr.org/home/splash-issta-2026/iwaco-2026
    -> 2026/7/21
  uv run deadlines.py https://2027.ecoop.org/track/ecoop-2027-technical-papers
    -> R1: 2026/11/19, R2: 2027/2/11
"""
import re
import sys

import httpx
from bs4 import BeautifulSoup

MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

DATE_RE = re.compile(r"(\d{1,2})\s+([A-Z][a-z]{2})\s+(\d{4})")
ROUND_RE = re.compile(r"round\s*(\d+)", re.IGNORECASE)


def parse_date(text: str) -> tuple[str | None, int]:
    m = DATE_RE.search(text)
    if not m:
        return None, 0
    d, mon, y = m.group(1), m.group(2), m.group(3)
    return f"{y}/{MONTHS[mon]}/{int(d)}", m.end()


def fetch(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; deadlines-fetcher/1.0)",
        "Accept": "text/html",
    }
    with httpx.Client(follow_redirects=True, timeout=30, headers=headers) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.text


def extract(html: str) -> list[tuple[int | None, str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.important-dates-in-sidebar")
    if table is None:
        raise SystemExit("No 'Important Dates' table found on this page.")
    subs: list[tuple[int | None, str, str]] = []
    for tr in table.select("tr"):
        td = tr.find("td")
        if td is None:
            continue
        text = td.get_text(" ", strip=True)
        if "submission" not in text.lower():
            continue
        date, end = parse_date(text)
        if date is None:
            continue
        raw_label = text[end:].strip()

        rm = ROUND_RE.search(raw_label)
        round_n = int(rm.group(1)) if rm else None
        if rm:
            raw_label = (raw_label[:rm.start()] + raw_label[rm.end():]).strip()
        raw_label = re.sub(
            r"\bsubmissions?\b", "", raw_label, flags=re.IGNORECASE
        ).strip(" -:")
        subs.append((round_n, raw_label, date))
    return subs


def format_subs(subs: list[tuple[int | None, str, str]]) -> str:
    if not subs:
        return ""
    if len(subs) == 1:
        return subs[0][2]
    rounds = [r for r, _, _ in subs if r is not None]
    if len(rounds) == len(subs) and len(set(rounds)) == len(subs):
        return ", ".join(f"R{r}: {d}" for r, _, d in sorted(subs))
    parts: list[str] = []
    for r, label, d in subs:
        prefix = f"R{r} " if r is not None else ""
        if label:
            parts.append(f"{prefix}{label}: {d}")
        elif prefix:
            parts.append(f"{prefix.strip()}: {d}")
        else:
            parts.append(d)
    return ", ".join(parts)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <researchr conference page url>", file=sys.stderr)
        return 2
    url = argv[1]
    html = fetch(url)
    subs = extract(html)
    out = format_subs(subs)
    if not out:
        print("No submission deadlines found.", file=sys.stderr)
        return 1
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
