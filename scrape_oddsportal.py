
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OddsPortal football odds parser.

Features:
- Hard timeout for the complete run.
- Page navigation timeout.
- Debug logging.
- Saves page diagnostics.
- Detects match links.
- Extracts odds only from explicitly labelled opening/current values.
- Applies user's odds-drop criteria.
- Groups signals by match and market.
"""

import os
import re
import csv
import json
import time
import hashlib
import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# --------------------------------------------------
# CONFIG
# --------------------------------------------------

BASE_URL = "https://www.oddsportal.com"
START_URL = os.getenv(
    "ODDSPORTAL_URL",
    "https://www.oddsportal.com/dropping-odds/"
)

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

LATEST_CSV = DATA_DIR / "oddsportal_latest.csv"
SIGNALS_CSV = DATA_DIR / "oddsportal_signals.csv"
HISTORY_CSV = DATA_DIR / "oddsportal_history.csv"
DEBUG_JSON = DATA_DIR / "oddsportal_debug.json"
DATABASE = DATA_DIR / "oddsportal_history.sqlite"

PAGE_TIMEOUT_MS = 30_000
TOTAL_TIMEOUT_SECONDS = 110
MAX_MATCHES = 250

HEADLESS = True

CSV_FIELDS = [
    "scraped_at",
    "match",
    "league",
    "market",
    "selection",
    "odds_old",
    "odds_new",
    "drop_pct",
    "match_url",
    "priority",
    "markets_count",
]

started_at = time.monotonic()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)

log = logging.getLogger("oddsportal")


# --------------------------------------------------
# HELPERS
# --------------------------------------------------

def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def check_deadline():
    elapsed = time.monotonic() - started_at
    if elapsed > TOTAL_TIMEOUT_SECONDS:
        raise TimeoutError(
            f"Total execution timeout: {TOTAL_TIMEOUT_SECONDS}s"
        )


def normalize_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


def safe_float(value):
    if value is None:
        return None

    value = str(value).replace(",", ".").strip()

    match = re.search(r"(?<!\d)(\d{1,3}(?:\.\d{1,3})?)(?!\d)", value)

    if not match:
        return None

    try:
        number = float(match.group(1))
        if 1.01 <= number <= 100:
            return number
    except ValueError:
        pass

    return None


def match_id(url):
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------
# USER ODDS CRITERIA
# --------------------------------------------------

def qualifies(old, new):
    if old is None or new is None:
        return False

    if new >= old:
        return False

    # Initial odds 4.00–5.00 -> 2.00 or lower
    if 4.00 <= old <= 5.00 and new <= 2.00:
        return True

    # Initial odds 2.00–3.00 -> 1.80 or lower
    if 2.00 <= old <= 3.00 and new <= 1.80:
        return True

    return False


def drop_percent(old, new):
    if not old:
        return 0

    return round((old - new) / old * 100, 2)


# --------------------------------------------------
# MARKET CLASSIFICATION
# --------------------------------------------------

def classify_market(text):
    t = (text or "").lower()

    # Handicap / Asian handicap
    if (
        "handicap" in t
        or "asian handicap" in t
        or "азиатск" in t
        or "фора" in t
    ):
        return "HANDICAP"

    # Total goals
    if (
        "over/under" in t
        or "total goals" in t
        or "goals over" in t
        or "тотал" in t
        or "over " in t
        or "under " in t
    ):
        return "TOTAL"

    # Match result
    if (
        "1x2" in t
        or "match winner" in t
        or "full time result" in t
        or "home win" in t
        or "away win" in t
        or "draw" in t
        or "перемога" in t
    ):
        return "1X2"

    return None


# --------------------------------------------------
# ODDS EXTRACTION
# --------------------------------------------------

def extract_labelled_odds(text):
    """
    Conservative extraction:
    requires explicit opening/current labels.
    Does not treat arbitrary numbers as odds.
    """

    text = normalize_text(text)

    old_patterns = [
        r"(?:opening|initial|starting|old)\s+odds?\s*[:=]?\s*(\d{1,2}(?:[.,]\d{1,3})?)",
        r"(?:odds?\s+opening|odds?\s+initial)\s*[:=]?\s*(\d{1,2}(?:[.,]\d{1,3})?)",
    ]

    new_patterns = [
        r"(?:current|latest|new)\s+odds?\s*[:=]?\s*(\d{1,2}(?:[.,]\d{1,3})?)",
        r"(?:odds?\s+current|odds?\s+latest)\s*[:=]?\s*(\d{1,2}(?:[.,]\d{1,3})?)",
    ]

    old = None
    new = None

    for pattern in old_patterns:
        found = re.search(pattern, text, re.I)
        if found:
            old = safe_float(found.group(1))
            break

    for pattern in new_patterns:
        found = re.search(pattern, text, re.I)
        if found:
            new = safe_float(found.group(1))
            break

    return old, new


# --------------------------------------------------
# DATABASE
# --------------------------------------------------

def init_db():
    with sqlite3.connect(DATABASE) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS odds_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scraped_at TEXT,
                match_id TEXT,
                match TEXT,
                market TEXT,
                selection TEXT,
                odds_old REAL,
                odds_new REAL,
                drop_pct REAL,
                match_url TEXT,
                UNIQUE(match_id, market, selection, odds_old, odds_new)
            )
        """)


