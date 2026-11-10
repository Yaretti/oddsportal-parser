
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
GREENLINE / OddsPortal Dropping Odds Parser

Features:
- OddsPortal dropping odds extraction.
- Old/new/best odds validation against displayed drop percentage.
- Home/away team identification.
- Flow direction and flow_to_team columns.
- Separate 1X2, total, negative and positive handicap markets.
- Exact user qualification criteria.
- NORMAL / HIGH / TOP signal grouping.
- SQLite automatic migration without deleting old history.
- CSV latest, history and signals.
- JSON diagnostics.

Note:
Odds movement is not proof of actual betting money volume.
Unknown selections are not guessed.
"""

import asyncio
import csv
import hashlib
import json
import logging
import os
import re
import sqlite3

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
CARDS_DEBUG_FILE = DATA_DIR / "oddsportal_cards_debug.json"
DB_FILE = DATA_DIR / "oddsportal_history.sqlite"

PAGE_WAIT_MS = int(os.getenv("PAGE_WAIT_MS", "5000"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "5"))
DROP_TOLERANCE = float(
    os.getenv("DROP_VALIDATION_TOLERANCE", "2.5")
)

# If true, parser tries to click a visible Next button.
ENABLE_PAGINATION = (
    os.getenv("ENABLE_PAGINATION", "true").lower() == "true"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)


# ============================================================
# OUTPUT FIELDS
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

SIGNAL_FIELDS = [
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
]


# ============================================================
# GENERAL HELPERS
# ============================================================

def utc_now():
    return datetime.now(timezone.utc).isoformat()


def clean_text(value):
    return re.sub(
        r"\s+",
        " ",
        str(value or "")
    ).strip()


def to_number(value):
    if value is None:
        return None

    try:
        return float(
            str(value).replace(",", ".").strip()
        )
    except (ValueError, TypeError):
        return None


def make_hash(value):
    return hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()[:32]


def canonical_match_url(url):
    """
    Remove market fragment and query parameters.
    The remaining event URL identifies the match.
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
                return (
                    clean_text(parts[0]),
                    clean_text(parts[1]),
                )

    return value, ""


def parse_match_time_and_teams(value):
    """
    Handles labels such as:
    Tomorrow 01:30 Once Caldas - Atl. Nacional
    Today 19:45 Colchester - Ipswich U21
    18:00 Chambery - Grenoble
    """

    value = clean_text(value)

    patterns = [
        r"^(Today|Tomorrow)\s+(\d{1,2}:\d{2})\s+(.+)$",
        r"^(\d{1,2}:\d{2})\s+(.+)$",
    ]

    for pattern in patterns:
        match = re.match(pattern, value, re.I)

        if not match:
            continue

        if len(match.groups()) == 3:
            match_time = (
                f"{match.group(1)} {match.group(2)}"
            )
            teams = match.group(3)
        else:
            match_time = match.group(1)
            teams = match.group(2)

        return clean_text(match_time), clean_text(teams)

    return "", value


# ============================================================
# URL / MARKET PARSING
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

    if not url or "#" not in url:
        return result

    fragment = url.split("#", 1)[1]

    if ":" in fragment:
        fragment = fragment.split(":", 1)[1]

    parts = fragment.split(";")

    if not parts:
        return result

    market_code = parts[0].strip().lower()

    selection_code = (
        parts[1].strip()
        if len(parts) > 1 else ""
    )

    line = (
        to_number(parts[2])
        if len(parts) > 2 else None
    )

    result["market_code"] = market_code
    result["selection_code"] = selection_code

    if line is not None:
        result["handicap_line"] = f"{line:.2f}"

    if market_code == "1x2":
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


