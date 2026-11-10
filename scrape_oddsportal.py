
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OddsPortal Dropping Odds Parser

Source:
https://www.oddsportal.com/dropping-odds/

Main filters:
1) Old odds 4.00-5.00 -> new odds <= 2.00
2) Old odds 2.00-3.00 -> new odds <= 1.80

Priority:
NORMAL = 1 qualifying market
HIGH   = 2 qualifying market groups
TOP    = 3 groups:
         1X2 + negative handicap + total

SQLite:
- Creates database if missing.
- Migrates old odds_history schema.
- Does not delete existing history.
- Avoids duplicate records.

IMPORTANT:
Odds movement is not proof of actual money flow.
"""

import os
import re
import csv
import json
import time
import hashlib
import sqlite3
import logging

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import sync_playwright


# ============================================================
# CONFIG
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

DATA_DIR.mkdir(parents=True, exist_ok=True)

SOURCE_URL = os.getenv(
    "ODDSPORTAL_URL",
    "https://www.oddsportal.com/dropping-odds/"
)

LATEST_CSV = DATA_DIR / "oddsportal_latest.csv"
HISTORY_CSV = DATA_DIR / "oddsportal_history.csv"
SIGNALS_CSV = DATA_DIR / "oddsportal_signals.csv"

DATABASE_FILE = DATA_DIR / "oddsportal_history.sqlite"
DEBUG_JSON = DATA_DIR / "oddsportal_debug.json"
DEBUG_HTML = DATA_DIR / "oddsportal_cards_debug.html"

HEADLESS = True
TIMEOUT_MS = 60000

# Accept small rounding difference between displayed percentage
# and percentage calculated from odds.
DROP_TOLERANCE_PP = 2.0

# Ancestor depth when locating a card around a match link.
MAX_ANCESTOR_DEPTH = 12

CSV_FIELDS = [
    "scraped_at",
    "sport",
    "country",
    "league",
    "teams",
    "match_time",
    "market",
    "market_group",
    "selection",
    "handicap_line",
    "drop_pct",
    "odds_old",
    "odds_new",
    "best_current_odds",
    "qualifies",
    "match_url",
    "match_key",
]

SIGNAL_FIELDS = [
    "scraped_at",
    "teams",
    "country",
    "league",
    "match_time",
    "market_count",
    "market_groups",
    "priority",
    "markets",
    "match_url",
    "match_key",
]


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

log = logging.getLogger("oddsportal")


# ============================================================
# GENERAL HELPERS
# ============================================================

def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_text(value):
    if value is None:
        return ""

    value = str(value)
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def normalize_team_name(value):
    value = clean_text(value).lower()
    value = re.sub(r"[^a-zа-яіїєґ0-9]+", " ", value)

    return value.strip()


def normalize_match_url(url):
    if not url:
        return ""

    parts = urlsplit(url)

    return urlunsplit((
        parts.scheme,
        parts.netloc,
        parts.path.rstrip("/"),
        "",
        ""
    ))


def make_match_key(teams, match_url):
    normalized_url = normalize_match_url(match_url)

    if normalized_url:
        return normalized_url.lower()

    return normalize_team_name(teams)


def make_record_key(row):
    raw = "|".join([
        str(row.get("match_key", "")),
        str(row.get("market_group", "")),
        str(row.get("market", "")),
        str(row.get("selection", "")),
        str(row.get("handicap_line", "")),
        str(row.get("odds_old", "")),
        str(row.get("odds_new", "")),
        str(row.get("drop_pct", "")),
    ])

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def safe_float(value):
    try:
        if value is None:
            return None

        return float(str(value).replace(",", "."))

    except (TypeError, ValueError):
        return None


def parse_decimal_tokens(text):
    """
    Finds decimal odds-like numbers.
    Requires a decimal separator to avoid treating minutes,
    dates and scores as odds.
    """

    if not text:
        return []

    pattern = r"(?<![\d])\d{1,4}[.,]\d{1,3}(?![\d])"

    result = []

    for match in re.finditer(pattern, text):
        number = safe_float(match.group(0))

        if number is None:
            continue

        if number < 1.01 or number > 1000:
            continue

        result.append(number)

    return result


def parse_drop_percentage(text):
    if not text:
        return None

    matches = re.findall(
        r"(-\s*\d{1,3}(?:[.,]\d+)?)\s*%",
        text
    )

    if not matches:
        return None

    value = matches[0]
    value = value.replace(" ", "").replace(",", ".")

    try:
        return float(value)
    except ValueError:
        return None


# ============================================================
# ODDS FILTER
# ============================================================

def qualifies_odds(old_odds, new_odds):
    if old_odds is None or new_odds is None:
        return False

    # Criterion 1:
    # 4.00-5.00 -> 2.00 or lower
    if 4.00 <= old_odds <= 5.00 and new_odds <= 2.00:
        return True

    # Criterion 2:
    # 2.00-3.00 -> 1.80 or lower
    if 2.00 <= old_odds <= 3.00 and new_odds <= 1.80:
        return True

    return False


def validate_drop(old_odds, new_odds, displayed_drop):
    if old_odds is None or new_odds is None:
        return False, None

    if old_odds <= 0:
        return False, None

    calculated = (new_odds - old_odds) / old_odds * 100

    if displayed_drop is None:
        return True, calculated

    difference = abs(calculated - displayed_drop)

    return difference <= DROP_TOLERANCE_PP, calculated


# ============================================================
# MARKET PARSING
# ============================================================

def parse_market_from_url(url):
    """
    Examples:
    :1X2;2
    :over-under;2;6.50;0
    :eh;2;-1.00;0
    :ah;2;0.75;0
    """

    result = {
        "market": "",
        "market_group": "other",
        "selection": "",
        "handicap_line": "",
    }

    if not url or "#" not in url:
        return result

    fragment = url.split("#", 1)[1]

    if ":" not in fragment:
        return result

    market_data = fragment.split(":", 1)[1]
    tokens = market_data.split(";")

    if not tokens:
        return result

    market_code = tokens[0].strip().lower()

    selection = tokens[1].strip() if len(tokens) > 1 else ""

    if market_code == "1x2":
        result["market"] = "1X2"
        result["market_group"] = "1X2"
        result["selection"] = selection

    elif market_code in ("over-under", "ou", "total"):
        result["market"] = "Total"
        result["market_group"] = "total"
        result["selection"] = selection

        if len(tokens) > 2:
            result["handicap_line"] = tokens[2]

    elif market_code in ("ah", "eh"):
        result["market"] = (
            "Asian Handicap" if market_code == "ah"
            else "European Handicap"
        )

        result["selection"] = selection

        line = ""

        for token in tokens[2:]:
            number = safe_float(token)

            if number is not None:
                line = token
                break

        result["handicap_line"] = line

        line_number = safe_float(line)

        if line_number is not None and line_number < 0:
            result["market_group"] = "negative_handicap"
        else:
            result["market_group"] = "positive_handicap"

    elif market_code == "dnb":
        result["market"] = "Draw No Bet"
        result["selection"] = selection

    elif market_code == "ht-ft":
        result["market"] = "Half Time / Full Time"
        result["selection"] = selection

    elif market_code == "qualify":
        result["market"] = "To Qualify"
        result["selection"] = selection

    else:
        result["market"] = market_code.upper()
        result["selection"] = selection

    return result


def market_label_from_text(text, fallback):
    """
    Uses visible market labels if available.
    Does not override the market group extracted from the URL.
    """

    if not text:
        return fallback

    patterns = [
        r"O/U\s*\d+(?:[.,]\d+)?",
        r"Over\s*/\s*Under",
        r"HT\s*/\s*FT",
        r"1X2",
        r"AH\s*[+-]?\d+(?:[.,]\d+)?",
        r"EH\s*[+-]?\d+(?:[.,]\d+)?",
        r"DNB",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)

        if match:
            return clean_text(match.group(0))

    return fallback


# ============================================================
# MATCH INFORMATION
# ============================================================

def parse_match_time_and_teams(anchor_text):
    """
    Attempts to extract match time and two team names
    from the visible match link.
    """

    lines = [
        clean_text(line)
        for line in str(anchor_text).splitlines()
    ]

    lines = [line for line in lines if line]

    full_text = clean_text(anchor_text)

    time_match = re.search(
        r"\b(?:Today|Tomorrow|Yesterday)?\s*"
        r"\d{1,2}:\d{2}\b",
        full_text,
        re.IGNORECASE
    )

    match_time = time_match.group(0).strip() if time_match else ""

    # Remove date/time fragments.
    cleaned = []

    for line in lines:
        line = re.sub(
            r"\b(?:Today|Tomorrow|Yesterday)\b",
            "",
            line,
            flags=re.IGNORECASE
        )

        line = re.sub(
            r"\b\d{1,2}:\d{2}\b",
            "",
            line
        )

        line = clean_text(line)

        if not line:
            continue

        if line.lower() in ("football", "h2h"):
            continue

        if re.fullmatch(r"[-–—]+", line):
            continue

        cleaned.append(line)

    # Prefer a line containing a separator between teams.
    for line in cleaned:
        parts = re.split(r"\s+(?:-|–|—|vs\.?)\s+", line, maxsplit=1, flags=re.I)

        if len(parts) == 2:
            home = clean_text(parts[0])
            away = clean_text(parts[1])

            if home and away:
                return f"{home} - {away}", match_time

    # Otherwise, use the last two meaningful lines.
    if len(cleaned) >= 2:
        return f"{cleaned[-2]} - {cleaned[-1]}", match_time

    if len(cleaned) == 1:
        return cleaned[0], match_time

    return full_text, match_time


def extract_league_links(links):
    """
    Finds country and league links.
    Excludes generic football and h2h pages.
    """

    candidates = []

    for item in links:
        href = item.get("href", "")
        text = clean_text(item.get("text", ""))

        if not href or not text:
            continue

        path = urlsplit(href).path.lower().strip("/")

        if not path.startswith("football/"):
            continue

        parts = path.split("/")

        if len(parts) < 3:
            continue

        if "h2h" in parts:
            continue

        if parts[-1] in ("football", "h2h"):
            continue

        candidates.append({
            "path": path,
            "text": text,
        })

    # Remove duplicate paths.
    unique = {}

    for item in candidates:
        unique[item["path"]] = item

    candidates = list(unique.values())

    if not candidates:
        return "", ""

    # Last meaningful breadcrumb is normally the competition.
    league = candidates[-1]["text"]

    country = ""

    if len(candidates) >= 2:
        country = candidates[-2]["text"]

    return country, league


# ============================================================
# BROWSER CARD EXTRACTION
# ============================================================

EXTRACT_CARDS_JS = r"""
() => {
    const anchors = Array.from(
        document.querySelectorAll('a[href*="/football/h2h/"]')
    );

    const output = [];
    const seen = new Set();

    for (const anchor of anchors) {
        const href = anchor.href || anchor.getAttribute("href") || "";

        if (!href || seen.has(href)) continue;
        seen.add(href);

        let node = anchor;
        let best = null;

        for (let depth = 0; depth < 13 && node; depth++) {
            const text = (node.innerText || "").trim();

            const hasBestOdds = /Best Current Odds/i.test(text);
            const hasDrop = /-\s*\d+(?:[.,]\d+)?\s*%/.test(text);

            const oddsMatches = text.match(
                /(?<![\d])\d{1,4}[.,]\d{1,3}(?![\d])/g
            ) || [];

            const links = Array.from(
                node.querySelectorAll("a")
            ).map(a => ({
                href: a.href || a.getAttribute("href") || "",
                text: (a.innerText || "").trim()
            }));

            const candidate = {
                href,
                anchorText: anchor.innerText || "",
                text,
                html: node.outerHTML || "",
                links,
                depth,
                oddsCount: oddsMatches.length,
                hasBestOdds,
                hasDrop
            };

            if (hasBestOdds && hasDrop && oddsMatches.length >= 3) {
                best = candidate;
                break;
            }

            node = node.parentElement;
        }

        if (best) {
            output.push(best);
        } else {
            output.push({
                href,
                anchorText: anchor.innerText || "",
                text: "",
                html: "",
                links: [],
                depth: -1,
                oddsCount: 0,
                hasBestOdds: false,
                hasDrop: false
            });
        }
    }

    return output;
}
"""


def extract_odds_from_card_text(text, displayed_drop):
    """
    Expected visible order:
    -62%
    59.00
    22.50
    26.00

    Returns old, new, best current.
    """

    if not text:
        return None, None, None

    marker = re.search(r"Best Current Odds", text, re.IGNORECASE)

    if marker:
        # Odds are usually before the Best Current Odds label
        # or immediately around it. Search the entire card,
        # then validate the triplet against displayed drop.
        pass

    numbers = parse_decimal_tokens(text)

    if len(numbers) < 3:
        return None, None, None

    # Test consecutive triplets and find the one whose first
    # two numbers reproduce the displayed percentage.
    candidates = []

    for index in range(len(numbers) - 2):
        old_odds = numbers[index]
        new_odds = numbers[index + 1]
        best_odds = numbers[index + 2]

        valid, calculated = validate_drop(
            old_odds,
            new_odds,
            displayed_drop
        )

        if valid:
            candidates.append((
                index,
                old_odds,
                new_odds,
                best_odds,
                calculated
            ))

    if candidates:
        # Prefer the earliest validated triplet.
        chosen = candidates[0]

        return chosen[1], chosen[2], chosen[3]

    # No validated triplet: do not silently guess.
    return None, None, None


def parse_card(card, scraped_at):
    href = card.get("href", "")
    text = card.get("text", "")
    anchor_text = card.get("anchorText", "")

    if not href or not text:
        return None, "card_container_not_found"

    displayed_drop = parse_drop_percentage(text)

    if displayed_drop is None:
        return None, "drop_percentage_not_found"

    old_odds, new_odds, best_odds = extract_odds_from_card_text(
        text,
        displayed_drop
    )

    if old_odds is None:
        return None, "odds_triplet_not_validated"

    valid_drop, calculated_drop = validate_drop(
        old_odds,
        new_odds,
        displayed_drop
    )

    if not valid_drop:
        return None, "drop_percentage_mismatch"

    market_data = parse_market_from_url(href)

    links = card.get("links", [])

    country, league = extract_league_links(links)

    teams, match_time = parse_match_time_and_teams(anchor_text)

    market = market_label_from_text(
        text,
        market_data["market"]
    )

    # Try to identify visible selection.
    selection = market_data["selection"]

    if selection:
        selection_map = {
            "1": "1",
            "2": "2",
            "x": "X",
            "0": "0",
        }

        selection = selection_map.get(
            selection.lower(),
            selection
        )

    # Total market selection may be visible as Over/Under.
    if market_data["market_group"] == "total":
        before_best = re.split(
            r"Best Current Odds",
            text,
            flags=re.IGNORECASE
        )[0]

        visible = re.findall(
            r"\b(Over|Under)\b",
            before_best,
            flags=re.IGNORECASE
        )

        if visible:
            selection = visible[-1].title()

    match_key = make_match_key(teams, href)

    qualifies = qualifies_odds(old_odds, new_odds)

    row = {
        "scraped_at": scraped_at,
        "sport": "Football",
        "country": country,
        "league": league,
        "teams": teams,
        "match_time": match_time,
        "market": market,
        "market_group": market_data["market_group"],
        "selection": selection,
        "handicap_line": market_data["handicap_line"],
        "drop_pct": displayed_drop,
        "odds_old": old_odds,
        "odds_new": new_odds,
        "best_current_odds": best_odds,
        "qualifies": qualifies,
        "match_url": href,
        "match_key": match_key,
    }

    return row, None


# ============================================================
# CSV
# ============================================================

def write_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=fields,
            extrasaction="ignore"
        )

        writer.writeheader()
        writer.writerows(rows)


def append_csv(path, rows, fields):
    if not rows:
        return

    exists = path.exists() and path.stat().st_size > 0

    with open(path, "a", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=fields,
            extrasaction="ignore"
        )

        if not exists:
            writer.writeheader()

        writer.writerows(rows)


# ============================================================
# SQLITE MIGRATION AND HISTORY
# ============================================================

DB_COLUMNS = {
    "record_key": "TEXT",
    "scraped_at": "TEXT",
    "sport": "TEXT",
    "country": "TEXT",
    "league": "TEXT",
    "teams": "TEXT",
    "match_time": "TEXT",
    "market": "TEXT",
    "market_group": "TEXT",
    "selection": "TEXT",
    "handicap_line": "TEXT",
    "drop_pct": "REAL",
    "odds_old": "REAL",
    "odds_new": "REAL",
    "best_current_odds": "REAL",
    "qualifies": "INTEGER",
    "match_url": "TEXT",
    "match_key": "TEXT",
}


def initialize_database(conn):
    """
    Creates table if missing and migrates old schemas.
    Existing rows are preserved.
    """

    conn.execute("""
        CREATE TABLE IF NOT EXISTS odds_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            record_key TEXT,
            scraped_at TEXT,
            sport TEXT,
            country TEXT,
            league TEXT,
            teams TEXT,
            match_time TEXT,
            market TEXT,
            market_group TEXT,
            selection TEXT,
            handicap_line TEXT,
            drop_pct REAL,
            odds_old REAL,
            odds_new REAL,
            best_current_odds REAL,
            qualifies INTEGER,
            match_url TEXT,
            match_key TEXT
        )
    """)

    existing_columns = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(odds_history)"
        ).fetchall()
    }

    for column, column_type in DB_COLUMNS.items():
        if column not in existing_columns:
            conn.execute(
                f"ALTER TABLE odds_history "
                f"ADD COLUMN {column} {column_type}"
            )

    conn.commit()

    # Backfill keys for old records where possible.
    rows = conn.execute("""
        SELECT *
        FROM odds_history
        WHERE record_key IS NULL OR record_key = ''
    """).fetchall()

    column_names = [
        item[1]
        for item in conn.execute(
            "PRAGMA table_info(odds_history)"
        ).fetchall()
    ]

    for old_row in rows:
        data = dict(zip(column_names, old_row))

        match_key = data.get("match_key") or make_match_key(
            data.get("teams", ""),
            data.get("match_url", "")
        )

        temporary = {
            "match_key": match_key,
            "market_group": data.get("market_group", ""),
            "market": data.get("market", ""),
            "selection": data.get("selection", ""),
            "handicap_line": data.get("handicap_line", ""),
            "odds_old": data.get("odds_old", ""),
            "odds_new": data.get("odds_new", ""),
            "drop_pct": data.get("drop_pct", ""),
        }

        key = make_record_key(temporary)

        conn.execute("""
            UPDATE odds_history
            SET record_key = ?, match_key = ?
            WHERE id = ?
        """, (key, match_key, data.get("id")))

    conn.commit()


def save_history(rows):
    """
    Stores only qualifying rows.
    Avoids duplicate records using record_key.
    """

    conn = sqlite3.connect(DATABASE_FILE)

    try:
        initialize_database(conn)

        new_rows = []

        columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(odds_history)"
            ).fetchall()
        }

        for row in rows:
            if not row.get("qualifies"):
                continue

            record_key = make_record_key(row)

            exists = conn.execute(
                "SELECT 1 FROM odds_history "
                "WHERE record_key = ? LIMIT 1",
                (record_key,)
            ).fetchone()

            if exists:
                continue

            values = {
                "record_key": record_key,
                **row,
            }

            insert_columns = [
                column
                for column in values
                if column in columns
            ]

            placeholders = ",".join(
                ["?"] * len(insert_columns)
            )

            column_sql = ",".join(
                f'"{column}"'
                for column in insert_columns
            )

            parameters = [
                values[column]
                for column in insert_columns
            ]

            # Preserve compatibility with older schemas that
            # may contain additional NOT NULL columns.
            table_info = conn.execute(
                "PRAGMA table_info(odds_history)"
            ).fetchall()

            for info in table_info:
                name = info[1]
                not_null = info[3]
                default_value = info[4]
                is_primary_key = info[5]

                if (
                    name not in insert_columns
                    and not_null
                    and not is_primary_key
                    and default_value is None
                ):
                    insert_columns.append(name)
                    column_sql = ",".join(
                        f'"{column}"'
                        for column in insert_columns
                    )

                    placeholders = ",".join(
                        ["?"] * len(insert_columns)
                    )

                    parameters.append("")

            sql = (
                f"INSERT INTO odds_history "
                f"({column_sql}) VALUES ({placeholders})"
            )

            conn.execute(sql, parameters)

            new_rows.append(row)

        conn.commit()

        return new_rows

    finally:
        conn.close()


# ============================================================
# SIGNAL GROUPING
# ============================================================

def build_signals(rows, scraped_at):
    grouped = {}

    for row in rows:
        if not row.get("qualifies"):
            continue

        key = row.get("match_key")

        if not key:
            continue

        if key not in grouped:
            grouped[key] = {
                "rows": [],
                "groups": set(),
            }

        grouped[key]["rows"].append(row)

        market_group = row.get("market_group")

        if market_group in (
            "1X2",
            "negative_handicap",
            "total",
        ):
            grouped[key]["groups"].add(market_group)

    signals = []

    for key, item in grouped.items():
        match_rows = item["rows"]
        groups = item["groups"]

        if not groups:
            continue

        count = len(groups)

        if count >= 3:
            priority = "TOP"
        elif count == 2:
            priority = "HIGH"
        else:
            priority = "NORMAL"

        first = match_rows[0]

        markets = []

        for row in match_rows:
            markets.append(
                "{} | {} | {} -> {}".format(
                    row.get("market", ""),
                    row.get("selection", ""),
                    row.get("odds_old", ""),
                    row.get("odds_new", "")
                )
            )

        signals.append({
            "scraped_at": scraped_at,
            "teams": first.get("teams", ""),
            "country": first.get("country", ""),
            "league": first.get("league", ""),
            "match_time": first.get("match_time", ""),
            "market_count": count,
            "market_groups": ", ".join(sorted(groups)),
            "priority": priority,
            "markets": " || ".join(markets),
            "match_url": normalize_match_url(
                first.get("match_url", "")
            ),
            "match_key": key,
        })

    priority_order = {
        "TOP": 0,
        "HIGH": 1,
        "NORMAL": 2,
    }

    signals.sort(
        key=lambda item: (
            priority_order.get(item["priority"], 9),
            item["teams"]
        )
    )

    return signals


# ============================================================
# DEBUG OUTPUT
# ============================================================

def save_debug(data):
    with open(DEBUG_JSON, "w", encoding="utf-8") as file:
        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2
        )


def save_html_samples(cards):
    fragments = []

    for index, card in enumerate(cards[:10]):
        html = card.get("html", "")

        if not html:
            continue

        fragments.append(
            f"\n<!-- CARD {index + 1} -->\n{html}\n"
        )

    with open(DEBUG_HTML, "w", encoding="utf-8") as file:
        file.write("\n".join(fragments))


# ============================================================
# SCRAPER
# ============================================================

def scrape_page():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ]
        )

        context = browser.new_context(
            viewport={
                "width": 1440,
                "height": 1000,
            },
            locale="en-US",
            timezone_id="UTC",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )

        page = context.new_page()
        page.set_default_timeout(TIMEOUT_MS)

        response = page.goto(
            SOURCE_URL,
            wait_until="domcontentloaded",
            timeout=TIMEOUT_MS
        )

        http_status = response.status if response else None

        page.wait_for_timeout(5000)

        # Wait for the page to render its content.
        try:
            page.wait_for_selector(
                'a[href*="/football/h2h/"]',
                timeout=20000
            )
        except Exception:
            log.warning("No match links appeared within timeout.")

        title = page.title()
        final_url = page.url

        cards = page.evaluate(EXTRACT_CARDS_JS)

        context.close()
        browser.close()

        return {
            "http_status": http_status,
            "page_title": title,
            "final_url": final_url,
            "cards": cards,
        }


# ============================================================
# MAIN
# ============================================================

def main():
    started = time.time()
    scraped_at = utc_now()

    errors = []
    parse_failures = []
    rows = []

    http_status = None
    page_title = ""
    final_url = ""

    log.info("Starting OddsPortal parser")
    log.info("URL: %s", SOURCE_URL)

    try:
        result = scrape_page()

        http_status = result["http_status"]
        page_title = result["page_title"]
        final_url = result["final_url"]

        cards = result["cards"]

        log.info("Cards found: %s", len(cards))

        save_html_samples(cards)

        for index, card in enumerate(cards):
            try:
                row, error = parse_card(
                    card,
                    scraped_at
                )

                if error:
                    parse_failures.append({
                        "card": index + 1,
                        "reason": error,
                        "anchor_text": clean_text(
                            card.get("anchorText", "")
                        )[:300],
                        "card_text": clean_text(
                            card.get("text", "")
                        )[:700],
                    })

                    continue

                rows.append(row)

            except Exception as exc:
                parse_failures.append({
                    "card": index + 1,
                    "reason": str(exc),
                    "anchor_text": clean_text(
                        card.get("anchorText", "")
                    )[:300],
                })

        qualifying_rows = [
            row for row in rows
            if row.get("qualifies")
        ]

        signals = build_signals(
            qualifying_rows,
            scraped_at
        )

        # Current snapshot.
        write_csv(
            LATEST_CSV,
            rows,
            CSV_FIELDS
        )

        # Persistent history.
        new_history_rows = save_history(rows)

        # CSV history contains only newly inserted rows.
        append_csv(
            HISTORY_CSV,
            new_history_rows,
            CSV_FIELDS
        )

        # Current grouped signals.
        write_csv(
            SIGNALS_CSV,
            signals,
            SIGNAL_FIELDS
        )

        duration = round(time.time() - started, 2)

        debug = {
            "started_at": scraped_at,
            "url": SOURCE_URL,
            "http_status": http_status,
            "page_title": page_title,
            "final_url": final_url,
            "cards_found": len(cards),
            "parsed_rows": len(rows),
            "qualifying_rows": len(qualifying_rows),
            "signals": len(signals),
            "new_database_records": len(new_history_rows),
            "parse_failures": len(parse_failures),
            "errors": errors,
            "parse_failure_samples": parse_failures[:30],
            "signal_samples": signals[:20],
            "duration_seconds": duration,
            "finished_at": utc_now(),
            "result": "completed",
        }

        save_debug(debug)

        log.info("Parsed rows: %s", len(rows))
        log.info("Qualifying rows: %s", len(qualifying_rows))
        log.info("Grouped signals: %s", len(signals))
        log.info(
            "New database records: %s",
            len(new_history_rows)
        )
        log.info("Parse failures: %s", len(parse_failures))
        log.info("Finished in %s seconds", duration)

    except Exception as exc:
        duration = round(time.time() - started, 2)

        errors.append(str(exc))

        debug = {
            "started_at": scraped_at,
            "url": SOURCE_URL,
            "http_status": http_status,
            "page_title": page_title,
            "final_url": final_url,
            "cards_found": 0,
            "parsed_rows": 0,
            "qualifying_rows": 0,
            "signals": 0,
            "new_database_records": 0,
            "errors": errors,
            "duration_seconds": duration,
            "finished_at": utc_now(),
            "result": "failed",
        }

        save_debug(debug)

        log.exception("Parser failed")

        raise


if __name__ == "__main__":
    main()
