
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ODDSPORTAL DROPPING ODDS PARSER

Filters:
1. Old odds 4.00-5.00 -> new odds <= 2.00
2. Old odds 2.00-3.00 -> new odds <= 1.80

Priority:
NORMAL = 1 qualifying group
HIGH   = 2 qualifying groups
TOP    = 1X2 + negative handicap + total

SQLite:
- Preserves old history.
- Migrates old database columns.
- Prevents duplicate records.

Note:
Odds movement does not prove actual money flow.
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
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import sync_playwright


# ============================================================
# CONFIGURATION
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
DROP_TOLERANCE_PP = 2.0
MAX_ANCESTOR_DEPTH = 12

CSV_FIELDS = [
    "scraped_at",
    "sport",
    "country",
    "country_slug",
    "league",
    "teams",
    "match_time",
    "market",
    "market_code",
    "market_group",
    "selection",
    "selection_code",
    "handicap_line",
    "drop_pct",
    "calculated_drop_pct",
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

    value = str(value).replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def safe_float(value):
    try:
        if value is None:
            return None

        return float(str(value).replace(",", "."))

    except (TypeError, ValueError):
        return None


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
    """
    All market links for the same event share the same
    H2H URL path. The fragment is deliberately removed.
    """

    base_url = normalize_match_url(match_url)

    if base_url:
        return base_url.lower()

    return normalize_team_name(teams)


def make_record_key(row):
    values = [
        row.get("match_key", ""),
        row.get("market_group", ""),
        row.get("market_code", ""),
        row.get("selection_code", ""),
        row.get("handicap_line", ""),
        row.get("odds_old", ""),
        row.get("odds_new", ""),
        row.get("drop_pct", ""),
    ]

    raw = "|".join(str(value) for value in values)

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()


def parse_decimal_tokens(text):
    """
    Only decimal numbers are considered.
    This avoids interpreting ordinary integers,
    scores and dates as odds.
    """

    pattern = r"(?<![\d])\d{1,4}[.,]\d{1,3}(?![\d])"

    result = []

    for match in re.finditer(pattern, text or ""):
        number = safe_float(match.group(0))

        if number is None:
            continue

        if 1.01 <= number <= 1000:
            result.append(number)

    return result


def parse_drop_percentage(text):
    matches = re.findall(
        r"(-\s*\d{1,3}(?:[.,]\d+)?)\s*%",
        text or ""
    )

    if not matches:
        return None

    value = matches[0].replace(" ", "").replace(",", ".")

    return safe_float(value)


# ============================================================
# ODDS FILTER
# ============================================================

def qualifies_odds(old_odds, new_odds):

    if old_odds is None or new_odds is None:
        return False

    # Rule 1:
    # Initial odds 4.00-5.00 -> 2.00 or lower.

    if 4.00 <= old_odds <= 5.00 and new_odds <= 2.00:
        return True

    # Rule 2:
    # Initial odds 2.00-3.00 -> 1.80 or lower.

    if 2.00 <= old_odds <= 3.00 and new_odds <= 1.80:
        return True

    return False


def validate_drop(old_odds, new_odds, displayed_drop):

    if old_odds is None or new_odds is None:
        return False, None

    if old_odds <= 0:
        return False, None

    calculated = (
        (new_odds - old_odds) / old_odds
    ) * 100

    if displayed_drop is None:
        return True, calculated

    difference = abs(calculated - displayed_drop)

    return difference <= DROP_TOLERANCE_PP, calculated


# ============================================================
# MARKET PARSING
# ============================================================

def parse_market_from_url(url):

    result = {
        "market": "",
        "market_code": "",
        "market_group": "other",
        "selection_code": "",
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

    code = tokens[0].strip().lower()
    selection_code = (
        tokens[1].strip()
        if len(tokens) > 1
        else ""
    )

    result["market_code"] = code
    result["selection_code"] = selection_code
    result["selection"] = selection_code

    if code == "1x2":

        result["market"] = "1X2"
        result["market_group"] = "1X2"

    elif code in ("over-under", "ou", "total"):

        result["market"] = "Total"
        result["market_group"] = "total"

        if len(tokens) > 2:
            result["handicap_line"] = tokens[2]

    elif code in ("ah", "eh"):

        result["market"] = (
            "Asian Handicap"
            if code == "ah"
            else "European Handicap"
        )

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

    elif code == "dnb":

        result["market"] = "DNB"

    elif code == "ht-ft":

        result["market"] = "HT/FT"

    elif code == "qualify":

        result["market"] = "To Qualify"

    else:

        result["market"] = code.upper()

    return result


def detect_market_label(text, fallback):

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
        match = re.search(
            pattern,
            text or "",
            re.IGNORECASE
        )

        if match:
            return clean_text(match.group(0))

    return fallback


def detect_visible_selection(text, market_data):

    """
    Attempts to read the actual visible selection from the card.

    The numeric selection code from the URL is kept separately.
    No undocumented mapping of 1X2 codes is assumed.
    """

    before_best = re.split(
        r"Best Current Odds",
        text or "",
        flags=re.IGNORECASE
    )[0]

    lines = [
        clean_text(line)
        for line in before_best.splitlines()
    ]

    lines = [line for line in lines if line]

    # Totals.
    if market_data["market_group"] == "total":

        for line in reversed(lines):
            if re.fullmatch(
                r"Over|Under",
                line,
                re.IGNORECASE
            ):
                return line.title()

    # 1X2 visible result labels.
    if market_data["market_group"] == "1X2":

        allowed = {
            "1", "X", "2",
            "Home", "Draw", "Away",
            "1X", "X2", "12",
        }

        for line in reversed(lines):
            if line in allowed:
                return line

    # Handicap visible selection.
    if market_data["market_group"] in (
        "negative_handicap",
        "positive_handicap"
    ):

        for line in reversed(lines):
            if line in ("1", "2", "Home", "Away"):
                return line

    # Do not invent a label.
    code = market_data.get("selection_code", "")

    return f"code:{code}" if code else ""


# ============================================================
# COUNTRY AND LEAGUE
# ============================================================

COUNTRY_NAMES = {
    "england": "England",
    "scotland": "Scotland",
    "wales": "Wales",
    "northern-ireland": "Northern Ireland",
    "colombia": "Colombia",
    "spain": "Spain",
    "france": "France",
    "germany": "Germany",
    "italy": "Italy",
    "portugal": "Portugal",
    "brazil": "Brazil",
    "argentina": "Argentina",
    "netherlands": "Netherlands",
    "belgium": "Belgium",
    "turkey": "Turkey",
    "greece": "Greece",
    "ukraine": "Ukraine",
    "poland": "Poland",
    "czech-republic": "Czech Republic",
    "croatia": "Croatia",
    "serbia": "Serbia",
    "romania": "Romania",
    "bulgaria": "Bulgaria",
    "denmark": "Denmark",
    "sweden": "Sweden",
    "norway": "Norway",
    "finland": "Finland",
    "usa": "USA",
    "united-states": "USA",
    "mexico": "Mexico",
    "japan": "Japan",
    "south-korea": "South Korea",
    "china": "China",
    "australia": "Australia",
    "india": "India",
    "international": "International",
    "world": "International",
}


def slug_to_title(slug):

    if not slug:
        return ""

    if slug in COUNTRY_NAMES:
        return COUNTRY_NAMES[slug]

    return " ".join(
        word.capitalize()
        for word in slug.replace("_", "-").split("-")
    )


def extract_country_league(links):

    candidates = []

    for item in links or []:

        href = item.get("href", "")
        label = clean_text(item.get("text", ""))

        if not href:
            continue

        path = urlsplit(href).path.lower().strip("/")
        parts = path.split("/")

        # Expected competition URL:
        # /football/country/competition/

        if len(parts) != 3:
            continue

        if parts[0] != "football":
            continue

        country_slug = parts[1]
        league_slug = parts[2]

        if country_slug in ("h2h", "football"):
            continue

        if league_slug in ("h2h", "football"):
            continue

        candidates.append({
            "country_slug": country_slug,
            "league_slug": league_slug,
            "league": label or slug_to_title(league_slug),
            "path": path,
        })

    if not candidates:
        return "", "", ""

    # Prefer the most specific competition link.
    candidates.sort(
        key=lambda item: len(item["path"]),
        reverse=True
    )

    chosen = candidates[0]

    country = slug_to_title(chosen["country_slug"])

    return (
        country,
        chosen["country_slug"],
        chosen["league"]
    )


# ============================================================
# MATCH TEAMS AND TIME
# ============================================================

def parse_match_time_and_teams(anchor_text):

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

    match_time = (
        clean_text(time_match.group(0))
        if time_match
        else ""
    )

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

        if re.fullmatch(r"[-–—]+", line):
            continue

        if line.lower() in ("football", "h2h"):
            continue

        cleaned.append(line)

    # First try a combined team line.
    for line in cleaned:

        parts = re.split(
            r"\s+(?:-|–|—|vs\.?)\s+",
            line,
            maxsplit=1,
            flags=re.IGNORECASE
        )

        if len(parts) == 2:

            home = clean_text(parts[0])
            away = clean_text(parts[1])

            if home and away:
                return f"{home} - {away}", match_time

    # Otherwise use the last two visible lines.
    if len(cleaned) >= 2:

        return (
            f"{cleaned[-2]} - {cleaned[-1]}",
            match_time
        )

    if len(cleaned) == 1:
        return cleaned[0], match_time

    return full_text, match_time


# ============================================================
# BROWSER EXTRACTION
# ============================================================

EXTRACT_CARDS_JS = r"""
() => {

    const anchors = Array.from(
        document.querySelectorAll('a[href*="/football/h2h/"]')
    );

    const output = [];
    const seen = new Set();

    for (const anchor of anchors) {

        const href =
            anchor.href ||
            anchor.getAttribute("href") ||
            "";

        if (!href || seen.has(href)) continue;

        seen.add(href);

        let node = anchor;
        let selected = null;

        for (
            let depth = 0;
            depth < 13 && node;
            depth++
        ) {

            const text = node.innerText || "";

            const hasBestOdds =
                /Best Current Odds/i.test(text);

            const hasDrop =
                /-\s*\d+(?:[.,]\d+)?\s*%/.test(text);

            const oddsMatches = text.match(
                /(?<![\d])\d{1,4}[.,]\d{1,3}(?![\d])/g
            ) || [];

            const links = Array.from(
                node.querySelectorAll("a")
            ).map(a => ({
                href: a.href || a.getAttribute("href") || "",
                text: a.innerText || ""
            }));

            if (
                hasBestOdds &&
                hasDrop &&
                oddsMatches.length >= 3
            ) {

                selected = {
                    href: href,
                    anchorText: anchor.innerText || "",
                    text: text,
                    html: node.outerHTML || "",
                    links: links,
                    depth: depth,
                    oddsCount: oddsMatches.length
                };

                // The first matching ancestor is the smallest.
                break;
            }

            node = node.parentElement;
        }

        if (selected) {
            output.push(selected);
        } else {
            output.push({
                href: href,
                anchorText: anchor.innerText || "",
                text: "",
                html: "",
                links: [],
                depth: -1,
                oddsCount: 0
            });
        }
    }

    const allH2H = Array.from(
        document.querySelectorAll('a[href*="/football/h2h/"]')
    ).map(a => a.href || a.getAttribute("href") || "");

    const paginationLinks = Array.from(
        document.querySelectorAll("a[href]")
    )
    .filter(a => {
        const text = (a.innerText || "").trim();
        const href = a.href || "";

        return (
            /next|more|load more/i.test(text) ||
            /page=\d+/i.test(href)
        );
    })
    .map(a => ({
        text: (a.innerText || "").trim(),
        href: a.href || ""
    }));

    return {
        cards: output,
        match_links_found: allH2H.length,
        unique_match_links: [...new Set(allH2H)].length,
        pagination_links: paginationLinks
    };
}
"""


# ============================================================
# ODDS EXTRACTION
# ============================================================

def extract_odds_from_card(text, displayed_drop):

    numbers = parse_decimal_tokens(text)

    if len(numbers) < 3:
        return None, None, None, None

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

    if not candidates:
        return None, None, None, None

    chosen = candidates[0]

    return (
        chosen[1],
        chosen[2],
        chosen[3],
        chosen[4]
    )


# ============================================================
# CARD PARSER
# ============================================================

def parse_card(card, scraped_at):

    href = card.get("href", "")
    text = card.get("text", "")
    anchor_text = card.get("anchorText", "")

    if not href or not text:
        return None, "card_container_not_found"

    displayed_drop = parse_drop_percentage(text)

    if displayed_drop is None:
        return None, "drop_percentage_not_found"

    (
        old_odds,
        new_odds,
        best_odds,
        calculated_drop
    ) = extract_odds_from_card(
        text,
        displayed_drop
    )

    if old_odds is None:
        return None, "odds_triplet_not_validated"

    valid, _ = validate_drop(
        old_odds,
        new_odds,
        displayed_drop
    )

    if not valid:
        return None, "drop_percentage_mismatch"

    market_data = parse_market_from_url(href)

    market_data["market"] = detect_market_label(
        text,
        market_data["market"]
    )

    market_data["selection"] = detect_visible_selection(
        text,
        market_data
    )

    country, country_slug, league = extract_country_league(
        card.get("links", [])
    )

    teams, match_time = parse_match_time_and_teams(
        anchor_text
    )

    match_key = make_match_key(
        teams,
        href
    )

    qualifies = qualifies_odds(
        old_odds,
        new_odds
    )

    row = {
        "scraped_at": scraped_at,
        "sport": "Football",
        "country": country,
        "country_slug": country_slug,
        "league": league,
        "teams": teams,
        "match_time": match_time,
        "market": market_data["market"],
        "market_code": market_data["market_code"],
        "market_group": market_data["market_group"],
        "selection": market_data["selection"],
        "selection_code": market_data["selection_code"],
        "handicap_line": market_data["handicap_line"],
        "drop_pct": displayed_drop,
        "calculated_drop_pct": round(
            calculated_drop,
            2
        ) if calculated_drop is not None else "",
        "odds_old": old_odds,
        "odds_new": new_odds,
        "best_current_odds": best_odds,
        "qualifies": qualifies,
        "match_url": href,
        "match_key": match_key,
    }

    return row, None


# ============================================================
# CSV OUTPUT
# ============================================================

def write_csv(path, rows, fields):

    with open(
        path,
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as file:

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

    exists = (
        path.exists()
        and path.stat().st_size > 0
    )

    with open(
        path,
        "a",
        newline="",
        encoding="utf-8-sig"
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=fields,
            extrasaction="ignore"
        )

        if not exists:
            writer.writeheader()

        writer.writerows(rows)


# ============================================================
# SQLITE DATABASE AND MIGRATION
# ============================================================

DB_COLUMNS = {
    "record_key": "TEXT",
    "scraped_at": "TEXT",
    "sport": "TEXT",
    "country": "TEXT",
    "country_slug": "TEXT",
    "league": "TEXT",
    "teams": "TEXT",
    "match_time": "TEXT",
    "market": "TEXT",
    "market_code": "TEXT",
    "market_group": "TEXT",
    "selection": "TEXT",
    "selection_code": "TEXT",
    "handicap_line": "TEXT",
    "drop_pct": "REAL",
    "calculated_drop_pct": "REAL",
    "odds_old": "REAL",
    "odds_new": "REAL",
    "best_current_odds": "REAL",
    "qualifies": "INTEGER",
    "match_url": "TEXT",
    "match_key": "TEXT",
}


def initialize_database(conn):

    conn.execute("""
        CREATE TABLE IF NOT EXISTS odds_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            record_key TEXT,
            scraped_at TEXT,
            sport TEXT,
            country TEXT,
            country_slug TEXT,
            league TEXT,
            teams TEXT,
            match_time TEXT,
            market TEXT,
            market_code TEXT,
            market_group TEXT,
            selection TEXT,
            selection_code TEXT,
            handicap_line TEXT,
            drop_pct REAL,
            calculated_drop_pct REAL,
            odds_old REAL,
            odds_new REAL,
            best_current_odds REAL,
            qualifies INTEGER,
            match_url TEXT,
            match_key TEXT
        )
    """)

    existing = {
        item[1]
        for item in conn.execute(
            "PRAGMA table_info(odds_history)"
        ).fetchall()
    }

    # Add missing columns without deleting old records.
    for column, column_type in DB_COLUMNS.items():

        if column not in existing:

            conn.execute(
                f'ALTER TABLE odds_history '
                f'ADD COLUMN "{column}" {column_type}'
            )

    conn.commit()

    # Backfill record_key for old rows.
    table_info = conn.execute(
        "PRAGMA table_info(odds_history)"
    ).fetchall()

    column_names = [
        item[1]
        for item in table_info
    ]

    old_rows = conn.execute("""
        SELECT *
        FROM odds_history
        WHERE record_key IS NULL
           OR record_key = ''
    """).fetchall()

    for old_row in old_rows:

        data = dict(
            zip(column_names, old_row)
        )

        match_key = data.get("match_key") or make_match_key(
            data.get("teams", ""),
            data.get("match_url", "")
        )

        temporary = {
            "match_key": match_key,
            "market_group": data.get("market_group", ""),
            "market_code": data.get("market_code", ""),
            "selection_code": data.get("selection_code", ""),
            "handicap_line": data.get("handicap_line", ""),
            "odds_old": data.get("odds_old", ""),
            "odds_new": data.get("odds_new", ""),
            "drop_pct": data.get("drop_pct", ""),
        }

        key = make_record_key(temporary)

        conn.execute("""
            UPDATE odds_history
            SET record_key = ?,
                match_key = ?
            WHERE id = ?
        """, (
            key,
            match_key,
            data.get("id")
        ))

    conn.commit()

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_odds_history_record_key
        ON odds_history(record_key)
    """)

    conn.commit()


def save_history(rows):

    conn = sqlite3.connect(DATABASE_FILE)

    try:

        initialize_database(conn)

        new_rows = []

        table_info = conn.execute(
            "PRAGMA table_info(odds_history)"
        ).fetchall()

        columns = {
            item[1]
            for item in table_info
        }

        for row in rows:

            if not row.get("qualifies"):
                continue

            record_key = make_record_key(row)

            exists = conn.execute(
                """
                SELECT 1
                FROM odds_history
                WHERE record_key = ?
                LIMIT 1
                """,
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

            # Preserve compatibility with older tables
            # containing additional required columns.
            for item in table_info:

                name = item[1]
                not_null = item[3]
                default_value = item[4]
                primary_key = item[5]

                if (
                    name not in insert_columns
                    and not_null
                    and not primary_key
                    and default_value is None
                ):

                    insert_columns.append(name)
                    values[name] = ""

            column_sql = ", ".join(
                f'"{column}"'
                for column in insert_columns
            )

            placeholders = ", ".join(
                "?"
                for _ in insert_columns
            )

            parameters = [
                values.get(column, "")
                for column in insert_columns
            ]

            sql = (
                f"INSERT INTO odds_history "
                f"({column_sql}) "
                f"VALUES ({placeholders})"
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
            "total"
        ):

            grouped[key]["groups"].add(
                market_group
            )

    signals = []

    for key, item in grouped.items():

        match_rows = item["rows"]
        groups = item["groups"]

        if not groups:
            continue

        count = len(groups)

        if count == 3:
            priority = "TOP"

        elif count == 2:
            priority = "HIGH"

        else:
            priority = "NORMAL"

        first = match_rows[0]

        markets = []

        for row in match_rows:

            markets.append(
                "{} [{}] {} -> {}".format(
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
            "market_groups": ", ".join(
                sorted(groups)
            ),
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
            priority_order.get(
                item["priority"],
                9
            ),
            item["teams"]
        )
    )

    return signals


# ============================================================
# DEBUG FILES
# ============================================================

def save_debug(data):

    with open(
        DEBUG_JSON,
        "w",
        encoding="utf-8"
    ) as file:

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

    with open(
        DEBUG_HTML,
        "w",
        encoding="utf-8"
    ) as file:

        file.write(
            "\n".join(fragments)
        )


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

        http_status = (
            response.status
            if response
            else None
        )

        page.wait_for_timeout(5000)

        try:

            page.wait_for_selector(
                'a[href*="/football/h2h/"]',
                timeout=20000
            )

        except Exception:

            log.warning(
                "Match links did not appear before timeout."
            )

        title = page.title()
        final_url = page.url

        extracted = page.evaluate(
            EXTRACT_CARDS_JS
        )

        context.close()
        browser.close()

        return {
            "http_status": http_status,
            "page_title": title,
            "final_url": final_url,
            "cards": extracted.get("cards", []),
            "match_links_found": extracted.get(
                "match_links_found", 0
            ),
            "unique_match_links": extracted.get(
                "unique_match_links", 0
            ),
            "pagination_links": extracted.get(
                "pagination_links", []
            ),
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

    match_links_found = 0
    unique_match_links = 0
    pagination_links = []

    log.info("Starting OddsPortal parser")
    log.info("Source: %s", SOURCE_URL)

    try:

        result = scrape_page()

        http_status = result["http_status"]
        page_title = result["page_title"]
        final_url = result["final_url"]

        cards = result["cards"]

        match_links_found = result["match_links_found"]
        unique_match_links = result["unique_match_links"]
        pagination_links = result["pagination_links"]

        log.info(
            "Unique match links: %s",
            unique_match_links
        )

        log.info(
            "Cards with containers: %s",
            len(cards)
        )

        save_html_samples(cards)

        seen_match_market = set()

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

                # Detect duplicate market cards.
                duplicate_key = (
                    row["match_key"],
                    row["market_code"],
                    row["selection_code"],
                    row["handicap_line"],
                )

                if duplicate_key in seen_match_market:

                    parse_failures.append({
                        "card": index + 1,
                        "reason": "duplicate_match_market",
                        "anchor_text": row["teams"],
                    })

                    continue

                seen_match_market.add(duplicate_key)

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

        # Current page snapshot.
        write_csv(
            LATEST_CSV,
            rows,
            CSV_FIELDS
        )

        # Persistent SQLite history.
        new_history_rows = save_history(rows)

        # Append only newly inserted records.
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

        # Count missing competition/country information.
        missing_country = sum(
            1 for row in rows
            if not row.get("country")
        )

        missing_league = sum(
            1 for row in rows
            if not row.get("league")
        )

        # Count market groups in the current snapshot.
        market_group_counts = {}

        for row in rows:

            group = row.get("market_group", "other")

            market_group_counts[group] = (
                market_group_counts.get(group, 0) + 1
            )

        duration = round(
            time.time() - started,
            2
        )

        debug = {
            "started_at": scraped_at,
            "url": SOURCE_URL,
            "http_status": http_status,
            "page_title": page_title,
            "final_url": final_url,

            "match_links_found": match_links_found,
            "unique_match_links": unique_match_links,
            "cards_found": len(cards),
            "parsed_rows": len(rows),

            "qualifying_rows": len(qualifying_rows),
            "signals": len(signals),
            "new_database_records": len(new_history_rows),

            "missing_country": missing_country,
            "missing_league": missing_league,

            "market_group_counts": market_group_counts,

            "pagination_links": pagination_links,

            "parse_failures": len(parse_failures),
            "parse_failure_samples": parse_failures[:30],

            "signal_samples": signals[:20],

            "errors": errors,
            "duration_seconds": duration,
            "finished_at": utc_now(),
            "result": "completed",
        }

        save_debug(debug)

        log.info("Parsed rows: %s", len(rows))
        log.info(
            "Qualifying rows: %s",
            len(qualifying_rows)
        )
        log.info("Signals: %s", len(signals))
        log.info(
            "New database records: %s",
            len(new_history_rows)
        )
        log.info(
            "Missing country: %s",
            missing_country
        )
        log.info(
            "Missing league: %s",
            missing_league
        )
        log.info(
            "Parse failures: %s",
            len(parse_failures)
        )
        log.info(
            "Finished in %s seconds",
            duration
        )

    except Exception as exc:

        duration = round(
            time.time() - started,
            2
        )

        errors.append(str(exc))

        debug = {
            "started_at": scraped_at,
            "url": SOURCE_URL,
            "http_status": http_status,
            "page_title": page_title,
            "final_url": final_url,

            "match_links_found": match_links_found,
            "unique_match_links": unique_match_links,

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