def market_display(market_code, handicap_line):
    line = to_number(handicap_line)

    if market_code == "1x2":
        return "1X2"

    if market_code == "over-under":
        return (
            f"O/U {line:g}"
            if line is not None
            else "Over/Under"
        )

    if market_code == "ah":
        if line is not None:
            sign = "+" if line > 0 else ""
            return f"AH {sign}{line:g}"
        return "Asian Handicap"

    if market_code == "eh":
        if line is not None:
            sign = "+" if line > 0 else ""
            return f"EH {sign}{line:g}"
        return "European Handicap"

    names = {
        "dnb": "DNB",
        "ht-ft": "HT/FT",
        "qualify": "To Qualify",
        "btts": "Both Teams To Score",
        "double-chance": "Double Chance",
    }

    return names.get(
        market_code,
        market_code or "Unknown"
    )


# ============================================================
# ODDS EXTRACTION AND VALIDATION
# ============================================================

def calculate_drop(old_odds, new_odds):
    if old_odds is None or old_odds <= 0:
        return None

    if new_odds is None:
        return None

    return (
        (new_odds - old_odds) / old_odds
    ) * 100.0


def extract_valid_odds(card_text):
    """
    Reads the displayed negative percentage and searches
    for a consecutive odds triplet matching that percentage.

    Example:
      old = 59.00
      new = 21.50
      calculated drop = -63.56%
      displayed drop = -64%
    """

    text = card_text or ""

    drop_match = re.search(
        r"(-\s*\d+(?:[.,]\d+)?)\s*%",
        text
    )

    if not drop_match:
        return None

    displayed_drop = to_number(
        drop_match.group(1).replace(" ", "")
    )

    if displayed_drop is None:
        return None

    remaining_text = text[drop_match.end():]

    values = re.findall(
        r"(?<![\w:])\d{1,5}(?:[.,]\d+)",
        remaining_text
    )

    values = [
        to_number(value)
        for value in values
    ]

    values = [
        value for value in values
        if value is not None and value > 0
    ]

    if len(values) < 3:
        return None

    for index in range(len(values) - 2):
        old_odds, new_odds, best_odds = (
            values[index:index + 3]
        )

        calculated = calculate_drop(
            old_odds,
            new_odds
        )

        if calculated is None:
            continue

        if abs(calculated - displayed_drop) <= DROP_TOLERANCE:
            return {
                "drop_pct": displayed_drop,
                "calculated_drop_pct": round(
                    calculated,
                    2
                ),
                "odds_old": old_odds,
                "odds_new": new_odds,
                "best_current_odds": best_odds,
            }

    return None


# ============================================================
# SELECTION PARSING
# ============================================================

def find_visible_selection(card_text, market_code):
    """
    Uses visible market selection text.
    Does not infer a selection from undocumented URL codes.
    """

    lines = [
        clean_text(line)
        for line in (card_text or "").splitlines()
        if clean_text(line)
    ]

    stop_index = len(lines)

    for index, line in enumerate(lines):
        if re.search(
            r"Best Current Odds",
            line,
            re.I
        ):
            stop_index = index
            break

    candidates = lines[:stop_index]

    if market_code == "over-under":
        for line in candidates:
            if line.lower() in ("over", "under"):
                return line.title()

        return ""

    if market_code == "1x2":
        for line in candidates:
            if line.upper() in ("1", "X", "2"):
                return line.upper()

            if line.lower() in (
                "home",
                "draw",
                "away",
            ):
                return line.title()

        return ""

    # For handicap and other markets, use an explicit
    # visible 1 / X / 2 selection only.
    if market_code in (
        "ah",
        "eh",
        "dnb",
        "qualify",
        "ht-ft",
    ):
        for line in candidates:
            if line.upper() in ("1", "X", "2"):
                return line.upper()

    return ""


# ============================================================
# FLOW TARGET
# ============================================================

def determine_flow(
    market_code,
    selection,
    home_team,
    away_team,
):
    """
    flow_to_team:
      Home team / Away team / Draw / Over / Under.

    If selection is unknown, do not guess.
    """

    selection = clean_text(selection)
    normalized = selection.lower()

    unknown = {
        "flow_direction": "Не визначено",
        "flow_to_team": "Не визначено",
        "flow_confidence": "low",
    }

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
            "flow_direction": "Тотал: вибір невідомий",
            "flow_to_team": "Не визначено",
            "flow_confidence": "low",
        }

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

    return unknown


