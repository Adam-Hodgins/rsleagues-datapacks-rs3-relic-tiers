import json
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from time import perf_counter


BASE_URL = (
    "https://secure.runescape.com/m=hiscore_oldschool_seasonal/overall"
    "?category_type=1&table=1&page={page_number}"
)
MAX_PAGE_GUESS = 20000
REQUEST_TIMEOUT_SECONDS = 20
MAX_REQUEST_RETRIES = 4
RETRY_BACKOFF_SECONDS = 2
VERBOSE = False
SHOW_PROGRESS = True
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) "
        "Gecko/20100101 Firefox/128.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://secure.runescape.com/m=hiscore_oldschool_seasonal/overall",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

TIER_THRESHOLDS = [
    ("Tier 1", 10),
    ("Tier 2", 750),
    ("Tier 3", 2000),
    ("Tier 4", 3500),
    ("Tier 5", 6000),
    ("Tier 6", 13000),
    ("Tier 7", 22000),
]

DATA_DIR = Path(__file__).parent / "Data"
OUTPUT_FILENAME = "rank-thresholds.json"

session = requests.Session()
session.headers.update(REQUEST_HEADERS)


def prepare_output_path():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    output_path = DATA_DIR / OUTPUT_FILENAME
    if output_path.exists():
        archive_number = 1
        while (DATA_DIR / f"{output_path.stem}_{archive_number}{output_path.suffix}").exists():
            archive_number += 1
        archive_path = DATA_DIR / f"{output_path.stem}_{archive_number}{output_path.suffix}"
        output_path.rename(archive_path)
    return output_path


def load_previous_output():
    output_path = DATA_DIR / OUTPUT_FILENAME
    if not output_path.exists():
        return None
    try:
        with output_path.open(encoding="utf-8") as output_file:
            return json.load(output_file)
    except (json.JSONDecodeError, OSError):
        return None


def _previous_tier_players(previous_data, tier_name):
    if not previous_data:
        return None
    for tier in previous_data.get("ranks", []):
        if tier.get("name") == tier_name:
            value = tier.get("players_qualified")
            return value if isinstance(value, int) else None
    return None


def _change(current_value, previous_value):
    if previous_value is None:
        return None
    return current_value - previous_value


def _parse_int(raw_value):
    return int(raw_value.replace(",", ""))


def print_progress(step, total_steps, message):
    percent = (step / total_steps) * 100
    print(f"[{step}/{total_steps} | {percent:5.1f}%] {message}")


def print_inline_status(message):
    print(f"\r{message}", end="", flush=True)


def _fetch_hiscores(url):
    last_error = None
    for attempt in range(1, MAX_REQUEST_RETRIES + 1):
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            return response
        except requests.HTTPError as exc:
            last_error = exc
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code in (403, 429, 500, 502, 503, 504) and attempt < MAX_REQUEST_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue
            raise
        except requests.RequestException as exc:
            last_error = exc
            if attempt < MAX_REQUEST_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue
            raise
    raise last_error


def _parse_hiscores_html(html):
    soup = BeautifulSoup(html, "html.parser")
    parsed_rows = []

    for row in soup.select("tr.personal-hiscores__row"):
        cells = row.find_all("td")
        if len(cells) < 3:
            continue
        rank = _parse_int(cells[0].text.strip())
        score = _parse_int(cells[-1].text.strip())
        parsed_rows.append((rank, score))
    if parsed_rows:
        return tuple(parsed_rows)

    content_div = soup.find("div", {"id": "contentHiscores"})
    if not content_div:
        return tuple()

    table = content_div.find("table")
    if not table:
        return tuple()

    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 3:
            continue
        rank = _parse_int(cells[0].text.strip())
        score = _parse_int(cells[2].text.strip())
        parsed_rows.append((rank, score))

    return tuple(parsed_rows)


@lru_cache(maxsize=None)
def get_hiscores_page(page_number):
    """
    Returns a tuple of (rank, score) rows for a hiscores page.
    Cached so repeated binary searches do not re-fetch pages.
    """
    url = BASE_URL.format(page_number=page_number)
    response = _fetch_hiscores(url)
    return _parse_hiscores_html(response.text)


