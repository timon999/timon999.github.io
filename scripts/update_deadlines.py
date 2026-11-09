# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "httpx",
#   "beautifulsoup4",
# ]
# ///
"""Update passed deadlines in conference_deadlines.html to the next edition.

For every row whose deadline has passed:
  1. Guess next year's page by bumping the year in the URL (pldi27 -> pldi28,
     2027.ecoop.org -> 2028.ecoop.org, ...). If the URL has no year, the same
     page is re-fetched in case it was updated in place.
  2. Fetch it and extract the deadline: dates near words like "submission" /
     "deadline" (or "abstract" for abstract rows) that lie roughly one year
     after the old deadline.
  3. Rows the heuristic cannot resolve, and the location of rows it did
     resolve, are handed to Claude (Sonnet, inside nono) in one batch.
     Claude only gets web tools and answers in JSON; this script edits the file.

Usage:
  uv run scripts/update_deadlines.py              # update the file
  uv run scripts/update_deadlines.py --dry-run    # only show what would change
  uv run scripts/update_deadlines.py --no-claude  # heuristics only
  uv run scripts/update_deadlines.py --only CSF   # only rows whose name contains CSF
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

HTML_FILE = Path(__file__).resolve().parent.parent / "conference_deadlines.html"
CSV_RE = re.compile(
    r'(<script type="text/csv" id="csv-data">\n)(.*?)(</script>)', re.DOTALL
)
FIELDS = ["name", "tier", "date", "location", "url"]

CLAUDE_CMD = [
    "nono", "run", "--profile", "claude-elan", "--allow-cwd", "--",
    "claude", "-p", "--model", "sonnet",
    "--allowedTools", "WebFetch", "WebSearch",
]

# A next-year deadline should land roughly a year after the old one.
MIN_SHIFT, MAX_SHIFT = 270, 460


@dataclass
class Row:
    name: str
    tier: str
    date: str
    location: str
    url: str
    # filled in while updating
    new: dict | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def wants_abstract(self) -> bool:
        return "abstract" in self.name.lower()

    def old_date(self) -> dt.date | None:
        try:
            return dt.date.fromisoformat(self.date)
        except ValueError:
            return None


# --------------------------------------------------------------------------
# CSV block in the HTML file
# --------------------------------------------------------------------------

def load(html: str) -> list[Row]:
    m = CSV_RE.search(html)
    if not m:
        sys.exit("CSV block not found in " + str(HTML_FILE))
    reader = csv.DictReader(io.StringIO(m.group(2)))
    return [Row(**{k: (r.get(k) or "").strip() for k in FIELDS}) for r in reader]


def dump(html: str, rows: list[Row]) -> str:
    buf = io.StringIO()
    buf.write(",".join(FIELDS) + "\n")
    w = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\n")
    for r in rows:
        w.writerow([r.name, r.tier, r.date, r.location, r.url])
    return CSV_RE.sub(lambda m: m.group(1) + buf.getvalue() + m.group(3), html, count=1)


# --------------------------------------------------------------------------
# Guessing next year's name / URL
# --------------------------------------------------------------------------

def edition_year(row: Row) -> int | None:
    for text in (row.name, row.url):
        years = [int(y) for y in re.findall(r"(?<!\d)(20[2-4]\d)(?!\d)", text)]
        if years:
            return max(years)
    return None


def bump(text: str, year: int, *, short: bool) -> str:
    """Replace `year` by year+1; with short=True also e.g. pldi27 -> pldi28."""
    text = re.sub(rf"(?<!\d){year}(?!\d)", str(year + 1), text)
    if short:
        yy, ny = f"{year % 100:02d}", f"{(year + 1) % 100:02d}"
        text = re.sub(rf"(?<=[A-Za-z]){yy}(?!\d)", ny, text)
    return text


def next_name(row: Row, year: int | None) -> str:
    name = bump(row.name, year, short=False) if year else row.name
    # Co-locations ("@ FLoC") change between years; don't carry them over blindly.
    return re.sub(r"\s*@.*$", "", name)


def next_url(row: Row, year: int | None) -> str:
    return bump(row.url, year, short=True) if year else row.url


# --------------------------------------------------------------------------
# Extracting deadlines from a page
# --------------------------------------------------------------------------

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
MON = r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?"
ORD = r"(?:st|nd|rd|th)?"
DATE_PATTERNS = [
    # 2027-01-28
    (re.compile(r"(?<!\d)(20\d\d)-(\d\d)-(\d\d)(?!\d)"), ("y", "m", "d")),
    # January 28, 2027 / Jan. 28th 2027
    (re.compile(rf"\b{MON}\s+(\d{{1,2}}){ORD},?\s+(20\d\d)\b", re.I), ("M", "d", "y")),
    # 28 January 2027 / Thu 28 Jan 2027 / 28th of January, 2027
    (re.compile(rf"\b(\d{{1,2}}){ORD}\s+(?:of\s+)?{MON},?\s+(20\d\d)\b", re.I), ("d", "M", "y")),
]

KEYWORDS = {
    "submission": 3, "deadline": 2, "paper": 2, "due": 1,
    "notification": -5, "acceptance": -3, "camera": -5, "final version": -4,
    "rebuttal": -4, "response": -4, "artifact": -4, "conference": -2,
    "workshop": -2, "tutorial": -4, "poster": -3, "demo": -3, "student": -3,
    "travel": -4, "early": -3, "proposal": -4, "competition": -4,
    "doctoral": -4, "talk": -2,
}


def find_dates(text: str) -> list[dt.date]:
    out = []
    for pat, order in DATE_PATTERNS:
        for m in pat.finditer(text):
            parts = dict(zip(order, m.groups()))
            try:
                month = (MONTHS[parts["M"][:3].lower()] if "M" in parts
                         else int(parts["m"]))
                out.append(dt.date(int(parts["y"]), month, int(parts["d"])))
            except (ValueError, KeyError):
                pass
    return out


WEEKDAYS = re.compile(r"\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?", re.I)


def label_letters(text: str) -> str:
    """The letters of `text` that are not part of a date or weekday."""
    for pat, _ in DATE_PATTERNS:
        text = pat.sub("", text)
    text = WEEKDAYS.sub("", text)
    return re.sub(r"[^A-Za-z]|\bAoE\b", "", text)


def score(context: str, wants_abstract: bool) -> int:
    c = context.lower()
    s = sum(w for k, w in KEYWORDS.items() if k in c)
    if "abstract" in c:
        s += 4 if wants_abstract else -4
    elif wants_abstract:
        s -= 3
    if "registration" in c and not wants_abstract:
        s -= 2
    return s


BLOCKS = ["tr", "li", "p", "dt", "dd", "h1", "h2", "h3", "h4", "h5", "h6", "td", "div", "span"]


def candidates(html: str) -> list[tuple[str, dt.date]]:
    """(context, date) pairs, one per innermost text block containing a date."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "s", "del", "strike"]):  # struck = superseded
        tag.decompose()
    out: list[tuple[str, dt.date]] = []
    seen = set()
    # Prefer researchr's important-dates table, where each row is self-contained.
    blocks = soup.select("table.important-dates-in-sidebar tr") or soup.find_all(BLOCKS)
    dated = {id(el) for el in blocks if find_dates(el.get_text(" ", strip=True))}
    for el in blocks:
        if id(el) not in dated:
            continue
        # skip wrappers: a child block that also contains a date is more precise
        if any(id(ch) in dated for ch in el.find_all(BLOCKS)):
            continue
        text = el.get_text(" ", strip=True)
        if len(find_dates(text)) == 1:
            context = text
            if len(label_letters(text)) < 6:  # bare date: label is in the previous sibling
                prev = el.find_previous_sibling() or el.parent
                if prev is not None:
                    context = prev.get_text(" ", strip=True)[:200] + " " + text
            pairs = [(context, find_dates(text)[0])]
        else:
            pairs = split_contexts(el.get_text("\n"))
        for pair in pairs:
            if pair not in seen:
                seen.add(pair)
                out.append(pair)
    return out


