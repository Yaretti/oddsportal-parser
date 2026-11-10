
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
GREENLINE / OddsPortal Dropping Odds Parser

Критерії відбору:
1. Початковий коефіцієнт 4.00–5.00 включно,
   новий коефіцієнт <= 2.00.
2. Початковий коефіцієнт 2.00–3.00 включно,
   новий коефіцієнт <= 1.80.

Пріоритет:
NORMAL = одна група ринків
HIGH   = дві групи ринків
TOP    = 1X2 + негативна фора + тотал

ВАЖЛИВО:
Рух коефіцієнта не є доказом грошового потоку.
"""

import csv
import hashlib
import json
import logging
import os
import re
import sqlite3
import time

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
    "https://www.oddsportal.com/dropping-odds/2/2/0/overall/"
)

HEADLESS = os.getenv("ODDSPORTAL_HEADLESS", "true").lower() != "false"

TIMEOUT_MS = int(
    os.getenv("ODDSPORTAL_TIMEOUT_MS", "60000")
)

DROP_TOLERANCE_PP = 2.0

LATEST_CSV = DATA_DIR / "oddsportal_latest.csv"
HISTORY_CSV = DATA_DIR / "oddsportal_history.csv"
SIGNALS_CSV = DATA_DIR / "oddsportal_signals.csv"

DATABASE_FILE = DATA_DIR / "oddsportal_history.sqlite"

DEBUG_JSON = DATA_DIR / "oddsportal_debug.json"
DEBUG_HTML = DATA_DIR / "oddsportal_cards_debug.html"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

log = logging.getLogger("oddsportal")


# ============================================================
# CSV FIELDS
# ============================================================

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
    "flow_to_team",
    "flow_direction",
    "flow_confidence",
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


# ============================================================
# HELPERS
# ============================================================

def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_text(value):
    if value is None:
        return ""

    return re.sub(
        r"\s+",
        " ",
        str(value).replace("\xa0", " ")
    ).strip()


def safe_float(value):
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def normalize_team_name(value):
    value = clean_text(value).lower()

    value = re.sub(
        r"[^a-zа-яіїєґ0-9]+",
        " ",
        value
    )

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

    keys = [
        "match_key",
        "market_group",
        "market_code",
        "selection_code",
        "handicap_line",
        "odds_old",
        "odds_new",
        "drop_pct",
    ]

    raw = "|".join(
        str(row.get(key, ""))
        for key in keys
    )

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()


# ============================================================
# ODDS PARSING
# ============================================================

def parse_decimal_tokens(text):

    pattern = r"(?<![\d])\d{1,4}[.,]\d{1,3}(?![\d])"

    values = []

    for match in re.finditer(pattern, text or ""):

        number = safe_float(match.group(0))

        if number is not None and 1.01 <= number <= 1000:
            values.append(number)

    return values


def parse_drop_percentage(text):

    matches = re.findall(
        r"[-−]\s*(\d{1,3}(?:[.,]\d+)?)\s*%",
        text or ""
    )

    if not matches:
        return None

    value = safe_float(matches[0])

    if value is None:
        return None

    return -abs(value)


def calculate_drop(old_odds, new_odds):

    if old_odds is None or new_odds is None:
        return None

    if old_odds <= 0:
        return None

    return (
        (new_odds - old_odds)
        / old_odds
    ) * 100


def validate_drop(old_odds, new_odds, displayed_drop):

    calculated = calculate_drop(
        old_odds,
        new_odds
    )

    if calculated is None:
        return False, None

    if displayed_drop is None:
        return False, calculated

    valid = (
        abs(calculated - displayed_drop)
        <= DROP_TOLERANCE_PP
    )

    return valid, calculated


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

    _, old_odds, new_odds, best_odds, calculated = candidates[0]

    return (
        old_odds,
        new_odds,
        best_odds,
        calculated
    )


# ============================================================
# QUALIFICATION RULES
# ============================================================

def qualifies_odds(old_odds, new_odds):

    if old_odds is None or new_odds is None:
        return False

    # Rule 1
    if 4.00 <= old_odds <= 5.00:
        return new_odds <= 2.00

    # Rule 2
    if 2.00 <= old_odds <= 3.00:
        return new_odds <= 1.80

    return False


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

    tokens = fragment.split(":", 1)[1].split(";")

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

        result.update(
            market="1X2",
            market_group="1X2"
        )

    elif code in ("over-under", "ou", "total"):

        result.update(
            market="Total",
            market_group="total"
        )

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

            if safe_float(token) is not None:
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

        found = re.search(
            pattern,
            text or "",
            re.IGNORECASE
        )

        if found:
            return clean_text(found.group(0))

    return fallback


def detect_visible_selection(text, market_data):

    before_best = re.split(
        r"Best Current Odds",
        text or "",
        flags=re.IGNORECASE
    )[0]

    lines = [
        clean_text(x)
        for x in before_best.splitlines()
    ]

    lines = [x for x in lines if x]

    group = market_data.get("market_group")

    if group == "total":

        for line in reversed(lines):

            if re.fullmatch(
                r"Over|Under",
                line,
                re.IGNORECASE
            ):
                return line.title()

    if group == "1X2":

        allowed = {
            "1", "X", "2",
            "Home", "Draw", "Away",
            "1X", "X2", "12"
        }

        for line in reversed(lines):

            if line in allowed:
                return line

    if group in (
        "negative_handicap",
        "positive_handicap"
    ):

        for line in reversed(lines):

            if line in ("1", "2", "Home", "Away"):
                return line

    code = market_data.get("selection_code", "")

    return f"code:{code}" if code else ""


# ============================================================
# FLOW DIRECTION
# ============================================================

def detect_flow_direction(selection, market_group, teams):

    selection = clean_text(selection).lower()

    sides = (
        teams.split(" - ", 1)
        if " - " in teams
        else []
    )

    home = sides[0] if len(sides) == 2 else ""
    away = sides[1] if len(sides) == 2 else ""

    result = {
        "flow_to_team": "Невизначено",
        "flow_direction": "Невизначено",
        "flow_confidence": "low",
    }

    if selection in ("home", "home team") and home:

        result.update({
            "flow_to_team": home,
            "flow_direction": "На господарів",
            "flow_confidence": "high",
        })

    elif selection in ("away", "away team") and away:

        result.update({
            "flow_to_team": away,
            "flow_direction": "На гостей",
            "flow_confidence": "high",
        })

    elif market_group == "total" and selection == "over":

        result.update({
            "flow_to_team": "Не застосовується",
            "flow_direction": "Тотал більше",
            "flow_confidence": "high",
        })

    elif market_group == "total" and selection == "under":

        result.update({
            "flow_to_team": "Не застосовується",
            "flow_direction": "Тотал менше",
            "flow_confidence": "high",
        })

    return result


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

        parts = (
            urlsplit(href)
            .path.lower()
            .strip("/")
            .split("/")
        )

        if len(parts) != 3:
            continue

        if parts[0] != "football":
            continue

        country_slug = parts[1]

        if country_slug in ("h2h", "football"):
            continue

        candidates.append({
            "country_slug": country_slug,
            "league": label or slug_to_title(parts[2]),
            "path": "/".join(parts),
        })

    if not candidates:
        return "", "", ""

    chosen = max(
        candidates,
        key=lambda item: len(item["path"])
    )

    return (
        slug_to_title(chosen["country_slug"]),
        chosen["country_slug"],
        chosen["league"]
    )


# ============================================================
# MATCH TIME AND TEAMS
# ============================================================

TIME_PATTERNS = [
    r"\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{1,2}:\d{2}\b",
    r"\b(?:Today|Tomorrow|Yesterday)\s+\d{1,2}:\d{2}\b",
    r"\b\d{1,2}:\d{2}\b",
]


def parse_match_time_and_teams(anchor_text, card_text=""):

    anchor_text = str(anchor_text or "")
    card_text = str(card_text or "")

    match_time = ""

    for source in (card_text, anchor_text):

        for pattern in TIME_PATTERNS:

            found = re.search(
                pattern,
                source,
                re.IGNORECASE
            )

            if found:
                match_time = clean_text(found.group(0))
                break

        if match_time:
            break

    lines = [
        clean_text(x)
        for x in anchor_text.splitlines()
    ]

    lines = [x for x in lines if x]

    cleaned = []

    for line in lines:

        line = re.sub(
            r"\b(?:Today|Tomorrow|Yesterday)\b",
            "",
            line,
            flags=re.IGNORECASE
        )

        line = re.sub(
            r"\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{1,2}:\d{2}\b",
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

        if line and not re.fullmatch(r"[-–—]+", line):
            cleaned.append(line)

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
                return (
                    f"{home} - {away}",
                    match_time
                )

    if len(cleaned) >= 2:

        return (
            f"{cleaned[-2]} - {cleaned[-1]}",
            match_time
        )

    if len(cleaned) == 1:
        return cleaned[0], match_time

    return "", match_time


# ============================================================
# DOM EXTRACTION
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
    let selected = null;

    for (let depth = 0; depth < 14 && node; depth++) {

      const text = node.innerText || "";

      const hasBest = /Best Current Odds/i.test(text);

      const hasDrop = /[-−]\s*\d+(?:[.,]\d+)?\s*%/.test(text);

      const odds = text.match(
        /(?<![\d])\d{1,4}[.,]\d{1,3}(?![\d])/g
      ) || [];

      const links = Array.from(
        node.querySelectorAll("a")
      ).map(a => ({
        href: a.href || a.getAttribute("href") || "",
        text: a.innerText || ""
      }));

      if (hasBest && hasDrop && odds.length >= 3) {

        selected = {
          href,
          anchorText: anchor.innerText || "",
          text,
          html: node.outerHTML || "",
          links,
          depth,
          oddsCount: odds.length
        };

        break;
      }

      node = node.parentElement;
    }

    if (selected) {

      output.push(selected);

    } else {

      output.push({
        href,
        anchorText: anchor.innerText || "",
        text: "",
        html: "",
        links: [],
        depth: -1,
        oddsCount: 0
      });

    }
  }

  const all = anchors.map(
    a => a.href || a.getAttribute("href") || ""
  );

  const pagination = Array.from(
    document.querySelectorAll("a[href]")
  )
  .filter(a =>
    /next|more|load more/i.test((a.innerText || "").trim())
    || /page=\d+/i.test(a.href || "")
  )
  .map(a => ({
    text: (a.innerText || "").trim(),
    href: a.href || ""
  }));

  return {
    cards: output,
    match_links_found: all.length,
    unique_match_links: [...new Set(all)].length,
    pagination_links: pagination
  };
}
"""


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
        anchor_text,
        text
    )

    flow = detect_flow_direction(
        market_data["selection"],
        market_data["market_group"],
        teams
    )

    match_key = make_match_key(
        teams,
        href
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

        "flow_to_team": flow["flow_to_team"],
        "flow_direction": flow["flow_direction"],
        "flow_confidence": flow["flow_confidence"],

        "handicap_line": market_data["handicap_line"],

        "drop_pct": displayed_drop,

        "calculated_drop_pct": (
            round(calculated_drop, 2)
            if calculated_drop is not None
            else ""
        ),

        "odds_old": old_odds,
        "odds_new": new_odds,
        "best_current_odds": best_odds,

        "qualifies": qualifies_odds(
            old_odds,
            new_odds
        ),

        "match_url": href,
        "match_key": match_key,
    }

    return row, None