# ============================================================
# USER QUALIFICATION CRITERIA
# ============================================================

def qualifies(old_odds, new_odds):
    """
    Rule 1:
      Initial odds 4.00–5.00 inclusive
      New odds <= 2.00

    Rule 2:
      Initial odds 2.00–3.00 inclusive
      New odds <= 1.80
    """

    if old_odds is None or new_odds is None:
        return False

    if 4.00 <= old_odds <= 5.00:
        return new_odds <= 2.00

    if 2.00 <= old_odds <= 3.00:
        return new_odds <= 1.80

    return False


# ============================================================
# CARD PARSING
# ============================================================

def parse_card(card):
    url = urljoin(
        SOURCE_URL,
        card.get("url", "")
    )

    match_time, teams = parse_match_time_and_teams(
        card.get("teams", "")
    )

    home_team, away_team = split_teams(teams)

    if not home_team or not away_team:
        return None, "teams_not_parsed"

    market_data = parse_market_fragment(url)

    market_code = market_data["market_code"]

    if not market_code:
        return None, "market_code_missing"

    odds_data = extract_valid_odds(
        card.get("text", "")
    )

    if not odds_data:
        return None, "odds_not_validated"

    selection = find_visible_selection(
        card.get("text", ""),
        market_code
    )

    flow = determine_flow(
        market_code,
        selection,
        home_team,
        away_team,
    )

    match_url = canonical_match_url(url)
    match_key = make_hash(match_url)

    handicap_line = market_data["handicap_line"]
    selection_code = market_data["selection_code"]

    market = market_display(
        market_code,
        handicap_line
    )

    # Include the odds state in the record key.
    # This allows a later odds movement to be stored
    # as a new history record for the same selection.
    identity = "|".join([
        match_key,
        market_code,
        selection_code,
        handicap_line,
        selection,
        str(odds_data["odds_old"]),
        str(odds_data["odds_new"]),
        str(odds_data["best_current_odds"]),
    ])

    record_key = make_hash(identity)

    row = {
        "scraped_at": utc_now(),

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
        "market_group": market_data["market_group"],

        "selection": selection or "Не визначено",
        "selection_code": selection_code,
        "handicap_line": handicap_line,

        "flow_direction": flow["flow_direction"],
        "flow_to_team": flow["flow_to_team"],
        "flow_confidence": flow["flow_confidence"],

        "drop_pct": odds_data["drop_pct"],
        "calculated_drop_pct": odds_data[
            "calculated_drop_pct"
        ],

        "odds_old": odds_data["odds_old"],
        "odds_new": odds_data["odds_new"],
        "best_current_odds": odds_data[
            "best_current_odds"
        ],

        "qualifies": qualifies(
            odds_data["odds_old"],
            odds_data["odds_new"],
        ),

        "match_url": match_url,
        "match_key": match_key,
        "record_key": record_key,
    }

    return row, None


# ============================================================
# PLAYWRIGHT CARD EXTRACTION
# ============================================================

