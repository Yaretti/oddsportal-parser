
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OddsPortal Dropping Odds Parser

Критерії відбору:
1. Коефіцієнт 4.00–5.00 -> 2.00 або нижче.
2. Коефіцієнт 2.00–3.00 -> 1.80 або нижче.

Пріоритет:
NORMAL = одна група
HIGH   = дві групи
TOP    = усі три групи

Групи:
- 1X2
- negative_handicap
- total

Позитивна фора не враховується у HIGH/TOP.
Рух коефіцієнта не є доказом фактичного грошового потоку.
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
# CONFIG
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

SOURCE_URL = os.getenv(
    "ODDSPORTAL_URL",
    "https://www.oddsportal.com/dropping-odds/2/2/0/overall/"
)

HEADLESS = True
TIMEOUT_MS = 60000
DROP_TOLERANCE_PP = 2.0

LATEST_CSV = DATA_DIR / "oddsportal_latest.csv"
HISTORY_CSV = DATA_DIR / "oddsportal_history.csv"
SIGNALS_CSV = DATA_DIR / "oddsportal_signals.csv"

DATABASE_FILE = DATA_DIR / "oddsportal_history.sqlite"

DEBUG_JSON = DATA_DIR / "oddsportal_debug.json"
DEBUG_HTML = DATA_DIR / "oddsportal_cards_debug.html"


CSV_FIELDS = [
    "scraped_at",
    "sport",
    "country",
    "country_slug",
    "league",
    "teams",
    "home_team",
    "away_team",
    "match_time",
    "market",
    "market_code",
    "market_group",
    "selection",
    "selection_code",
    "handicap_line",
    "flow_to_team",
    "flow_direction",
    "flow_confidence",
    "drop_pct",
    "calculated_drop_pct",
    "odds_old",
    "odds_new",
    "best_current_odds",
    "qualifies",
    "match_url",
    "match_key"
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
    "flow_targets",
    "match_url",
    "match_key"
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

    return re.sub(r"\s+", " ", value).strip()


def safe_float(value):
    try:
        if value is None:
            return None

        return float(str(value).replace(",", ".").strip())

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
    base = normalize_match_url(match_url)

    if base:
        return base.lower()

    return normalize_team_name(teams)


def make_record_key(row):

    fields = (
        "match_key",
        "market_group",
        "market_code",
        "selection_code",
        "handicap_line",
        "odds_old",
        "odds_new",
        "drop_pct"
    )

    raw = "|".join(
        str(row.get(key, ""))
        for key in fields
    )

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()


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
        r"(-\s*\d{1,3}(?:[.,]\d+)?)\s*%",
        text or ""
    )

    if not matches:
        return None

    return safe_float(
        matches[0].replace(" ", "")
    )


# ============================================================
# QUALIFICATION RULES
# ============================================================

def qualifies_odds(old_odds, new_odds):

    if old_odds is None or new_odds is None:
        return False

    # Rule 1
    if 4.00 <= old_odds <= 5.00 and new_odds <= 2.00:
        return True

    # Rule 2
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

    valid = (
        abs(calculated - displayed_drop)
        <= DROP_TOLERANCE_PP
    )

    return valid, calculated


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
        "handicap_line": ""
    }

    if not url or "#" not in url:
        return result

    fragment = url.split("#", 1)[1]

    if ":" not in fragment:
        return result

    tokens = fragment.split(":", 1)[1].split(";")

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

        result.update({
            "market": "1X2",
            "market_group": "1X2"
        })

    elif code in ("over-under", "ou", "total"):

        result.update({
            "market": "Total",
            "market_group": "total"
        })

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
        r"DNB"
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


# ============================================================
# SELECTION / FLOW DIRECTION
# ============================================================

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
    code = market_data.get("market_code")

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

            if line in (
                "1", "X", "2",
                "Home", "Draw", "Away"
            ):
                return line

    selection_code = market_data.get(
        "selection_code",
        ""
    )

    if selection_code:
        return f"code:{selection_code}"

    return ""