# ============================================================
# CSV WRITING
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
# SQLITE
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
    "flow_to_team": "TEXT",
    "flow_direction": "TEXT",
    "flow_confidence": "TEXT",
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
            flow_to_team TEXT,
            flow_direction TEXT,
            flow_confidence TEXT,
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

    # Міграція без видалення старих записів.
    for column, column_type in DB_COLUMNS.items():

        if column not in existing:

            conn.execute(
                f'ALTER TABLE odds_history ADD COLUMN "{column}" {column_type}'
            )

    conn.commit()

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_odds_history_record_key
        ON odds_history(record_key)
    """)

    conn.commit()


def save_history(rows):

    conn = sqlite3.connect(DATABASE_FILE)

    new_rows = []

    try:

        initialize_database(conn)

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
                WHERE record_key=?
                LIMIT 1
                """,
                (record_key,)
            ).fetchone()

            if exists:
                continue

            values = {
                "record_key": record_key,
                **row
            }

            insert_columns = [
                key
                for key in values
                if key in columns
            ]

            # Сумісність із додатковими NOT NULL колонками
            # старої таблиці, якщо вони існують.
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

            params = [
                values.get(column, "")
                for column in insert_columns
            ]

            conn.execute(
                f"""
                INSERT INTO odds_history ({column_sql})
                VALUES ({placeholders})
                """,
                params
            )

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
                "groups": set()
            }

        grouped[key]["rows"].append(row)

        group = row.get("market_group")

        if group in (
            "1X2",
            "negative_handicap",
            "total"
        ):

            grouped[key]["groups"].add(group)

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

        market_lines = []

        for row in match_rows:

            market_lines.append(
                (
                    "{} [{}] {} -> {} | "
                    "Команда: {} | Напрямок: {}"
                ).format(
                    row.get("market", ""),
                    row.get("selection", ""),
                    row.get("odds_old", ""),
                    row.get("odds_new", ""),
                    row.get("flow_to_team", "Невизначено"),
                    row.get("flow_direction", "Невизначено")
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
            "markets": " || ".join(market_lines),
            "match_url": normalize_match_url(
                first.get("match_url", "")
            ),
            "match_key": key,
        })

    order = {
        "TOP": 0,
        "HIGH": 1,
        "NORMAL": 2
    }

    signals.sort(
        key=lambda item: (
            order.get(item["priority"], 9),
            item["teams"]
        )
    )

    return signals