def split_contexts(text: str) -> list[tuple[str, dt.date]]:
    """Per-date contexts for a block listing several dates, one per line or as
    "Abstract: Feb 15, Paper: Feb 22": the label is the text before the date."""
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    out = []
    for i, line in enumerate(lines):
        spans = sorted((m.start(), m.end()) for pat, _ in DATE_PATTERNS
                       for m in pat.finditer(line))
        prev_end = 0
        for start, end in spans:
            if start < prev_end:  # overlapping match of another pattern
                continue
            context = line[prev_end:end]
            if len(label_letters(context)) < 6 and i > 0:
                context = lines[i - 1] + " " + context
            out += [(context, d) for d in find_dates(line[start:end])]
            prev_end = end
    return out


def pick(row: Row, cands: list[tuple[str, dt.date]]) -> dt.date | None:
    old = row.old_date()
    if old is None:
        return None
    best = None
    for context, d in cands:
        shift = (d - old).days
        if not MIN_SHIFT <= shift <= MAX_SHIFT:
            continue
        s = score(context, row.wants_abstract)
        if s < 3:
            continue
        key = (abs(shift - 365) - 10 * s, d)  # close to anniversary, strong label
        if best is None or key < best[0]:
            best = (key, d)
    return best[1] if best else None


# --------------------------------------------------------------------------
# Heuristic pass
# --------------------------------------------------------------------------