def resolve_flow(market_data, selection):

    code = market_data.get("market_code", "")

    normalized = clean_text(selection).lower()

    unknown = {
        "flow_to_team": "Невизначено",
        "flow_direction": "unknown",
        "flow_confidence": "low"
    }

    if normalized.startswith("code:") or not normalized:
        return unknown

    # TOTAL

    if code in ("over-under", "ou", "total"):

        if normalized == "over":

            return {
                "flow_to_team": "Тотал: Over",
                "flow_direction": "total_over",
                "flow_confidence": "high"
            }

        if normalized == "under":

            return {
                "flow_to_team": "Тотал: Under",
                "flow_direction": "total_under",
                "flow_confidence": "high"
            }

    # 1X2

    if code == "1x2":

        mapping = {
            "1": ("Господарі", "home"),
            "home": ("Господарі", "home"),

            "2": ("Гості", "away"),
            "away": ("Гості", "away"),

            "x": ("Нічия", "draw"),
            "draw": ("Нічия", "draw"),

            "1x": ("1X", "home_or_draw"),
            "x2": ("X2", "draw_or_away"),
            "12": ("12", "no_draw")
        }

        if normalized in mapping:

            target, direction = mapping[normalized]

            return {
                "flow_to_team": target,
                "flow_direction": direction,
                "flow_confidence": "high"
            }

    # ASIAN / EUROPEAN HANDICAP

    if code in ("ah", "eh"):

        european = code == "eh"

        mapping = {
            "1": (
                "Господарі з урахуванням фори"
                if european
                else "Господарі (AH)",
                "home_handicap"
            ),

            "home": (
                "Господарі з урахуванням фори"
                if european
                else "Господарі (AH)",
                "home_handicap"
            ),

            "2": (
                "Гості з урахуванням фори"
                if european
                else "Гості (AH)",
                "away_handicap"
            ),

            "away": (
                "Гості з урахуванням фори"
                if european
                else "Гості (AH)",
                "away_handicap"
            )
        }

        # EH X is NOT ordinary match draw.
        if normalized == "x" and european:

            return {
                "flow_to_team": "Фора: нічия (EH X)",
                "flow_direction": "european_handicap_draw",
                "flow_confidence": "high"
            }

        if normalized in mapping:

            target, direction = mapping[normalized]

            return {
                "flow_to_team": target,
                "flow_direction": direction,
                "flow_confidence": "high"
            }

    # DRAW NO BET

    if code == "dnb":

        if normalized in ("1", "home"):

            target = "Господарі (DNB)"
            direction = "home_dnb"

        elif normalized in ("2", "away"):

            target = "Гості (DNB)"
            direction = "away_dnb"

        else:

            return unknown

        return {
            "flow_to_team": target,
            "flow_direction": direction,
            "flow_confidence": "high"
        }

    # TO QUALIFY

    if code == "qualify":

        if normalized in ("1", "home"):

            target = "Господарі: проходять далі"
            direction = "home_qualify"

        elif normalized in ("2", "away"):

            target = "Гості: проходять далі"
            direction = "away_qualify"

        else:

            return unknown

        return {
            "flow_to_team": target,
            "flow_direction": direction,
            "flow_confidence": "high"
        }

    return {
        "flow_to_team": f"Вибір: {selection}",
        "flow_direction": "unmapped_selection",
        "flow_confidence": "medium"
    }