def get_page_signature(page_number):
    """
    Lightweight fingerprint used to detect "out of range" pages.
    OSRS returns page 1 when page_number is too high.
    """
    rows = get_hiscores_page(page_number)
    if not rows:
        return tuple()
    return rows[0], rows[-1]


def get_total_pages():
    first_page_signature = get_page_signature(1)

    low = 2
    high = MAX_PAGE_GUESS
    last_valid_page = 1
    iteration = 0

    while low <= high:
        iteration += 1
        mid = (low + high) // 2
        if VERBOSE:
            print(f"Checking page: {mid}")
        elif SHOW_PROGRESS:
            print_inline_status(f"Finding total pages... iteration {iteration}, checking page {mid}")
        current_signature = get_page_signature(mid)

        if current_signature == first_page_signature:
            high = mid - 1
        else:
            last_valid_page = mid
            low = mid + 1

    if SHOW_PROGRESS and not VERBOSE:
        print()
    return last_valid_page


def get_last_rank_for_threshold(total_pages, threshold, min_page=1):
    """
    Binary-search the last page containing at least one player at/above threshold,
    then scan only that final page to get the precise last qualifying rank.
    """
    low = min_page
    high = total_pages
    best_rank = 0
    best_page = min_page
    iteration = 0

    while low <= high:
        iteration += 1
        mid = (low + high) // 2
        if VERBOSE:
            print(f"Checking page: {mid} for threshold {threshold}")
        elif SHOW_PROGRESS:
            print_inline_status(
                f"Finding cutoff {threshold:,}... iteration {iteration}, checking page {mid}"
            )

        rows = get_hiscores_page(mid)
        if not rows:
            high = mid - 1
            continue

        found_on_page = rows[0][1] >= threshold
        if found_on_page:
            low = mid + 1
            best_page = mid
        else:
            high = mid - 1

    if SHOW_PROGRESS and not VERBOSE:
        print()
    rows = get_hiscores_page(best_page)
    for rank, score in rows:
        if score >= threshold:
            best_rank = rank
        else:
            break

    return best_rank, best_page


start_time = perf_counter()
total_steps = len(TIER_THRESHOLDS) + 2
step = 0

total_pages = get_total_pages()
step += 1
if SHOW_PROGRESS:
    print_progress(step, total_steps, f"Total pages found: {total_pages}")
else:
    print(f"Total Pages: {total_pages}")

last_page_data = get_hiscores_page(total_pages)
total_players = last_page_data[-1][0] if last_page_data else 0
step += 1
if SHOW_PROGRESS:
    print_progress(step, total_steps, f"Total players found: {total_players:,}")
else:
    print(f"Total Players: {total_players}")

tier_results = {}
min_page_hint = 1
# Search highest thresholds first so min_page_hint stays valid (same as Ranks).
for tier_name, threshold in sorted(TIER_THRESHOLDS, key=lambda item: item[1], reverse=True):
    players, found_page = get_last_rank_for_threshold(total_pages, threshold, min_page_hint)
    tier_results[tier_name] = players
    min_page_hint = found_page
    step += 1
    if SHOW_PROGRESS:
        print_progress(
            step,
            total_steps,
            f"{tier_name} cutoff ({threshold:,}) found: {players:,} players",
        )
    else:
        print(f"Players with a score of {threshold} or more: {players}")

previous_data = load_previous_output()
previous_total_players = (
    previous_data.get("total_players")
    if previous_data and isinstance(previous_data.get("total_players"), int)
    else None
)

output_data = {
    "logged_at": datetime.now(timezone.utc).isoformat(),
    "total_players": total_players,
    "total_players_change": _change(total_players, previous_total_players),
    "ranks": [],
}

for tier_name, threshold in TIER_THRESHOLDS:
    players_qualified = tier_results[tier_name]
    previous_players_qualified = _previous_tier_players(previous_data, tier_name)
    output_data["ranks"].append(
        {
            "name": tier_name,
            "point_cutoff": threshold,
            "players_qualified": players_qualified,
            "players_qualified_change": _change(players_qualified, previous_players_qualified),
        }
    )

output_path = prepare_output_path()

with output_path.open("w", encoding="utf-8") as output_file:
    json.dump(output_data, output_file, indent=2)
    output_file.write("\n")

elapsed = perf_counter() - start_time
print()
print(f"Wrote rank data to {output_path}")
print(f"Completed in {elapsed:.2f}s")