# ============================================================
# DEBUG OUTPUT
# ============================================================

def save_debug(data):

    DEBUG_JSON.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )


def save_html_samples(cards):

    fragments = []

    for index, card in enumerate(cards[:15]):

        html = card.get("html", "")

        if html:

            fragments.append(
                f"\n<!-- CARD {index + 1} -->\n{html}\n"
            )

    DEBUG_HTML.write_text(
        "\n".join(fragments),
        encoding="utf-8"
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
                "--disable-dev-shm-usage"
            ]
        )

        context = browser.new_context(
            viewport={
                "width": 1440,
                "height": 1000
            },
            locale="en-US",
            timezone_id="UTC",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            )
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

        page.wait_for_timeout(6000)

        try:

            page.wait_for_selector(
                'a[href*="/football/h2h/"]',
                timeout=20000
            )

        except Exception:

            log.warning(
                "Match links did not appear before timeout."
            )

        extracted = page.evaluate(
            EXTRACT_CARDS_JS
        )

        result = {
            "http_status": http_status,
            "page_title": page.title(),
            "final_url": page.url,
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
            "body_text": clean_text(
                page.locator("body").inner_text()
            )[:3000],
        }

        context.close()
        browser.close()

        return result


# ============================================================
# MAIN
# ============================================================