# ============================================================
# COUNTRY / LEAGUE
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
    "world": "International"
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

        if len(parts) != 3:
            continue

        if parts[0] != "football":
            continue

        country_slug = parts[1]
        league_slug = parts[2]

        if country_slug in ("h2h", "football"):
            continue

        candidates.append({
            "country_slug": country_slug,
            "league": label or slug_to_title(league_slug),
            "path": path
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
# DATE / MATCH PARSING
# ============================================================

def parse_match_time_and_teams(anchor_text):

    lines = [
        clean_text(x)
        for x in str(anchor_text).splitlines()
    ]

    lines = [x for x in lines if x]

    full_text = clean_text(anchor_text)

    date_time_pattern = re.compile(
        r"^\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{1,2}:\d{2})\s+(.+?)\s*$",
        re.IGNORECASE
    )

    relative_pattern = re.compile(
        r"^\s*((?:(?:Today|Tomorrow|Yesterday)\s+)?\d{1,2}:\d{2})\s+(.+?)\s*$",
        re.IGNORECASE
    )

    match_time = ""
    cleaned = []

    for line in lines:

        date_match = date_time_pattern.match(line)

        relative_match = relative_pattern.match(line)

        if date_match:

            match_time = clean_text(
                date_match.group(1)
            )

            cleaned.append(
                clean_text(date_match.group(2))
            )

            continue

        if relative_match:

            match_time = clean_text(
                relative_match.group(1)
            )

            cleaned.append(
                clean_text(relative_match.group(2))
            )

            continue

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

        line = re.sub(
            r"\b\d{1,2}\s+[A-Za-z]{3}\b",
            "",
            line,
            flags=re.IGNORECASE
        )

        line = clean_text(line)

        if line and not re.fullmatch(r"[-–—]+", line):

            if line.lower() not in ("football", "h2h"):
                cleaned.append(line)

    # Explicit team separator

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
                    match_time,
                    home,
                    away
                )

    # Separate lines for teams

    if len(cleaned) >= 2:

        home = cleaned[-2]
        away = cleaned[-1]

        return (
            f"{home} - {away}",
            match_time,
            home,
            away
        )

    if len(cleaned) == 1:

        return (
            cleaned[0],
            match_time,
            cleaned[0],
            ""
        )

    return full_text, match_time, "", ""


# ============================================================
# PLAYWRIGHT EXTRACTION
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

    if (!href || seen.has(href)) {
      continue;
    }

    seen.add(href);

    let node = anchor;
    let selected = null;

    for (let depth = 0; depth < 13 && node; depth++) {

      const text = node.innerText || "";

      const hasBest = /Best Current Odds/i.test(text);

      const hasDrop =
        /-\s*\d+(?:[.,]\d+)?\s*%/.test(text);

      const odds =
        text.match(
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

    output.push(
      selected || {
        href,
        anchorText: anchor.innerText || "",
        text: "",
        html: "",
        links: [],
        depth: -1,
        oddsCount: 0
      }
    );
  }

  const paginationLinks = Array.from(
    document.querySelectorAll("a[href]")
  )
  .filter(a =>
    /next|more|load more/i.test(
      (a.innerText || "").trim()
    ) ||
    /page=\d+/i.test(a.href || "")
  )
  .map(a => ({
    text: (a.innerText || "").trim(),
    href: a.href || ""
  }));

  return {
    cards: output,
    match_links_found: anchors.length,
    unique_match_links: seen.size,
    pagination_links: paginationLinks
  };
}
"""


def scrape_page():

    with sync_playwright() as p:

        browser = p.chromium.launch(
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
                "AppleWebKit/537.36 (KHTML, like Gecko) "
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
            )
        }

        context.close()
        browser.close()

        return result


# ============================================================
# ODDS EXTRACTION
# ============================================================

def extract_odds_from_card(text, displayed_drop):

    numbers = parse_decimal_tokens(text)

    if len(numbers) < 3:
        return None, None, None, None

    candidates = []

    for i in range(len(numbers) - 2):

        old_odds, new_odds, best_odds = numbers[i:i + 3]

        valid, calculated = validate_drop(
            old_odds,
            new_odds,
            displayed_drop
        )

        if valid:

            candidates.append((
                i,
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


def parse_card(card, scraped_at):

    href = card.get("href", "")
    text = card.get("text", "")
    anchor_text = card.get("anchorText", "")

    if not href or not text:
        return None, "card_container_not_found"

    displayed_drop = parse_drop_percentage(text)

    if displayed_drop is None:
        return None, "drop_percentage_not_found"

    old_odds, new_odds, best_odds, calculated_drop = (
        extract_odds_from_card(
            text,
            displayed_drop
        )
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

    flow = resolve_flow(
        market_data,
        market_data["selection"]
    )

    country, country_slug, league = extract_country_league(
        card.get("links", [])
    )

    teams, match_time, home_team, away_team = (
        parse_match_time_and_teams(anchor_text)
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
        "home_team": home_team,
        "away_team": away_team,
        "match_time": match_time,

        "market": market_data["market"],
        "market_code": market_data["market_code"],
        "market_group": market_data["market_group"],

        "selection": market_data["selection"],
        "selection_code": market_data["selection_code"],
        "handicap_line": market_data["handicap_line"],

        "flow_to_team": flow["flow_to_team"],
        "flow_direction": flow["flow_direction"],
        "flow_confidence": flow["flow_confidence"],

        "drop_pct": displayed_drop,
        "calculated_drop_pct": round(
            calculated_drop,
            2
        ),

        "odds_old": old_odds,
        "odds_new": new_odds,
        "best_current_odds": best_odds,

        "qualifies": qualifies_odds(
            old_odds,
            new_odds
        ),

        "match_url": href,
        "match_key": match_key
    }

    return row, None


# ============================================================
# CSV
# ============================================================

def write_csv(path, rows, fields):

    with open(
        path,
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as f:

        writer = csv.DictWriter(
            f,
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
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=fields,
            extrasaction="ignore"
        )

        if not exists:
            writer.writeheader()

        writer.writerows(rows)


# ============================================================
# SQLITE MIGRATION
# ============================================================

DB_COLUMNS = {

    "record_key": "TEXT",

    "scraped_at": "TEXT",

    "sport": "TEXT",

    "country": "TEXT",
    "country_slug": "TEXT",
    "league": "TEXT",

    "teams": "TEXT",
    "home_team": "TEXT",
    "away_team": "TEXT",
    "match_time": "TEXT",

    "market": "TEXT",
    "market_code": "TEXT",
    "market_group": "TEXT",

    "selection": "TEXT",
    "selection_code": "TEXT",
    "handicap_line": "TEXT",

    "flow_to_team": "TEXT",
    "flow_direction": "TEXT",
    "flow_confidence": "TEXT",

    "drop_pct": "REAL",
    "calculated_drop_pct": "REAL",

    "odds_old": "REAL",
    "odds_new": "REAL",
    "best_current_odds": "REAL",

    "qualifies": "INTEGER",

    "match_url": "TEXT",
    "match_key": "TEXT"
}


def initialize_database(conn):

    definitions = ",\n".join(
        f'"{name}" {kind}'
        for name, kind in DB_COLUMNS.items()
    )

    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS odds_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            {definitions}
        )
    """)

    existing = {
        item[1]
        for item in conn.execute(
            "PRAGMA table_info(odds_history)"
        ).fetchall()
    }

    # Non-destructive migration of old database.
    for name, kind in DB_COLUMNS.items():

        if name not in existing:

            conn.execute(
                f'ALTER TABLE odds_history ADD COLUMN "{name}" {kind}'
            )

    conn.commit()

    info = conn.execute(
        "PRAGMA table_info(odds_history)"
    ).fetchall()

    column_names = [
        item[1]
        for item in info
    ]

    old_rows = conn.execute("""
        SELECT *
        FROM odds_history
        WHERE record_key IS NULL
           OR record_key = ''
    """).fetchall()

    for old in old_rows:

        data = dict(
            zip(column_names, old)
        )

        match_key = data.get("match_key") or make_match_key(
            data.get("teams", ""),
            data.get("match_url", "")
        )

        temp = dict(data)
        temp["match_key"] = match_key

        record_key = make_record_key(temp)

        conn.execute("""
            UPDATE odds_history
            SET record_key = ?,
                match_key = ?
            WHERE id = ?
        """, (
            record_key,
            match_key,
            data.get("id")
        ))

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_odds_history_record_key
        ON odds_history(record_key)
    """)

    conn.commit()


def save_history(rows):

    conn = sqlite3.connect(DATABASE_FILE)

    try:

        initialize_database(conn)

        table_info = conn.execute(
            "PRAGMA table_info(odds_history)"
        ).fetchall()

        columns = {
            item[1]
            for item in table_info
        }

        new_rows = []

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
                **row
            }

            insert_columns = [
                key
                for key in values
                if key in columns
            ]

            # Support older schemas with required columns.
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

            names_sql = ", ".join(
                f'"{name}"'
                for name in insert_columns
            )

            marks = ", ".join(
                "?"
                for _ in insert_columns
            )

            params = [
                values.get(name, "")
                for name in insert_columns
            ]

            conn.execute(
                f"""
                INSERT INTO odds_history ({names_sql})
                VALUES ({marks})
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

        grouped.setdefault(
            key,
            {
                "rows": [],
                "groups": set()
            }
        )

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

        match_rows = item["rows"]

        first = match_rows[0]

        market_descriptions = []
        flow_targets = []

        for row in match_rows:

            market_descriptions.append(
                "{} [{}] {} -> {}".format(
                    row.get("market", ""),
                    row.get("selection", ""),
                    row.get("odds_old", ""),
                    row.get("odds_new", "")
                )
            )

            target = row.get(
                "flow_to_team",
                "Невизначено"
            )

            if target not in flow_targets:
                flow_targets.append(target)

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

            "markets": " || ".join(
                market_descriptions
            ),

            "flow_targets": " | ".join(
                flow_targets
            ),

            "match_url": normalize_match_url(
                first.get("match_url", "")
            ),

            "match_key": key
        })

    order = {
        "TOP": 0,
        "HIGH": 1,
        "NORMAL": 2
    }

    signals.sort(
        key=lambda x: (
            order.get(x["priority"], 9),
            x["teams"]
        )
    )

    return signals


