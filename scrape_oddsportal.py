
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import re
import csv
import json
import time
import hashlib
import sqlite3
import logging

from pathlib import Path
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# ==================================================
# CONFIG
# ==================================================

URL = "https://www.oddsportal.com/dropping-odds/"
DATA = Path("data")
DATA.mkdir(exist_ok=True)

LATEST = DATA / "oddsportal_latest.csv"
SIGNALS = DATA / "oddsportal_signals.csv"
DEBUG = DATA / "oddsportal_debug.json"
DATABASE = DATA / "oddsportal_history.sqlite"

PAGE_TIMEOUT = 30000
TOTAL_TIMEOUT = 110
MAX_ROWS = 500

FIELDS = [
    "scraped_at",
    "match",
    "league",
    "market",
    "selection",
    "handicap_line",
    "drop_pct",
    "odds_old",
    "odds_new",
    "best_current_odds",
    "match_url",
    "priority",
    "markets_count",
]

started = time.monotonic()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)

log = logging.getLogger("oddsportal")


# ==================================================
# HELPERS
# ==================================================

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def check_timeout():
    if time.monotonic() - started > TOTAL_TIMEOUT:
        raise TimeoutError("Total parser timeout")


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def number(value):
    try:
        return float(str(value).replace(",", "."))
    except (ValueError, TypeError):
        return None


def make_id(value):
    return hashlib.sha1(value.encode()).hexdigest()[:16]


# ==================================================
# USER CRITERIA
# ==================================================

def qualifies(old, new):
    if old is None or new is None or new >= old:
        return False

    # 4.00–5.00 -> 2.00 or lower
    if 4.00 <= old <= 5.00 and new <= 2.00:
        return True

    # 2.00–3.00 -> 1.80 or lower
    if 2.00 <= old <= 3.00 and new <= 1.80:
        return True

    return False


# ==================================================
# MARKET DETECTION
# ==================================================

def classify_market(line):
    value = clean(line).upper()

    # Only full-time 1X2, not HT/FT or half-time markets
    if value in ("1X2", "1X2 FT", "1X2 FT ONLY"):
        return "1X2"

    # Totals
    if value.startswith("O/U") or "OVER/UNDER" in value:
        return "TOTAL"

    # Handicap
    if value.startswith("AH") or value.startswith("EH"):
        return "HANDICAP"

    return None


def get_handicap_line(line):
    match = re.search(r"(?:AH|EH)\s*([+-]\s*\d+(?:\.\d+)?)", line, re.I)

    if not match:
        return ""

    return match.group(1).replace(" ", "")


def is_negative_handicap(line):
    value = get_handicap_line(line)
    return value.startswith("-")


# ==================================================
# PARSE CARD
# ==================================================

