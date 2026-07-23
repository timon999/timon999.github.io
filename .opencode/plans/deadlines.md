# Plan: Conference submission deadline extractor

## Goal
A `uv run` script (PEP 723 inline dependencies) that takes a researchr-based
conference/track page URL and prints submission deadlines:
- single deadline -> `2026/7/21`
- multiple rounds -> `R1: 2026/11/19, R2: 2027/2/11`

## Findings (verified against both example URLs)
Both `conf.researchr.org/...` and `2027.ecoop.org/track/...` are the same
researchr platform. Deadlines live in:

    <table class="table table-hover important-dates-in-sidebar">
      <tr ...><td><strong>Tue 21 Jul 2026</strong><br/><strong>Submission Deadline</strong></td></tr>
      ...
      <tr ...><td>Thu 19 Nov 2026<br/>Round 1 submissions</td></tr>
      <tr ...><td>Thu 11 Feb 2027<br/>Round 2 Submissions</td></tr>
      <tr ...><td>Mon 11 - Fri 15 Jan 2027<br/>Round 1 Author response period</td></tr>  <!-- filtered out -->
      <tr ...><td>Thu 28 Jan 2027<br/>Round 1 Notification</td></tr>                        <!-- filtered out -->
    </table>

Date format: `DayOfWeek 1-2digits MonAbr 4digits` e.g. `Tue 21 Jul 2026`.
Month table maps `Jul->7`, `Nov->11`, `Feb->2`.

IWACO: one `<td>` containing "Submission" -> emit `2026/7/21`.
ECOOP: two `<td>`s containing "submission" (Round 1 submissions, Round 2
Submissions) and several non-submission rows (Notification, Author response)
that must be excluded by the "submission" substring filter.

## Algorithm
1. `httpx.get(url, follow_redirects=True)` with a UA header.
2. Parse HTML with BeautifulSoup; `soup.select_one('table.important-dates-in-sidebar')`.
3. For each `tr` -> read raw text of its `td` (joining `br`/elements with a
   space). Skip if lowercase text does not contain "submission".
4. Split into `(date_str, raw_label)`:
   - date via regex `(\d{1,2})\s+([A-Z][a-z]{2})\s+(\d{4})` -> `y/M/d`
   - label = text *after* the matched date, trimmed
5. Round via `round\s*(\d+)` (case-insensitive) on the raw label; if found,
   remove that span from the label and strip a trailing generic
   "submission"/"submissions" token -> `clean_label`.
   - ECOOP "Round 1 submissions" -> round=1, clean_label="" (handled by R-format)
   - "Abstract submission" -> round=None, clean_label="Abstract"
6. Formatting:
   - 1 submission row total -> bare date (IWACO case)
   - all rows have a round AND each round appears exactly once ->
     `R{n}: y/M/d` joined by ", "  (ECOOP case)
   - otherwise (multi non-round, or duplicates): label every row as
     `{prefix}{label}: y/M/d` where prefix is `R{n} ` when round set else "",
     joined by ", "  (e.g. "Abstract: 2026/7/1, Full paper: 2026/7/21")
   - when a clean_label is empty in this mode, just use the R-prefix or bare
     date so we never emit a dangling `: `.

## File to create
`deadlines.py` at repo root (PEP 723 inline metadata):

```python
# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx", "beautifulsoup4"]
# ///
import re, sys
import httpx
from bs4 import BeautifulSoup

MONTHS = {'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,
          'Jul':7,'Aug':8,'Sep':9,'Oct':10,'Nov':11,'Dec':12}

DATE_RE = re.compile(r'(\d{1,2})\s+([A-Z][a-z]{2})\s+(\d{4})')
ROUND_RE = re.compile(r'round\s*(\d+)', re.IGNORECASE)

def parse_date(text):
    m = DATE_RE.search(text)
    if not m: return None, None
    d, mon, y = m.group(1), m.group(2), m.group(3)
    return f"{y}/{MONTHS[mon]}/{int(d)}", m.end()

def fetch(url):
    headers = {'User-Agent': 'Mozilla/5.0 (compatible; deadlines-fetcher/1.0)',
               'Accept': 'text/html'}
    with httpx.Client(follow_redirects=True, timeout=30, headers=headers) as c:
        r = c.get(url); r.raise_for_status(); return r.text

def extract(html):
    soup = BeautifulSoup(html, 'html.parser')
    table = soup.select_one('table.important-dates-in-sidebar')
    if table is None:
        raise SystemExit("No 'Important Dates' table found on this page.")
    subs = []
    for tr in table.select('tr'):
        td = tr.find('td')
        if td is None: continue
        text = td.get_text(' ', strip=True)
        if 'submission' not in text.lower(): continue
        date, end = parse_date(text)
        if date is None: continue
        raw_label = text[end:].strip()
        rm = ROUND_RE.search(raw_label)
        round_n = int(rm.group(1)) if rm else None
        # strip the round span and a trailing generic 'submission(s)' word
        if rm:
            raw_label = (raw_label[:rm.start()] + raw_label[rm.end():]).strip()
        raw_label = re.sub(r'\bsubmissions?\b', '', raw_label, flags=re.IGNORECASE).strip(' -:/')
        subs.append((round_n, raw_label, date))
    return subs

def format(subs):
    if not subs: return ''
    if len(subs) == 1:
        # single submission row -> bare date
        return subs[0][2]
    rounds = [r for r, _, _ in subs if r is not None]
    if len(rounds) == len(subs) and len(set(rounds)) == len(subs):
        # classic rounds-only layout: R1/R2/R3...
        return ', '.join(f'R{r}: {d}' for r, _, d in sorted(subs))
    # otherwise: label each row, prefixing round if present
    parts = []
    for r, label, d in subs:
        prefix = f'R{r} ' if r is not None else ''
        if label:
            parts.append(f'{prefix}{label}: {d}')
        elif prefix:
            parts.append(f'{prefix.strip()}: {d}')
        else:
            parts.append(d)
    return ', '.join(parts)

def main(argv):
    if len(argv) != 2:
        print(f'usage: {argv[0]} <researchr page url>', file=sys.stderr); return 2
    out = format(extract(fetch(argv[1])))
    if not out:
        print('No submission deadlines found.', file=sys.stderr); return 1
    print(out); return 0

if __name__ == '__main__':
    sys.exit(main(sys.argv))
```

## Verification
```
uv run deadlines.py https://conf.researchr.org/home/splash-issta-2026/iwaco-2026
# expect: 2026/7/21
uv run deadlines.py https://2027.ecoop.org/track/ecoop-2027-technical-papers
# expect: R1: 2026/11/19, R2: 2027/2/11
```

## Notes / assumptions
- Only researchr-based sites are targeted (covers conf.researchr.org and
  per-conference domains like YYYY.ecoop.org, YYYY.splashcon.org running the
  same software). Non-researchr conference sites will hit the "No Important
  Dates table" error.
- Output is unpadded (M and D without leading zeros) to match the examples
  (e.g. `2026/7/21`, `2027/2/11`).
- The "submission" substring filter keeps IWACO's "Submission Deadline" and
  ECOOP's "Round 1 submissions"/"Round 2 Submissions" while excluding
  Notification / Author-response rows.
- Per user choice "Label them": when a track has multiple non-round submission
  rows (e.g. abstract + full paper), each is printed as `{Label}: {date}`;
  when rounds are present the `Round N` prefix is converted to an `R{n} `
  prefix and the generic "submission(s)" word is stripped from the label.