async def extract_cards(page):
    """
    Finds match links and their closest market-card container.

    Country and league are read from the card's football
    breadcrumb links when available.
    """

    cards = await page.locator(
        'a[href*="/football/h2h/"]'
    ).evaluate_all("""
        anchors => {
            const output = [];
            const seen = new Set();

            for (const anchor of anchors) {
                const href = anchor.href || "";
                const label = (anchor.innerText || "").trim();

                if (!href || !label) continue;

                let node = anchor;
                let selected = null;

                for (let depth = 0; depth < 14 && node; depth++) {
                    const text = (node.innerText || "").trim();

                    const hasDrop =
                        /-\\s*\\d+(?:[.,]\\d+)?\\s*%/.test(text);

                    const hasBest =
                        /Best Current Odds/i.test(text);

                    if (
                        hasDrop &&
                        hasBest &&
                        text.length < 5000
                    ) {
                        selected = node;
                        break;
                    }

                    node = node.parentElement;
                }

                if (!selected) continue;

                const cardText = (
                    selected.innerText || ""
                ).trim();

                // A card URL includes a market fragment.
                const key = href.toLowerCase();

                if (seen.has(key)) continue;
                seen.add(key);

                let country = "";
                let countrySlug = "";
                let league = "";

                const breadcrumbLinks = [
                    ...selected.querySelectorAll(
                        'a[href*="/football/"]'
                    )
                ];

                const breadcrumbData = [];

                for (const link of breadcrumbLinks) {
                    const linkHref =
                        link.getAttribute("href") || "";

                    const linkText =
                        (link.innerText || "").trim();

                    if (
                        !linkText ||
                        linkHref.includes("/h2h/")
                    ) {
                        continue;
                    }

                    const parts = linkHref
                        .split("/")
                        .filter(Boolean);

                    if (
                        parts.length >= 3 &&
                        parts[0] === "football"
                    ) {
                        breadcrumbData.push({
                            slug: parts[1],
                            text: linkText
                        });
                    }
                }

                if (breadcrumbData.length >= 1) {
                    countrySlug = breadcrumbData[0].slug;
                    country = breadcrumbData[0].text;
                }

                if (breadcrumbData.length >= 2) {
                    league = breadcrumbData[
                        breadcrumbData.length - 1
                    ].text;
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

    return cards


async def click_next_page(page):
    """
    Click only a visible enabled button explicitly named Next.
    """

    candidates = [
        page.get_by_role(
            "button",
            name=re.compile(r"^Next$", re.I)
        ),
        page.get_by_role(
            "link",
            name=re.compile(r"^Next$", re.I)
        ),
    ]

    for locator in candidates:
        try:
            if await locator.count() == 0:
                continue

            button = locator.first

            if not await button.is_visible():
                continue

            if not await button.is_enabled():
                continue

            await button.click(timeout=5000)
            await page.wait_for_timeout(3000)

            return True

        except Exception:
            continue

    return False


# ============================================================
# SCRAPING
# ============================================================

async def scrape():
    rows = []
    failures = []
    debug_cards = []

    debug = {
        "started_at": utc_now(),
        "source_url": SOURCE_URL,
        "http_status": None,
        "final_url": "",
        "pages_scanned": 0,
        "cards_found": 0,
        "parsed_rows": 0,
        "parse_failures": 0,
        "duplicate_records": 0,
        "qualifying_rows": 0,
        "signals": 0,
        "market_group_counts": {},
        "flow_confidence_counts": {},
        "errors": [],
    }

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
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

        if response:
            debug["http_status"] = response.status

            if response.status >= 400:
                await browser.close()
                raise RuntimeError(
                    f"OddsPortal HTTP {response.status}"
                )

        debug["final_url"] = page.url

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
        seen_pages = set()

        for page_number in range(MAX_PAGES):
            if page.url in seen_pages:
                break

            seen_pages.add(page.url)

            debug["pages_scanned"] += 1

            cards = await extract_cards(page)

            logging.info(
                "Page %s: %s market cards",
                page_number + 1,
                len(cards),
            )

            debug["cards_found"] += len(cards)

            for card_number, card in enumerate(cards, start=1):
                debug_cards.append({
                    "page": page_number + 1,
                    "card": card_number,
                    "url": card.get("url", ""),
                    "teams": card.get("teams", ""),
                    "country": card.get("country", ""),
                    "league": card.get("league", ""),
                    "text": card.get("text", ""),
                })

                try:
                    row, error = parse_card(card)

                    if row is None:
                        debug["parse_failures"] += 1

                        failures.append({
                            "page": page_number + 1,
                            "card": card_number,
                            "teams": card.get("teams", ""),
                            "reason": error,
                        })

                        continue

                    record_key = row["record_key"]

                    if record_key in seen_records:
                        debug["duplicate_records"] += 1
                        continue

                    seen_records.add(record_key)
                    rows.append(row)

                except Exception as exc:
                    debug["parse_failures"] += 1

                    failures.append({
                        "page": page_number + 1,
                        "card": card_number,
                        "teams": card.get("teams", ""),
                        "reason": str(exc),
                    })

                    logging.exception(
                        "Card parsing failed"
                    )

            if not ENABLE_PAGINATION:
                break

            if page_number + 1 >= MAX_PAGES:
                break

            moved = await click_next_page(page)

            if not moved:
                break

        await browser.close()

    debug["parsed_rows"] = len(rows)
    debug["qualifying_rows"] = sum(
        1 for row in rows
        if row["qualifies"]
    )

    debug["parse_failure_samples"] = failures[:50]

    for row in rows:
        group = row["market_group"]

        debug["market_group_counts"][group] = (
            debug["market_group_counts"].get(group, 0) + 1
        )

        confidence = row["flow_confidence"]

        debug["flow_confidence_counts"][confidence] = (
            debug["flow_confidence_counts"].get(
                confidence,
                0
            ) + 1
        )

    CARDS_DEBUG_FILE.write_text(
        json.dumps(
            debug_cards,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return rows, debug


# ============================================================
# SIGNAL GROUPING
# ============================================================

def build_signals(rows):
    """
    NORMAL:
      One qualifying group.

    HIGH:
      Two qualifying groups.

    TOP:
      All three:
      1X2 + negative_handicap + total.

    Positive handicap does not count as negative handicap.
    """

    grouped = {}

    for row in rows:
        if not row["qualifies"]:
            continue

        match_key = row["match_key"]

        if match_key not in grouped:
            grouped[match_key] = {
                "match_key": match_key,
                "teams": row["teams"],
                "home_team": row["home_team"],
                "away_team": row["away_team"],
                "country": row["country"],
                "league": row["league"],
                "match_time": row["match_time"],
                "match_url": row["match_url"],
                "groups": set(),
                "rows": [],
            }

        grouped[match_key]["groups"].add(
            row["market_group"]
        )

        grouped[match_key]["rows"].append(row)

    target_groups = {
        "1X2",
        "negative_handicap",
        "total",
    }

    signals = []

    for item in grouped.values():
        relevant_groups = (
            item["groups"] & target_groups
        )

        group_count = len(relevant_groups)

        if group_count == 0:
            continue

        if target_groups.issubset(item["groups"]):
            priority = "TOP"
        elif group_count == 2:
            priority = "HIGH"
        else:
            priority = "NORMAL"

        flow_targets = sorted(set(
            row["flow_to_team"]
            for row in item["rows"]
            if row["flow_to_team"] != "Не визначено"
        ))

        signals.append({
            "scraped_at": utc_now(),
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
            "qualifying_group_count": group_count,
            "flow_targets": " | ".join(flow_targets),
            "qualifying_markets": " | ".join(
                row["market"]
                for row in item["rows"]
            ),
            "qualifying_selections": " | ".join(
                row["selection"]
                for row in item["rows"]
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
# SQLITE DATABASE AND SAFE MIGRATION
# ============================================================

def init_db():
    """
    Create the table if needed.
    Add missing columns to an existing database.
    Never delete existing records.
    """

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
            flow_confidence TEXT,
            odds_old REAL,
            odds_new REAL,
            best_current_odds REAL,
            drop_pct REAL,
            calculated_drop_pct REAL,
            qualifies INTEGER,
            raw_json TEXT
        )
    """)

    required_columns = {
        "record_key": "TEXT",
        "scraped_at": "TEXT",
        "match_key": "TEXT",
        "teams": "TEXT",
        "market_code": "TEXT",
        "market_group": "TEXT",
        "selection": "TEXT",
        "selection_code": "TEXT",
        "flow_to_team": "TEXT",
        "flow_direction": "TEXT",
        "flow_confidence": "TEXT",
        "odds_old": "REAL",
        "odds_new": "REAL",
        "best_current_odds": "REAL",
        "drop_pct": "REAL",
        "calculated_drop_pct": "REAL",
        "qualifies": "INTEGER",
        "raw_json": "TEXT",
    }

    existing_columns = {
        row[1]
        for row in connection.execute(
            "PRAGMA table_info(odds_history)"
        ).fetchall()
    }

    added_columns = []

    for column, column_type in required_columns.items():
        if column not in existing_columns:
            connection.execute(
                f"ALTER TABLE odds_history "
                f"ADD COLUMN {column} {column_type}"
            )
            added_columns.append(column)

    connection.commit()

    logging.info(
        "SQLite schema checked. Added columns: %s",
        added_columns if added_columns else "none"
    )

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
                flow_confidence,
                odds_old,
                odds_new,
                best_current_odds,
                drop_pct,
                calculated_drop_pct,
                qualifies,
                raw_json
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?
            )
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
            row["flow_confidence"],
            row["odds_old"],
            row["odds_new"],
            row["best_current_odds"],
            row["drop_pct"],
            row["calculated_drop_pct"],
            int(row["qualifies"]),
            json.dumps(
                row,
                ensure_ascii=False
            ),
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
    """
    Append only unseen record_key values.
    Old history columns are preserved.
    New columns are added to the CSV header when the file
    is rewritten through a temporary file.
    """

    existing_rows = []
    existing_keys = set()

    if HISTORY_FILE.exists():
        try:
            with HISTORY_FILE.open(
                "r",
                newline="",
                encoding="utf-8-sig",
            ) as file:
                reader = csv.DictReader(file)

                for old_row in reader:
                    existing_rows.append(old_row)

                    key = old_row.get("record_key")

                    if key:
                        existing_keys.add(key)

        except Exception as exc:
            logging.warning(
                "Could not read existing history CSV: %s",
                exc,
            )

    new_rows = [
        row for row in rows
        if row["record_key"] not in existing_keys
    ]

    if not new_rows and not existing_rows:
        return 0

    if new_rows:
        merged_rows = existing_rows + new_rows

        temp_file = HISTORY_FILE.with_suffix(".tmp")

        with temp_file.open(
            "w",
            newline="",
            encoding="utf-8-sig",
        ) as file:
            writer = csv.DictWriter(
                file,
                fieldnames=FIELDS,
                extrasaction="ignore",
            )

            writer.writeheader()
            writer.writerows(merged_rows)

        temp_file.replace(HISTORY_FILE)

    return len(new_rows)