def parse_card(card):
    lines = [
        clean(x)
        for x in card.get("text", "").splitlines()
        if clean(x)
    ]

    if not lines:
        return None

    # Find market label
    market_index = None
    market = None
    market_label = ""

    for i, line in enumerate(lines):
        detected = classify_market(line)

        if detected:
            market_index = i
            market = detected
            market_label = line
            break

    if market_index is None:
        return None

    # For the priority combination, only negative handicaps count
    handicap_line = ""

    if market == "HANDICAP":
        handicap_line = get_handicap_line(market_label)

        if not handicap_line.startswith("-"):
            return None

    # Find percentage and odds
    joined = " ".join(lines)

    pattern = re.search(
        r"(-\d{1,3})\s*%\s*"
        r"(\d{1,3}(?:[.,]\d{1,3})?)\s+"
        r"(\d{1,3}(?:[.,]\d{1,3})?)\s+"
        r"(\d{1,3}(?:[.,]\d{1,3})?)",
        joined
    )

    if not pattern:
        return None

    drop_pct = abs(float(pattern.group(1)))
    old = number(pattern.group(2))
    new = number(pattern.group(3))
    best = number(pattern.group(4))

    if not qualifies(old, new):
        return None

    # Selection usually follows market label
    selection = ""

    if market_index + 1 < len(lines):
        selection = lines[market_index + 1]

    # Competition: last breadcrumb before market label
    league = ""

    for line in reversed(lines[:market_index]):
        if line.lower() not in ("football", "/", "world"):
            league = line
            break

    # Extract teams from lines after "Best Current Odds"
    match_name = ""
    best_index = None

    for i, line in enumerate(lines):
        if "best current odds" in line.lower():
            best_index = i
            break

    if best_index is not None:
        tail = lines[best_index + 1:]

        # Remove date and time lines
        tail = [
            x for x in tail
            if not re.fullmatch(
                r"(today|tomorrow|\d{1,2}\s+\w+|\d{1,2}:\d{2})",
                x,
                re.I
            )
        ]

        if "-" in tail:
            dash = tail.index("-")

            if dash > 0 and dash + 1 < len(tail):
                home = tail[dash - 1]
                away = tail[dash + 1]
                match_name = f"{home} - {away}"

    if not match_name:
        match_name = clean(card.get("anchor_text")) or "Unknown match"

    return {
        "scraped_at": now(),
        "match": match_name,
        "league": league,
        "market": market,
        "selection": selection,
        "handicap_line": handicap_line,
        "drop_pct": drop_pct,
        "odds_old": old,
        "odds_new": new,
        "best_current_odds": best,
        "match_url": card.get("url", ""),
        "priority": "",
        "markets_count": 0,
    }


# ==================================================
# EXTRACT CARDS DIRECTLY FROM DROPPING ODDS PAGE
# ==================================================

def extract_cards(page):
    log.info("Extracting dropping odds cards from main page...")

    cards = page.evaluate("""
    () => {
        const anchors = Array.from(document.querySelectorAll('a[href]'));
        const result = [];
        const seen = new Set();

        for (const a of anchors) {
            const href = a.href || '';

            if (
                !href.includes('/football/h2h/') &&
                !href.includes('/match/')
            ) {
                continue;
            }

            let node = a;
            let best = null;

            // Find the smallest ancestor containing the full odds card
            for (let depth = 0; depth < 10 && node; depth++) {
                const text = (node.innerText || '').trim();

                const hasPercent = /-\\d{1,3}\\s*%/.test(text);
                const hasBest = /Best Current Odds/i.test(text);
                const hasMarket = /1X2|O\\/U|AH\\s*[+-]|EH\\s*[+-]|DNB|HT\\/FT/i.test(text);

                if (
                    hasPercent &&
                    hasBest &&
                    hasMarket &&
                    text.length < 5000
                ) {
                    best = {
                        url: href,
                        anchor_text: (a.innerText || a.textContent || '').trim(),
                        text: text
                    };
                    break;
                }

                node = node.parentElement;
            }

            if (!best) continue;

            const key = best.text;

            if (seen.has(key)) continue;

            seen.add(key);
            result.push(best);
        }

        return result;
    }
    """)

    log.info("Cards extracted: %s", len(cards))

    return cards[:MAX_ROWS]


# ==================================================
# SIGNAL GROUPING
# ==================================================

def group_signals(rows):
    groups = {}

    for row in rows:
        key = make_id(row["match"] + "|" + row["league"])

        if key not in groups:
            groups[key] = []

        groups[key].append(row)

    result = []

    for match_rows in groups.values():
        markets = set(row["market"] for row in match_rows)

        count = len(markets)

        if count >= 3:
            priority = "TOP"
        elif count == 2:
            priority = "HIGH"
        else:
            priority = "NORMAL"

        for row in match_rows:
            row["markets_count"] = count
            row["priority"] = priority
            result.append(row)

    return result


# ==================================================
# DATABASE
# ==================================================