def fetch(client: httpx.Client, url: str) -> tuple[str, str] | None:
    try:
        r = client.get(url)
    except httpx.HTTPError:
        return None
    if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
        return None
    return str(r.url), r.text


def heuristic(row: Row, client: httpx.Client) -> None:
    year = edition_year(row)
    url = next_url(row, year)
    got = fetch(client, url)
    if got is None:
        row.notes.append(f"no page at {url}")
        return
    final_url, html = got
    if year and url != row.url and str(year + 1) not in final_url + html[:20000]:
        row.notes.append(f"{url} redirected to {final_url}, which is not about {year + 1}")
        return
    d = pick(row, candidates(html))
    if d is None:
        row.notes.append(f"no plausible deadline found on {final_url}")
        return
    row.new = {
        "name": next_name(row, year), "tier": row.tier, "date": d.isoformat(),
        "location": "", "url": url, "source": "heuristic",
    }


def dedupe(rows: list[Row]) -> None:
    """Sibling rows (R1/R2, Summer/Fall) must not collapse onto one date."""
    seen: dict[tuple, Row] = {}
    for r in rows:
        if not r.new:
            continue
        key = (r.new["url"], r.new["date"], r.wants_abstract)
        if key in seen:
            for x in (r, seen[key]):
                x.notes.append(f"date {x.new['date']} also matched a sibling row")
                x.new = None
        else:
            seen[key] = r


# --------------------------------------------------------------------------
# Claude fallback
# --------------------------------------------------------------------------

PROMPT = """You are updating a list of academic conference submission deadlines.
Today is {today}. Each entry below is a deadline that has passed. For each one,
find the NEXT edition of the same venue and the same kind of deadline (abstract
vs. full paper; for multi-cycle venues, the same round/cycle).

Entries with "proposed" were already found by a script: verify the date and
URL, and fill in the location (and a co-location suffix like "@ FLoC" in the
name if applicable). For the others, search the web.

Use official conference pages (call for papers / important dates). Only report
dates that are officially announced, not guesses. Dates are AoE, give them as
YYYY-MM-DD. Location format: "City, Country" (e.g. "Nijmegen, Netherlands").

Reply with ONLY a JSON array, no prose, one object per entry, in this shape:
[{{"id": 0, "name": "CSF 2028 Winter", "date": "2028-01-27",
   "location": "Paris, France", "url": "https://...", "found": true}}]
Use "found": false (other fields may be null) if the next edition's deadline
is not announced yet. Do not modify any files.

Entries:
{entries}
"""


