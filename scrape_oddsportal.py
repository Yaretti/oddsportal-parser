
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OddsPortal Dropping Odds parser

Features:
- Parses individual market selections.
- Extracts old/new/best odds and validates displayed drop.
- Identifies the team/outcome receiving the odds movement.
- Separates 1X2, negative handicap, positive handicap and totals.
- Applies the user's exact odds criteria.
- Groups qualifying markets into NORMAL / HIGH / TOP.
- Saves latest, history, signals, SQLite and debug files.

IMPORTANT:
The parser does not claim to know actual betting stake volume.
Odds movement is not proof of money flow.
"""

import asyncio
import csv
import json
import logging
import os
import re
import sqlite3
import hashlib

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)


# ============================================================
# CONFIGURATION
# ============================================================

SOURCE_URL = os.getenv(
    "ODDSPORTAL_URL",
    "https://www.oddsportal.com/dropping-odds/2/2/0/overall/"
)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

LATEST_FILE = DATA_DIR / "oddsportal_latest.csv"
HISTORY_FILE = DATA_DIR / "oddsportal_history.csv"
SIGNALS_FILE = DATA_DIR / "oddsportal_signals.csv"
DEBUG_FILE = DATA_DIR / "oddsportal_debug.json"
HTML_DEBUG_FILE = DATA_DIR / "oddsportal_cards_debug.html"
DB_FILE = DATA_DIR / "oddsportal_history.sqlite"

MAX_PAGES = int(os.getenv("MAX_PAGES", "5"))
DROP_VALIDATION_TOLERANCE = float(
    os.getenv("DROP_VALIDATION_TOLERANCE", "2.5")
)

PAGE_WAIT_MS = int(os.getenv("PAGE_WAIT_MS", "5000"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)


# ============================================================
# CSV SCHEMA
# ============================================================

FIELDS = [
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

    "flow_direction",
    "flow_to_team",
    "flow_confidence",

    "drop_pct",
    "calculated_drop_pct",

    "odds_old",
    "odds_new",
    "best_current_odds",

    "qualifies",
    "match_url",
    "match_key",
    "record_key",
]


# ============================================================
# HELPERS
# ============================================================

def now_utc():
    return datetime.now(timezone.utc).isoformat()


def number(value):
    if value is None:
        return None

    try:
        return float(str(value).replace(",", ".").strip())
    except (ValueError, TypeError):
        return None


def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def slug_to_name(value):
    value = (value or "").replace("-", " ").replace("_", " ")
    return " ".join(word.capitalize() for word in value.split())


def split_teams(value):
    value = clean_text(value)

    separators = [
        " - ",
        " – ",
        " — ",
        " vs ",
        " v ",
    ]

    for separator in separators:
        if separator in value:
            parts = value.split(separator, 1)
            if len(parts) == 2:
                return parts[0].strip(), parts[1].strip()

    return value, ""


def canonical_match_url(url):
    """
    Removes the market fragment.
    Keeps the event URL as the match identifier.
    """

    if not url:
        return ""

    parts = urlsplit(url)

    return urlunsplit((
        parts.scheme.lower(),
        parts.netloc.lower(),
        parts.path.lower().rstrip("/"),
        "",
        "",
    ))


def make_hash(value):
    return hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()[:24]


# ============================================================
# URL MARKET PARSING
# ============================================================

def parse_market_fragment(url):
    """
    Examples:

    #OISydU3L:over-under;2;6.50;0
    #ITTAoLdt:1X2;2
    #4z1nvQcc:eh;2;1.00;0
    #zuiH5zFs:ah;2;0.75;0
    """

    result = {
        "market_code": "",
        "selection_code": "",
        "handicap_line": "",
        "market_group": "other",
    }

    if "#" not in (url or ""):
        return result

    fragment = url.split("#", 1)[1]

    if ":" in fragment:
        fragment = fragment.split(":", 1)[1]

    parts = fragment.split(";")

    if not parts:
        return result

    market_code = parts[0].strip().lower()

    selection_code = (
        parts[1].strip() if len(parts) > 1 else ""
    )

    line = (
        number(parts[2]) if len(parts) > 2 else None
    )

    result["market_code"] = market_code
    result["selection_code"] = selection_code

    if line is not None:
        result["handicap_line"] = f"{line:.2f}"

    if market_code in ("1x2",):
        result["market_group"] = "1X2"

    elif market_code in ("over-under", "total"):
        result["market_group"] = "total"

    elif market_code in ("ah", "eh"):
        if line is None:
            result["market_group"] = "handicap_unknown"
        elif line < 0:
            result["market_group"] = "negative_handicap"
        elif line > 0:
            result["market_group"] = "positive_handicap"
        else:
            result["market_group"] = "zero_handicap"

    else:
        result["market_group"] = "other"

    return result


def market_display(market_code, line):
    if market_code == "1x2":
        return "1X2"

    if market_code == "over-under":
        if line:
            return f"O/U {float(line):g}"
        return "Over/Under"

    if market_code == "ah":
        if line:
            sign = "+" if float(line) > 0 else ""
            return f"AH {sign}{float(line):g}"
        return "Asian Handicap"

    if market_code == "eh":
        if line:
            sign = "+" if float(line) > 0 else ""
            return f"EH {sign}{float(line):g}"
        return "European Handicap"

    names = {
        "dnb": "DNB",
        "ht-ft": "HT/FT",
        "qualify": "To Qualify",
        "btts": "Both Teams To Score",
        "double-chance": "Double Chance",
    }

    return names.get(market_code, market_code or "Unknown")


# ============================================================
# SELECTION AND TEAM FLOW
# ============================================================

def find_visible_selection(card_text, market_code):
    """
    Reads the selection displayed in the market card.

    It deliberately does not guess from arbitrary numeric
    values in the odds table.
    """

    lines = [
        clean_text(line)
        for line in (card_text or "").splitlines()
        if clean_text(line)
    ]

    normalized = [
        line.lower()
        for line in lines
    ]

    market_markers = (
        "best current odds",
        "best odds",
    )

    stop_index = len(lines)

    for i, line in enumerate(normalized):
        if any(marker in line for marker in market_markers):
            stop_index = i
            break

    candidates = lines[:stop_index]

    if market_code == "over-under":
        for line in candidates:
            low = line.lower()

            if low in ("over", "under"):
                return line.title()

        return ""

    if market_code == "1x2":
        for line in candidates:
            if line.upper() in ("1", "X", "2"):
                return line.upper()

            if line.lower() in (
                "draw",
                "home",
                "away",
            ):
                return line.title()

        return ""

    if market_code in ("ah", "eh", "dnb", "qualify"):
        for line in candidates:
            if line.upper() in ("1", "2", "X"):
                return line.upper()

        return ""

    return ""


def determine_flow(
    market_code,
    selection,
    selection_code,
    home_team,
    away_team,
):
    """
    Determines the outcome/team receiving the odds movement.

    A team is assigned only when the selection is explicit.

    For 1X2, the known OddsPortal URL convention from the
    supplied examples is used as a fallback only for code 2.
    Other undocumented codes are not guessed.
    """

    selection = clean_text(selection)
    normalized = selection.lower()

    result = {
        "flow_direction": "Не визначено",
        "flow_to_team": "Не визначено",
        "flow_confidence": "low",
    }

    # TOTALS: no team is the target.
    if market_code == "over-under":
        if normalized == "over":
            return {
                "flow_direction": "Over",
                "flow_to_team": "Тотал: Over",
                "flow_confidence": "high",
            }

        if normalized == "under":
            return {
                "flow_direction": "Under",
                "flow_to_team": "Тотал: Under",
                "flow_confidence": "high",
            }

        return {
            "flow_direction": "Total: unknown",
            "flow_to_team": "Тотал: вибір не визначено",
            "flow_confidence": "low",
        }

    # 1X2
    if market_code == "1x2":
        if normalized in ("1", "home"):
            return {
                "flow_direction": "1 / Господарі",
                "flow_to_team": home_team or "Господарі",
                "flow_confidence": "high",
            }

        if normalized in ("2", "away"):
            return {
                "flow_direction": "2 / Гості",
                "flow_to_team": away_team or "Гості",
                "flow_confidence": "high",
            }

        if normalized in ("x", "draw"):
            return {
                "flow_direction": "X / Нічия",
                "flow_to_team": "Нічия",
                "flow_confidence": "high",
            }

        # Known example supplied earlier:
        # 1X2;2 corresponds to the away selection.
        if str(selection_code) == "2":
            return {
                "flow_direction": "2 / Гості (за кодом URL)",
                "flow_to_team": away_team or "Гості",
                "flow_confidence": "medium",
            }

        return result

    # Handicap and other team-based markets.
    if normalized in ("1", "home"):
        return {
            "flow_direction": "1 / Господарі",
            "flow_to_team": home_team or "Господарі",
            "flow_confidence": "high",
        }

    if normalized in ("2", "away"):
        return {
            "flow_direction": "2 / Гості",
            "flow_to_team": away_team or "Гості",
            "flow_confidence": "high",
        }

    if normalized in ("x", "draw"):
        return {
            "flow_direction": "X / Нічия",
            "flow_to_team": "Нічия",
            "flow_confidence": "high",
        }

    return result


# ============================================================
# ODDS VALIDATION
# ============================================================

def calculate_drop(old, new):
    if not old or old <= 0:
        return None

    if new is None:
        return None

    return ((new - old) / old) * 100.0


def extract_valid_odds(card_text, displayed_drop):
    """
    Finds the first three odds values after the displayed
    percentage that mathematically match the drop.

    Example:
    old=59.00, new=21.50 => -63.56%
    displayed drop=-64%
    """

    text = card_text or ""

    drop_match = re.search(
        r"(-\s*\d+(?:[.,]\d+)?)\s*%",
        text
    )

    if not drop_match:
        return None

    drop = number(
        drop_match.group(1).replace(" ", "")
    )

    after_drop = text[drop_match.end():]

    values = re.findall(
        r"(?<![\w:])\d{1,5}(?:[.,]\d+)",
        after_drop
    )

    values = [
        number(value)
        for value in values
    ]

    values = [
        value for value in values
        if value is not None and value > 0
    ]

    if len(values) < 3:
        return None

    # Test consecutive triplets because page markup may
    # contain extra numeric values.
    for index in range(len(values) - 2):
        old, new, best = values[index:index + 3]

        calculated = calculate_drop(old, new)

        if calculated is None:
            continue

        if abs(calculated - drop) <= DROP_VALIDATION_TOLERANCE:
            return {
                "drop_pct": drop,
                "calculated_drop_pct": round(calculated, 2),
                "odds_old": old,
                "odds_new": new,
                "best_current_odds": best,
            }

    return None


# ============================================================
# QUALIFICATION
# ============================================================

def qualifies(old, new):
    if old is None or new is None:
        return False

    # Rule 1:
    # Initial odds 4.00–5.00 -> new odds <= 2.00
    if 4.00 <= old <= 5.00 and new <= 2.00:
        return True

    # Rule 2:
    # Initial odds 2.00–3.00 -> new odds <= 1.80
    if 2.00 <= old <= 3.00 and new <= 1.80:
        return True

    return False


# ============================================================
# CARD PARSING
# ============================================================

def parse_event_time_and_teams(raw_label):
    raw_label = clean_text(raw_label)

    patterns = [
        r"^(Today|Tomorrow)\s+(\d{1,2}:\d{2})\s+(.+)$",
        r"^(\d{1,2}\s+\w+)\s+(\d{1,2}:\d{2})\s+(.+)$",
    ]

    for pattern in patterns:
        match = re.match(pattern, raw_label, re.I)

        if match:
            match_time = (
                f"{match.group(1)} {match.group(2)}"
            )
            teams = match.group(3).strip()
            return match_time, teams

    return "", raw_label


def parse_card(card):
    url = urljoin(SOURCE_URL, card.get("url", ""))

    raw_label = card.get("teams", "")
    match_time, teams = parse_event_time_and_teams(raw_label)

    home_team, away_team = split_teams(teams)

    if not home_team or not away_team:
        return None

    market_data = parse_market_fragment(url)

    market_code = market_data["market_code"]
    selection_code = market_data["selection_code"]
    handicap_line = market_data["handicap_line"]

    market_group = market_data["market_group"]

    market = market_display(
        market_code,
        handicap_line
    )

    odds_data = extract_valid_odds(
        card.get("text", ""),
        None
    )

    if not odds_data:
        return None

    selection = find_visible_selection(
        card.get("text", ""),
        market_code
    )

    flow = determine_flow(
        market_code,
        selection,
        selection_code,
        home_team,
        away_team,
    )

    match_url = canonical_match_url(url)
    match_key = make_hash(match_url)

    record_identity = "|".join([
        match_key,
        market_code,
        selection_code,
        str(handicap_line),
    ])

    record_key = make_hash(record_identity)

    return {
        "scraped_at": now_utc(),

        "sport": "Football",
        "country": card.get("country", ""),
        "country_slug": card.get("country_slug", ""),
        "league": card.get("league", ""),

        "teams": teams,
        "home_team": home_team,
        "away_team": away_team,
        "match_time": match_time,

        "market": market,
        "market_code": market_code,
        "market_group": market_group,

        "selection": selection or "Не визначено",
        "selection_code": selection_code,
        "handicap_line": handicap_line,

        "flow_direction": flow["flow_direction"],
        "flow_to_team": flow["flow_to_team"],
        "flow_confidence": flow["flow_confidence"],

        "drop_pct": odds_data["drop_pct"],
        "calculated_drop_pct": odds_data["calculated_drop_pct"],

        "odds_old": odds_data["odds_old"],
        "odds_new": odds_data["odds_new"],
        "best_current_odds": odds_data["best_current_odds"],

        "qualifies": qualifies(
            odds_data["odds_old"],
            odds_data["odds_new"],
        ),

        "match_url": match_url,
        "match_key": match_key,
        "record_key": record_key,
    }


# ============================================================
# BROWSER EXTRACTION
# ============================================================

async def extract_cards(page):
    return await page.locator(
        'a[href*="/football/h2h/"]'
    ).evaluate_all("""
        anchors => {
            const output = [];
            const seen = new Set();

            for (const a of anchors) {
                const href = a.href || "";
                const label = (a.innerText || "").trim();

                if (!href || !label) continue;

                let node = a;
                let selected = null;

                for (let depth = 0; depth < 14 && node; depth++) {
                    const text = (node.innerText || "").trim();

                    const hasDrop = /-\\s*\\d+(?:[.,]\\d+)?\\s*%/.test(text);
                    const hasBest = /Best Current Odds/i.test(text);

                    if (
                        hasDrop &&
                        hasBest &&
                        text.length < 3500
                    ) {
                        selected = node;
                        break;
                    }

                    node = node.parentElement;
                }

                if (!selected) continue;

                const cardText = (selected.innerText || "").trim();

                // Avoid duplicate DOM anchors pointing to
                // exactly the same market card.
                const key = href.toLowerCase();

                if (seen.has(key)) continue;
                seen.add(key);

                let country = "";
                let countrySlug = "";
                let league = "";

                const links = selected.querySelectorAll(
                    'a[href*="/football/"]'
                );

                for (const link of links) {
                    const linkHref = link.getAttribute("href") || "";
                    const parts = linkHref.split("/").filter(Boolean);

                    // /football/country/competition/
                    if (
                        parts.length >= 3 &&
                        parts[0] === "football" &&
                        !linkHref.includes("/h2h/")
                    ) {
                        countrySlug = parts[1] || "";
                        country = (link.innerText || "").trim();
                        league = (link.innerText || "").trim();

                        if (countrySlug && league) break;
                    }
                }

                output.push({
                    teams: label,
                    url: href,
                    text: cardText,
                    country: country,
                    country_slug: countrySlug,
                    league: league
                });
            }

            return output;
        }
    """)


async def scrape():
    rows = []
    failures = []
    html_samples = []

    debug = {
        "started_at": now_utc(),
        "url": SOURCE_URL,
        "pages_scanned": 0,
        "cards_found": 0,
        "parsed_rows": 0,
        "parse_failures": 0,
        "duplicate_records": 0,
        "qualifying_rows": 0,
        "signals": 0,
        "errors": [],
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox"],
        )

        page = await browser.new_page(
            locale="en-GB",
            timezone_id="Europe/Kyiv",
            viewport={
                "width": 1440,
                "height": 1100,
            },
        )

        await page.set_extra_http_headers({
            "Accept-Language": "en-GB,en;q=0.9"
        })

        response = await page.goto(
            SOURCE_URL,
            wait_until="domcontentloaded",
            timeout=90000,
        )

        if response and response.status >= 400:
            await browser.close()
            raise RuntimeError(
                f"OddsPortal HTTP {response.status}"
            )

        try:
            await page.wait_for_load_state(
                "networkidle",
                timeout=20000,
            )
        except PlaywrightTimeoutError:
            logging.warning(
                "Network idle timeout; continuing"
            )

        await page.wait_for_timeout(PAGE_WAIT_MS)

        seen_records = set()

        for page_no in range(MAX_PAGES):
            debug["pages_scanned"] += 1

            cards = await extract_cards(page)

            logging.info(
                "Page %s: %s market cards",
                page_no + 1,
                len(cards),
            )

            debug["cards_found"] += len(cards)

            for card in cards:
                try:
                    row = parse_card(card)

                    if not row:
                        debug["parse_failures"] += 1

                        failures.append({
                            "page": page_no + 1,
                            "teams": card.get("teams", ""),
                            "reason": "odds_or_teams_not_parsed",
                        })

                        if len(html_samples) < 5:
                            html_samples.append(
                                card.get("text", "")
                            )

                        continue

                    key = row["record_key"]

                    if key in seen_records:
                        debug["duplicate_records"] += 1
                        continue

                    seen_records.add(key)
                    rows.append(row)

                except Exception as exc:
                    debug["parse_failures"] += 1

                    failures.append({
                        "page": page_no + 1,
                        "teams": card.get("teams", ""),
                        "reason": str(exc),
                    })

            if page_no + 1 >= MAX_PAGES:
                break

            next_button = page.get_by_role(
                "button",
                name=re.compile(r"^Next$", re.I),
            )

            if (
                await next_button.count() == 0
                or not await next_button.first.is_enabled()
            ):
                break

            await next_button.first.click()
            await page.wait_for_timeout(3000)

        await browser.close()

    debug["parsed_rows"] = len(rows)
    debug["qualifying_rows"] = sum(
        1 for row in rows if row["qualifies"]
    )

    debug["parse_failure_samples"] = failures[:20]
    debug["market_group_counts"] = {}

    for row in rows:
        group = row["market_group"]
        debug["market_group_counts"][group] = (
            debug["market_group_counts"].get(group, 0) + 1
        )

    debug["flow_confidence_counts"] = {}

    for row in rows:
        confidence = row["flow_confidence"]
        debug["flow_confidence_counts"][confidence] = (
            debug["flow_confidence_counts"].get(confidence, 0) + 1
        )

    HTML_DEBUG_FILE.write_text(
        "\n\n--- CARD ---\n\n".join(html_samples),
        encoding="utf-8",
    )

    return rows, debug


# ============================================================
# SIGNAL GROUPING
# ============================================================

def build_signals(rows):
    """
    NORMAL: one qualifying market group.
    HIGH: two qualifying market groups.
    TOP: all three groups:
          1X2 + negative_handicap + total.

    Positive handicap never counts as negative handicap.
    """

    grouped = {}

    for row in rows:
        if not row["qualifies"]:
            continue

        match_key = row["match_key"]

        grouped.setdefault(match_key, {
            "match_key": match_key,
            "teams": row["teams"],
            "home_team": row["home_team"],
            "away_team": row["away_team"],
            "league": row["league"],
            "country": row["country"],
            "match_time": row["match_time"],
            "match_url": row["match_url"],
            "groups": set(),
            "rows": [],
        })

        grouped[match_key]["groups"].add(
            row["market_group"]
        )

        grouped[match_key]["rows"].append(row)

    signals = []

    target_groups = {
        "1X2",
        "negative_handicap",
        "total",
    }

    for item in grouped.values():
        groups = item["groups"]

        relevant_groups = groups.intersection(
            target_groups
        )

        count = len(relevant_groups)

        if count == 0:
            continue

        if target_groups.issubset(groups):
            priority = "TOP"
        elif count == 2:
            priority = "HIGH"
        else:
            priority = "NORMAL"

        flow_targets = sorted(set(
            row["flow_to_team"]
            for row in item["rows"]
            if row["flow_to_team"]
        ))

        signals.append({
            "scraped_at": now_utc(),
            "priority": priority,
            "teams": item["teams"],
            "home_team": item["home_team"],
            "away_team": item["away_team"],
            "country": item["country"],
            "league": item["league"],
            "match_time": item["match_time"],
            "qualifying_groups": ",".join(
                sorted(relevant_groups)
            ),
            "qualifying_group_count": count,
            "flow_targets": " | ".join(flow_targets),
            "qualifying_markets": " | ".join(
                row["market"] for row in item["rows"]
            ),
            "qualifying_selections": " | ".join(
                row["selection"] for row in item["rows"]
            ),
            "match_url": item["match_url"],
            "match_key": item["match_key"],
        })

    priority_order = {
        "TOP": 0,
        "HIGH": 1,
        "NORMAL": 2,
    }

    signals.sort(
        key=lambda item: priority_order[item["priority"]]
    )

    return signals


# ============================================================
# SQLITE
# ============================================================

def init_db():
    connection = sqlite3.connect(DB_FILE)

    connection.execute("""
        CREATE TABLE IF NOT EXISTS odds_history (
            record_key TEXT PRIMARY KEY,
            scraped_at TEXT,
            match_key TEXT,
            teams TEXT,
            market_code TEXT,
            market_group TEXT,
            selection TEXT,
            selection_code TEXT,
            flow_to_team TEXT,
            flow_direction TEXT,
            odds_old REAL,
            odds_new REAL,
            best_current_odds REAL,
            drop_pct REAL,
            calculated_drop_pct REAL,
            qualifies INTEGER,
            raw_json TEXT
        )
    """)

    connection.commit()
    connection.close()


def save_database(rows):
    init_db()

    connection = sqlite3.connect(DB_FILE)

    inserted = 0

    for row in rows:
        cursor = connection.execute("""
            INSERT OR IGNORE INTO odds_history (
                record_key,
                scraped_at,
                match_key,
                teams,
                market_code,
                market_group,
                selection,
                selection_code,
                flow_to_team,
                flow_direction,
                odds_old,
                odds_new,
                best_current_odds,
                drop_pct,
                calculated_drop_pct,
                qualifies,
                raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            row["record_key"],
            row["scraped_at"],
            row["match_key"],
            row["teams"],
            row["market_code"],
            row["market_group"],
            row["selection"],
            row["selection_code"],
            row["flow_to_team"],
            row["flow_direction"],
            row["odds_old"],
            row["odds_new"],
            row["best_current_odds"],
            row["drop_pct"],
            row["calculated_drop_pct"],
            int(row["qualifies"]),
            json.dumps(row, ensure_ascii=False),
        ))

        inserted += cursor.rowcount

    connection.commit()
    connection.close()

    return inserted