def save_database(rows):
    inserted = 0

    with sqlite3.connect(DATABASE) as conn:
        for row in rows:
            before = conn.total_changes

            conn.execute("""
                INSERT OR IGNORE INTO odds_history (
                    scraped_at, match_id, match, market,
                    selection, odds_old, odds_new,
                    drop_pct, match_url
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                row["scraped_at"],
                row["match_id"],
                row["match"],
                row["market"],
                row["selection"],
                row["odds_old"],
                row["odds_new"],
                row["drop_pct"],
                row["match_url"],
            ))

            if conn.total_changes > before:
                inserted += 1

    return inserted


# --------------------------------------------------
# CSV
# --------------------------------------------------

def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=CSV_FIELDS,
            extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(rows)


def append_history(rows):
    exists = HISTORY_CSV.exists()

    with open(HISTORY_CSV, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=CSV_FIELDS,
            extrasaction="ignore"
        )

        if not exists:
            writer.writeheader()

        writer.writerows(rows)


# --------------------------------------------------
# PAGE COLLECTION
# --------------------------------------------------

def collect_match_links(page):
    log.info("Searching for football match links...")

    links = page.locator("a[href]").evaluate_all("""
        elements => elements.map(a => ({
            href: a.href,
            text: (a.innerText || a.textContent || '').trim()
        }))
    """)

    result = {}

    for item in links:
        href = item.get("href", "")
        label = normalize_text(item.get("text", ""))

        if not href:
            continue

        if "/football/h2h/" not in href and "/match/" not in href:
            continue

        if href not in result:
            result[href] = label

    matches = list(result.items())[:MAX_MATCHES]

    log.info("Match links found: %s", len(matches))

    return matches


def extract_page_diagnostics(page):
    return page.evaluate("""
        () => ({
            title: document.title,
            url: location.href,
            bodyText: (document.body?.innerText || '').slice(0, 30000),
            links: Array.from(document.querySelectorAll('a[href]'))
                .slice(0, 300)
                .map(a => ({
                    text: (a.innerText || a.textContent || '').trim(),
                    href: a.href
                })),
            htmlLength: document.documentElement?.outerHTML?.length || 0
        })
    """)


# --------------------------------------------------
# MATCH CARD EXTRACTION
# --------------------------------------------------

def collect_card_data(page, match_url, anchor_label):
    """
    Reads the match page and searches for explicitly labelled odds.
    """

    check_deadline()

    try:
        response = page.goto(
            match_url,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT_MS
        )

        if response and response.status >= 400:
            log.warning(
                "Match page HTTP %s: %s",
                response.status,
                match_url
            )
            return []

        page.wait_for_timeout(700)

    except PlaywrightTimeoutError:
        log.warning("Match page navigation timeout: %s", match_url)
        return []

    except Exception as exc:
        log.warning("Match page error: %s", exc)
        return []

    data = page.evaluate("""
        () => {
            const selectors = [
                '[data-old-odds]',
                '[data-opening-odds]',
                '[data-current-odds]',
                '[data-odds]',
                '[class*="odds"]',
                '[class*="market"]',
                '[class*="outcome"]',
                '[class*="bookmaker"]'
            ];

            const nodes = new Set();

            for (const selector of selectors) {
                document.querySelectorAll(selector).forEach(el => {
                    nodes.add(el);
                });
            }

            return Array.from(nodes).slice(0, 500).map(el => ({
                text: (el.innerText || el.textContent || '').trim().slice(0, 1500),
                old: el.getAttribute('data-old-odds')
                    || el.getAttribute('data-opening-odds')
                    || '',
                current: el.getAttribute('data-current-odds')
                    || '',
                html: el.outerHTML.slice(0, 2500)
            }));
        }
    """)

    rows = []
    timestamp = now_utc()

    for item in data:
        text = normalize_text(item.get("text", ""))

        if not text:
            continue

        market = classify_market(text)

        if not market:
            continue

        old = safe_float(item.get("old"))
        new = safe_float(item.get("current"))

        if old is None or new is None:
            old2, new2 = extract_labelled_odds(text)
            old = old if old is not None else old2
            new = new if new is not None else new2

        if old is None or new is None:
            continue

        if not qualifies(old, new):
            continue

        rows.append({
            "scraped_at": timestamp,
            "match_id": match_id(match_url),
            "match": anchor_label or "Unknown match",
            "league": "",
            "market": market,
            "selection": "",
            "odds_old": old,
            "odds_new": new,
            "drop_pct": drop_percent(old, new),
            "match_url": match_url,
            "priority": "",
            "markets_count": 0,
        })

    return rows


# --------------------------------------------------
# SIGNAL GROUPING
# --------------------------------------------------

def group_signals(rows):
    grouped = {}

    for row in rows:
        key = row["match_id"]

        if key not in grouped:
            grouped[key] = {
                "match": row["match"],
                "match_url": row["match_url"],
                "markets": {},
                "rows": [],
            }

        grouped[key]["markets"][row["market"]] = True
        grouped[key]["rows"].append(row)

    signals = []

    for item in grouped.values():
        count = len(item["markets"])

        if count >= 3:
            priority = "TOP"
        elif count == 2:
            priority = "HIGH"
        else:
            priority = "NORMAL"

        for row in item["rows"]:
            row["priority"] = priority
            row["markets_count"] = count
            signals.append(row)

    return signals


# --------------------------------------------------
# DEBUG OUTPUT
# --------------------------------------------------

def save_debug(data):
    with open(DEBUG_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# --------------------------------------------------
# MAIN
# --------------------------------------------------

def main():
    log.info("======================================")
    log.info("ODDSPORTAL PARSER STARTED")
    log.info("Target: %s", START_URL)
    log.info("Total timeout: %s seconds", TOTAL_TIMEOUT_SECONDS)
    log.info("======================================")

    debug = {
        "started_at": now_utc(),
        "start_url": START_URL,
        "errors": [],
        "match_links_found": 0,
        "matches_checked": 0,
        "odds_rows_found": 0,
        "signals_found": 0,
    }

    init_db()

    all_rows = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        context = browser.new_context(
            viewport={"width": 1440, "height": 1000},
            locale="en-US",
            timezone_id="Europe/Kyiv",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )

        page = context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT_MS)

        try:
            log.info("Opening OddsPortal dropping odds page...")

            response = page.goto(
                START_URL,
                wait_until="domcontentloaded",
                timeout=PAGE_TIMEOUT_MS
            )

            debug["http_status"] = response.status if response else None

            log.info(
                "Main page loaded. HTTP status: %s",
                debug["http_status"]
            )

            page.wait_for_timeout(1500)

            diagnostics = extract_page_diagnostics(page)
            debug["page_title"] = diagnostics.get("title")
            debug["page_url"] = diagnostics.get("url")
            debug["html_length"] = diagnostics.get("htmlLength")
            debug["body_text"] = diagnostics.get("bodyText", "")[:12000]

            log.info("Page title: %s", debug["page_title"])
            log.info("HTML length: %s", debug["html_length"])

            with open(
                DATA_DIR / "oddsportal_page_debug.json",
                "w",
                encoding="utf-8"
            ) as f:
                json.dump(diagnostics, f, ensure_ascii=False, indent=2)

            match_links = collect_match_links(page)
            debug["match_links_found"] = len(match_links)

            for index, (url, label) in enumerate(match_links, start=1):
                check_deadline()

                log.info(
                    "Checking match %s/%s: %s",
                    index,
                    len(match_links),
                    label or url
                )

                match_page = context.new_page()
                match_page.set_default_timeout(PAGE_TIMEOUT_MS)

                try:
                    rows = collect_card_data(
                        match_page,
                        url,
                        label
                    )

                    all_rows.extend(rows)
                    debug["matches_checked"] += 1

                    if rows:
                        log.info(
                            "Found qualifying odds: %s",
                            len(rows)
                        )

                except TimeoutError:
                    raise

                except Exception as exc:
                    message = f"{url}: {type(exc).__name__}: {exc}"
                    debug["errors"].append(message)
                    log.warning(message)

                finally:
                    match_page.close()

            signals = group_signals(all_rows)

            debug["odds_rows_found"] = len(all_rows)
            debug["signals_found"] = len(signals)

            write_csv(LATEST_CSV, all_rows)
            write_csv(SIGNALS_CSV, signals)

            inserted = save_database(all_rows)

            # Append only newly inserted database rows.
            # This prevents unchanged values from being duplicated.
            if inserted:
                # SQLite is the canonical history store.
                # CSV latest/signals contain this run's results.
                log.info("New database records: %s", inserted)

            debug["finished_at"] = now_utc()
            debug["duration_seconds"] = round(
                time.monotonic() - started_at, 2
            )
            debug["result"] = "completed"

            save_debug(debug)

            log.info("--------------------------------------")
            log.info("Matches found: %s", debug["match_links_found"])
            log.info("Matches checked: %s", debug["matches_checked"])
            log.info("Qualifying odds: %s", debug["odds_rows_found"])
            log.info("Signals: %s", debug["signals_found"])
            log.info("Finished in %s seconds", debug["duration_seconds"])
            log.info("--------------------------------------")

        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    try:
        main()

    except TimeoutError as exc:
        log.error("TIMEOUT: %s", exc)

        save_debug({
            "finished_at": now_utc(),
            "result": "timeout",
            "error": str(exc),
            "duration_seconds": round(
                time.monotonic() - started_at, 2
            ),
        })

        raise SystemExit(0)

    except Exception as exc:
        log.exception("FATAL ERROR")

        save_debug({
            "finished_at": now_utc(),
            "result": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "duration_seconds": round(
                time.monotonic() - started_at, 2
            ),
        })

        raise