def ask_claude(rows: list[Row], today: dt.date) -> None:
    entries = []
    for i, r in enumerate(rows):
        e = {"id": i, "name": r.name, "old_date": r.date,
             "old_location": r.location, "old_url": r.url}
        if r.new:
            e["proposed"] = {k: r.new[k] for k in ("name", "date", "url")}
        entries.append(e)
    prompt = PROMPT.format(today=today.isoformat(), entries=json.dumps(entries, indent=1))
    print(f"\nAsking Claude about {len(rows)} entr{'y' if len(rows) == 1 else 'ies'}...",
          file=sys.stderr)
    try:
        res = subprocess.run(CLAUDE_CMD, input=prompt, capture_output=True,
                             text=True, timeout=1800)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"Claude fallback failed: {e}", file=sys.stderr)
        return
    if res.returncode != 0:
        print(f"Claude exited with {res.returncode}:\n{res.stderr[-2000:]}", file=sys.stderr)
        return
    out = res.stdout
    try:
        answers = json.loads(out[out.index("["): out.rindex("]") + 1])
    except ValueError:
        print("Could not parse Claude's answer:\n" + out[-2000:], file=sys.stderr)
        return
    for a in answers:
        try:
            r = rows[int(a["id"])]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        if not a.get("found"):
            if r.new:  # Claude could not confirm the heuristic result: keep it, flag it
                r.notes.append("Claude could not confirm this")
            else:
                r.notes.append("Claude: not announced yet")
            continue
        try:
            d = dt.date.fromisoformat(a["date"])
        except (TypeError, ValueError):
            r.notes.append(f"Claude gave invalid date {a.get('date')!r}")
            continue
        old = r.old_date()
        if old and d <= old:
            r.notes.append(f"Claude gave {d}, not after the old deadline")
            continue
        if r.new and r.new["date"] != d.isoformat():
            r.notes.append(f"heuristic said {r.new['date']}, Claude says {d}")
        r.new = {
            "name": a.get("name") or (r.new or {}).get("name") or r.name,
            "tier": r.tier, "date": d.isoformat(),
            "location": a.get("location") or "",
            "url": a.get("url") or (r.new or {}).get("url") or r.url,
            "source": "claude" if not r.new else "heuristic+claude",
        }


# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="don't write the file")
    ap.add_argument("--no-claude", action="store_true", help="skip the Claude fallback")
    ap.add_argument("--only", help="only rows whose name contains this (case-insensitive)")
    ap.add_argument("--today", type=dt.date.fromisoformat, default=dt.date.today(),
                    help="pretend today is this date (YYYY-MM-DD)")
    args = ap.parse_args()

    html = HTML_FILE.read_text()
    rows = load(html)
    todo = [r for r in rows
            if (d := r.old_date()) and d < args.today
            and (not args.only or args.only.lower() in r.name.lower())]
    if not todo:
        print("Nothing to update.")
        return

    headers = {"User-Agent": "Mozilla/5.0 (compatible; deadline-updater/1.0)"}
    with httpx.Client(follow_redirects=True, timeout=20, headers=headers) as client:
        for r in todo:
            print(f"{r.name:40} ", end="", flush=True, file=sys.stderr)
            heuristic(r, client)
            print(r.new["date"] if r.new else "-", file=sys.stderr)
    dedupe(todo)

    if not args.no_claude:
        ask_claude(todo, args.today)

    changed = [r for r in todo if r.new]
    print()
    for r in changed:
        n = r.new
        print(f"[{n['source']}] {r.name} ({r.date}) -> {n['name']} ({n['date']}, "
              f"{n['location'] or 'location unknown'}) {n['url']}")
        for note in r.notes:
            print(f"    note: {note}")
        r.name, r.date, r.location, r.url = n["name"], n["date"], n["location"], n["url"]
    for r in todo:
        if not r.new:
            print(f"[unchanged] {r.name} ({r.date}): {'; '.join(r.notes) or 'no info'}")

    if changed and not args.dry_run:
        HTML_FILE.write_text(dump(html, rows))
        print(f"\nUpdated {len(changed)} row(s) in {HTML_FILE.name}.")
    elif changed:
        print("\n(dry run, file not written)")


if __name__ == "__main__":
    main()