# ============================================================
# CSV OUTPUT
# ============================================================

def write_csv(path, rows, fields):
    with path.open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=fields,
            extrasaction="ignore",
        )

        writer.writeheader()
        writer.writerows(rows)


def append_history(rows):
    existing_keys = set()

    if HISTORY_FILE.exists():
        try:
            with HISTORY_FILE.open(
                "r",
                newline="",
                encoding="utf-8-sig",
            ) as file:
                reader = csv.DictReader(file)

                for row in reader:
                    key = row.get("record_key")
                    if key:
                        existing_keys.add(key)

        except Exception as exc:
            logging.warning(
                "Could not read history CSV: %s",
                exc,
            )

    new_rows = [
        row for row in rows
        if row["record_key"] not in existing_keys
    ]

    if not new_rows:
        return 0

    file_exists = HISTORY_FILE.exists()

    with HISTORY_FILE.open(
        "a",
        newline="",
        encoding="utf-8-sig",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=FIELDS,
            extrasaction="ignore",
        )

        if not file_exists:
            writer.writeheader()

        writer.writerows(new_rows)

    return len(new_rows)


# ============================================================
# MAIN
# ============================================================

async def main():
    started = datetime.now(timezone.utc)

    rows, debug = await scrape()

    if not rows:
        debug["result"] = "no_rows"
        DEBUG_FILE.write_text(
            json.dumps(debug, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        raise RuntimeError(
            "No rows parsed. Check OddsPortal page structure "
            "and data/oddsportal_cards_debug.html"
        )

    signals = build_signals(rows)

    write_csv(
        LATEST_FILE,
        rows,
        FIELDS,
    )

    write_csv(
        SIGNALS_FILE,
        signals,
        list(signals[0].keys()) if signals else [
            "scraped_at",
            "priority",
            "teams",
            "home_team",
            "away_team",
            "country",
            "league",
            "match_time",
            "qualifying_groups",
            "qualifying_group_count",
            "flow_targets",
            "qualifying_markets",
            "qualifying_selections",
            "match_url",
            "match_key",
        ],
    )

    history_added = append_history(rows)
    database_added = save_database(rows)

    finished = datetime.now(timezone.utc)

    debug.update({
        "finished_at": finished.isoformat(),
        "duration_seconds": round(
            (finished - started).total_seconds(),
            2,
        ),
        "history_new_rows": history_added,
        "database_new_records": database_added,
        "signals": len(signals),
        "signal_priorities": {
            "TOP": sum(
                1 for s in signals
                if s["priority"] == "TOP"
            ),
            "HIGH": sum(
                1 for s in signals
                if s["priority"] == "HIGH"
            ),
            "NORMAL": sum(
                1 for s in signals
                if s["priority"] == "NORMAL"
            ),
        },
        "result": "completed",
    })

    DEBUG_FILE.write_text(
        json.dumps(debug, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    logging.info("====================================")
    logging.info("OddsPortal parser completed")
    logging.info("Rows: %s", len(rows))
    logging.info("Qualifying rows: %s", debug["qualifying_rows"])
    logging.info("Signals: %s", len(signals))
    logging.info("History added: %s", history_added)
    logging.info("Database added: %s", database_added)
    logging.info("====================================")


if __name__ == "__main__":
    asyncio.run(main())