# ============================================================
# MAIN
# ============================================================

async def main():
    started = datetime.now(timezone.utc)

    rows, debug = await scrape()

    if not rows:
        debug["result"] = "no_rows"
        debug["finished_at"] = utc_now()

        DEBUG_FILE.write_text(
            json.dumps(
                debug,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        raise RuntimeError(
            "No valid rows parsed. "
            "Check data/oddsportal_cards_debug.json"
        )

    signals = build_signals(rows)

    # Current snapshot.
    write_csv(
        LATEST_FILE,
        rows,
        FIELDS,
    )

    # Signals.
    write_csv(
        SIGNALS_FILE,
        signals,
        SIGNAL_FIELDS,
    )

    # Historical CSV and SQLite.
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
                1 for signal in signals
                if signal["priority"] == "TOP"
            ),
            "HIGH": sum(
                1 for signal in signals
                if signal["priority"] == "HIGH"
            ),
            "NORMAL": sum(
                1 for signal in signals
                if signal["priority"] == "NORMAL"
            ),
        },
        "result": "completed",
    })

    DEBUG_FILE.write_text(
        json.dumps(
            debug,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    logging.info("------------------------------------")
    logging.info("OddsPortal parser completed")
    logging.info("Pages scanned: %s", debug["pages_scanned"])
    logging.info("Cards found: %s", debug["cards_found"])
    logging.info("Rows parsed: %s", debug["parsed_rows"])
    logging.info(
        "Qualifying rows: %s",
        debug["qualifying_rows"]
    )
    logging.info("Signals: %s", debug["signals"])
    logging.info("History added: %s", history_added)
    logging.info("Database added: %s", database_added)
    logging.info(
        "Market groups: %s",
        debug["market_group_counts"]
    )
    logging.info(
        "Flow confidence: %s",
        debug["flow_confidence_counts"]
    )
    logging.info("------------------------------------")


if __name__ == "__main__":
    asyncio.run(main())