def init_db():
    with sqlite3.connect(DATABASE) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS odds_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scraped_at TEXT,
                match TEXT,
                league TEXT,
                market TEXT,
                selection TEXT,
                odds_old REAL,
                odds_new REAL,
                best_current_odds REAL,
                drop_pct REAL,
                match_url TEXT,
                UNIQUE(match, league, market, selection,
                       odds_old, odds_new)
            )
        """)


def save_db(rows):
    inserted = 0

    with sqlite3.connect(DATABASE) as conn:
        for row in rows:
            before = conn.total_changes

            conn.execute("""
                INSERT OR IGNORE INTO odds_history (
                    scraped_at, match, league, market,
                    selection, odds_old, odds_new,
                    best_current_odds, drop_pct, match_url
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                row["scraped_at"],
                row["match"],
                row["league"],
                row["market"],
                row["selection"],
                row["odds_old"],
                row["odds_new"],
                row["best_current_odds"],
                row["drop_pct"],
                row["match_url"],
            ))

            if conn.total_changes > before:
                inserted += 1

    return inserted


# ==================================================
# CSV
# ==================================================

def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=FIELDS,
            extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(rows)


# ==================================================
# MAIN
# ==================================================

def main():
    log.info("ODDSPORTAL PARSER START")
    log.info("URL: %s", URL)

    debug = {
        "started_at": now(),
        "url": URL,
        "cards_found": 0,
        "qualifying_rows": 0,
        "signals": 0,
        "errors": [],
    }

    init_db()

    rows = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )

        page = browser.new_page(
            viewport={"width": 1440, "height": 1000},
            locale="en-US",
        )

        page.set_default_timeout(PAGE_TIMEOUT)

        try:
            log.info("Opening dropping odds page...")

            response = page.goto(
                URL,
                wait_until="domcontentloaded",
                timeout=PAGE_TIMEOUT
            )

            debug["http_status"] = response.status if response else None

            log.info("HTTP status: %s", debug["http_status"])

            page.wait_for_timeout(1500)

            debug["page_title"] = page.title()
            debug["final_url"] = page.url

            cards = extract_cards(page)
            debug["cards_found"] = len(cards)

            log.info("Parsing cards...")

            for index, card in enumerate(cards, 1):
                check_timeout()

                row = parse_card(card)

                if row:
                    rows.append(row)

                if index % 10 == 0:
                    log.info(
                        "Processed %s/%s cards; qualifying: %s",
                        index,
                        len(cards),
                        len(rows)
                    )

            signals = group_signals(rows)

            write_csv(LATEST, rows)
            write_csv(SIGNALS, signals)

            inserted = save_db(rows)

            debug["qualifying_rows"] = len(rows)
            debug["signals"] = len(signals)
            debug["new_database_records"] = inserted
            debug["finished_at"] = now()
            debug["duration_seconds"] = round(
                time.monotonic() - started,
                2
            )
            debug["result"] = "completed"

            with open(DEBUG, "w", encoding="utf-8") as f:
                json.dump(debug, f, ensure_ascii=False, indent=2)

            log.info("==============================")
            log.info("Cards found: %s", len(cards))
            log.info("Qualifying rows: %s", len(rows))
            log.info("Signals: %s", len(signals))
            log.info("New DB records: %s", inserted)
            log.info("Duration: %s sec", debug["duration_seconds"])
            log.info("==============================")

        finally:
            browser.close()


if __name__ == "__main__":
    try:
        main()

    except TimeoutError as exc:
        log.error("TIMEOUT: %s", exc)

        with open(DEBUG, "w", encoding="utf-8") as f:
            json.dump({
                "result": "timeout",
                "error": str(exc),
                "finished_at": now(),
            }, f, ensure_ascii=False, indent=2)

        raise SystemExit(0)

    except Exception as exc:
        log.exception("FATAL ERROR")

        with open(DEBUG, "w", encoding="utf-8") as f:
            json.dump({
                "result": "error",
                "error": str(exc),
                "finished_at": now(),
            }, f, ensure_ascii=False, indent=2)

        raise