def main():

    started = time.time()

    scraped_at = utc_now()

    parse_failures = []

    rows = []

    log.info("Starting OddsPortal parser")
    log.info("Source: %s", SOURCE_URL)

    result = scrape_page()

    cards = result["cards"]

    save_html_samples(cards)

    seen = set()

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

            duplicate_key = (
                row["match_key"],
                row["market_code"],
                row["selection_code"],
                row["handicap_line"],
                row["odds_old"],
                row["odds_new"],
            )

            if duplicate_key in seen:

                parse_failures.append({
                    "card": index + 1,
                    "reason": "duplicate_match_market",
                    "anchor_text": row["teams"],
                })

                continue

            seen.add(duplicate_key)

            rows.append(row)

        except Exception as exc:

            parse_failures.append({
                "card": index + 1,
                "reason": repr(exc),
                "anchor_text": clean_text(
                    card.get("anchorText", "")
                )[:300],
            })

    qualifying_rows = [
        row
        for row in rows
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

    # Preserve and append only newly recorded qualifying movements.
    new_history_rows = save_history(rows)

    append_csv(
        HISTORY_CSV,
        new_history_rows,
        CSV_FIELDS
    )

    write_csv(
        SIGNALS_CSV,
        signals,
        SIGNAL_FIELDS
    )

    missing_country = sum(
        not row.get("country")
        for row in rows
    )

    missing_league = sum(
        not row.get("league")
        for row in rows
    )

    missing_time = sum(
        not row.get("match_time")
        for row in rows
    )

    missing_teams = sum(
        not row.get("teams")
        for row in rows
    )

    group_counts = {}

    for row in rows:

        group = row.get(
            "market_group",
            "other"
        )

        group_counts[group] = (
            group_counts.get(group, 0) + 1
        )

    duration = round(
        time.time() - started,
        2
    )

    debug = {
        "started_at": scraped_at,
        "url": SOURCE_URL,
        "http_status": result["http_status"],
        "page_title": result["page_title"],
        "final_url": result["final_url"],
        "match_links_found": result["match_links_found"],
        "unique_match_links": result["unique_match_links"],
        "cards_found": len(cards),
        "parsed_rows": len(rows),
        "qualifying_rows": len(qualifying_rows),
        "signals": len(signals),
        "new_database_records": len(new_history_rows),
        "missing_country": missing_country,
        "missing_league": missing_league,
        "missing_match_time": missing_time,
        "missing_teams": missing_teams,
        "market_group_counts": group_counts,
        "pagination_links": result["pagination_links"],
        "parse_failures": len(parse_failures),
        "parse_failure_samples": parse_failures[:40],
        "signal_samples": signals[:20],
        "body_preview": result["body_text"],
        "duration_seconds": duration,
        "finished_at": utc_now(),
        "result": "completed",
    }

    save_debug(debug)

    log.info(
        "Cards=%s Parsed=%s Qualifying=%s Signals=%s NewDB=%s",
        len(cards),
        len(rows),
        len(qualifying_rows),
        len(signals),
        len(new_history_rows)
    )

    log.info(
        "Missing teams=%s time=%s country=%s league=%s failures=%s",
        missing_teams,
        missing_time,
        missing_country,
        missing_league,
        len(parse_failures)
    )

    log.info(
        "Finished in %s seconds",
        duration
    )


if __name__ == "__main__":
    main()