# ============================================================
# DEBUG
# ============================================================

def save_debug(data):

    with open(
        DEBUG_JSON,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )


def save_html_samples(cards):

    fragments = []

    for index, card in enumerate(cards[:10]):

        html = card.get("html", "")

        if html:

            fragments.append(
                f"\n<!-- CARD {index + 1} -->\n{html}\n"
            )

    with open(
        DEBUG_HTML,
        "w",
        encoding="utf-8"
    ) as f:

        f.write("\n".join(fragments))


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

    seen_market = set()

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
                    )[:700]
                })

                continue

            duplicate_key = (

                row["match_key"],

                row["market_code"],

                row["selection_code"],

                row["handicap_line"]
            )

            if duplicate_key in seen_market:

                parse_failures.append({

                    "card": index + 1,

                    "reason": "duplicate_match_market",

                    "anchor_text": row["teams"]
                })

                continue

            seen_market.add(duplicate_key)

            rows.append(row)

        except Exception as exc:

            parse_failures.append({

                "card": index + 1,

                "reason": str(exc),

                "anchor_text": clean_text(
                    card.get("anchorText", "")
                )[:300]
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

    write_csv(
        LATEST_CSV,
        rows,
        CSV_FIELDS
    )

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
        1 for row in rows
        if not row.get("country")
    )

    missing_league = sum(
        1 for row in rows
        if not row.get("league")
    )

    market_group_counts = {}

    for row in rows:

        group = row.get(
            "market_group",
            "other"
        )

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

        "http_status": result.get("http_status"),

        "page_title": result.get("page_title", ""),

        "final_url": result.get("final_url", ""),

        "match_links_found": result.get(
            "match_links_found",
            0
        ),

        "unique_match_links": result.get(
            "unique_match_links",
            0
        ),

        "cards_found": len(cards),

        "parsed_rows": len(rows),

        "qualifying_rows": len(qualifying_rows),

        "signals": len(signals),

        "new_database_records": len(new_history_rows),

        "missing_country": missing_country,

        "missing_league": missing_league,

        "market_group_counts": market_group_counts,

        "pagination_links": result.get(
            "pagination_links",
            []
        ),

        "parse_failures": len(parse_failures),

        "parse_failure_samples": parse_failures[:30],

        "signal_samples": signals[:20],

        "duration_seconds": duration,

        "finished_at": utc_now(),

        "result": "completed"
    }

    save_debug(debug)

    log.info("Parsed rows: %s", len(rows))

    log.info(
        "Qualifying rows: %s",
        len(qualifying_rows)
    )

    log.info(
        "Signals: %s",
        len(signals)
    )

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


if __name__ == "__main__":
    main()
