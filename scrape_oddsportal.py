
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OddsPortal Dropping Odds parser.

Reads:
- match
- league
- market and selection
- drop percentage
- old odds
- new odds
- best current odds

Signals:
A) old odds 4.00-5.00 -> new odds <= 2.00
B) old odds 2.00-3.00 -> new odds <= 1.80

Priority by qualifying market groups:
1 group  = NORMAL
2 groups = HIGH
3 groups = TOP

TOP requires:
1X2 + negative handicap + total goals.

Important:
Odds movement is not proof of money flow or betting volume.
"""

import os
import re
import csv
import json
import time
import sqlite3
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse, unquote

from playwright.sync_api import sync_playwright


# ---------------------------------------------------------
# CONFIG
# ---------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DEBUG_DIR = DATA_DIR / "debug"

DATA_DIR.mkdir(exist_ok=True)
DEBUG_DIR.mkdir(exist_ok=True)

URL = os.getenv(
    "ODDSPORTAL_URL",
    "https://www.oddsportal.com/dropping-odds/2/2/0/overall/"
)

HEADLESS = True
TIMEOUT = 60000

LATEST_CSV = DATA_DIR / "oddsportal_latest.csv"
HISTORY_CSV = DATA_DIR / "oddsportal_history.csv"
SIGNALS_CSV = DATA_DIR / "oddsportal_signals.csv"
DATABASE = DATA_DIR / "oddsportal_history.sqlite"
DEBUG_JSON = DATA_DIR / "oddsportal_debug.json"

CSV_FIELDS = [
    "scraped_at",
    "sport",
    "league",
    "country",
    "home_team",
    "away_team",
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
    "criterion",
    "match_url",
    "market_url",
]

SIGNAL_FIELDS = [
    "scraped_at",
    "sport",
    "league",
    "teams",
    "match_time",
    "priority",
    "qualifying_markets",
    "market_count",
    "has_1x2",
    "has_negative_handicap",
    "has_total",
    "all_three_markets",
    "details",
    "match_url",
]


# ---------------------------------------------------------
# HELPERS
# ---------------------------------------------------------

def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_text(value):
    if not value:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def parse_number(value):
    if value is None:
        return None

    value = str(value).strip().replace(",", ".")

    try:
        number = float(value)
        if number <= 0 or number > 10000:
            return None
        return number
    except (ValueError, TypeError):
        return None


def extract_odds(text):
    """
    Extract decimal odds from text.
    Only values with a decimal point are accepted.
    """

    found = re.findall(r"(?<![\d.])\d{1,4}\.\d{1,3}(?!\d)", text or "")

    result = []
    for item in found:
        number = parse_number(item)
        if number is not None:
            result.append(number)

    return result


def write_csv(path, rows, fields, append=False):
    if not rows:
        return

    mode = "a" if append else "w"
    file_exists = path.exists() and path.stat().st_size > 0

    with open(path, mode, newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=fields,
            extrasaction="ignore"
        )

        if not append or not file_exists:
            writer.writeheader()

        writer.writerows(rows)


def safe_filename(value):
    value = re.sub(r"[^a-zA-Z0-9_-]+", "_", value)
    return value[:100]


# ---------------------------------------------------------
# MARKET PARSING
# ---------------------------------------------------------

def get_market_fragment(url):
    """
    Example fragments:
    #abc:1X2;2
    #abc:over-under;2;6.50;0
    #abc:ah;2;0.75;0
    #abc:eh;2;-1.00;0
    """

    if "#" not in url:
        return []

    fragment = unquote(url.split("#", 1)[1])

    if ":" in fragment:
        fragment = fragment.split(":", 1)[1]

    return [x.strip() for x in fragment.split(";") if x.strip()]


def classify_market(url, card_text):
    parts = get_market_fragment(url)
    code = parts[0].lower() if parts else ""

    text = (card_text or "").lower()

    market = "OTHER"
    group = "other"
    handicap_line = None

    if code in ("1x2",):
        market = "1X2"
        group = "1x2"

    elif code in ("over-under", "total", "totals"):
        market = "TOTAL"
        group = "total"

    elif code in ("ah", "asian-handicap", "eh", "european-handicap"):
        market = "HANDICAP"

        if len(parts) >= 3:
            handicap_line = parse_number(parts[2])

        if handicap_line is not None and handicap_line < 0:
            group = "negative_handicap"
        else:
            group = "positive_or_zero_handicap"

    elif code == "dnb":
        market = "DNB"
        group = "other"

    elif code == "ht-ft":
        market = "HT/FT"
        group = "other"

    elif code in ("qualify", "to-qualify"):
        market = "TO QUALIFY"
        group = "other"

    else:
        if "1x2" in text:
            market = "1X2"
            group = "1x2"
        elif "o/u" in text or "over/under" in text:
            market = "TOTAL"
            group = "total"
        elif "asian handicap" in text or re.search(r"\bah\b", text):
            market = "HANDICAP"
            group = "other"

    # Market display name from the card.
    market_line = ""

    for line in (card_text or "").splitlines():
        line = clean_text(line)
        if not line:
            continue

        upper = line.upper()

        if (
            upper == "1X2"
            or upper.startswith("O/U")
            or upper.startswith("AH ")
            or upper.startswith("EH ")
            or upper in ("DNB", "HT/FT")
            or "1ST HALF" in upper
            or "2ND HALF" in upper
        ):
            market_line = line
            break

    if market_line:
        market = market_line

    # Handicap line from URL has priority.
    if group == "negative_handicap" and handicap_line is not None:
        handicap_line = float(handicap_line)

    return {
        "market": market,
        "market_group": group,
        "handicap_line": handicap_line,
        "fragment": parts,
    }


def extract_selection(card_text, market_info):
    lines = [
        clean_text(x)
        for x in (card_text or "").splitlines()
        if clean_text(x)
    ]

    market = market_info["market"]

    try:
        index = lines.index(market)
        if index + 1 < len(lines):
            candidate = lines[index + 1]

            if candidate.lower() not in (
                "best current odds",
                "football",
                "basketball",
                "tennis",
            ):
                return candidate
    except ValueError:
        pass

    # Fallback: URL fragment outcome index.
    parts = market_info.get("fragment", [])

    if len(parts) >= 2:
        return parts[1]

    return ""


# ---------------------------------------------------------
# MATCH AND LEAGUE PARSING
# ---------------------------------------------------------

def extract_match(anchor_text):
    lines = [
        clean_text(x)
        for x in (anchor_text or "").splitlines()
        if clean_text(x)
    ]

    ignored = {
        "-", "–", "—",
    }

    filtered = []

    for line in lines:
        if line in ignored:
            continue

        if re.fullmatch(r"\d{1,2}:\d{2}", line):
            continue

        if line.lower() in (
            "today",
            "tomorrow",
            "yesterday",
        ):
            continue

        if re.fullmatch(r"\d{1,2}\s+[A-Za-z]{3}", line):
            continue

        filtered.append(line)

    if len(filtered) >= 2:
        return filtered[-2], filtered[-1]

    # Inline fallback.
    match = re.search(r"(.+?)\s+[-–—]\s+(.+)", clean_text(anchor_text))

    if match:
        return match.group(1).strip(), match.group(2).strip()

    return "", ""


def extract_match_time(anchor_text):
    text = clean_text(anchor_text)

    date_match = re.search(
        r"(Today|Tomorrow|Yesterday|\d{1,2}\s+[A-Za-z]{3})",
        text,
        re.IGNORECASE
    )

    time_match = re.search(r"\b\d{1,2}:\d{2}\b", text)

    date_part = date_match.group(1) if date_match else ""
    time_part = time_match.group(0) if time_match else ""

    return clean_text(f"{date_part} {time_part}")


def extract_league(links):
    """
    Uses football breadcrumb links inside the match card.
    """

    candidates = []

    for item in links or []:
        href = item.get("href", "")
        label = clean_text(item.get("text", ""))

        if (
            "/football/" in href
            and "/h2h/" not in href
            and label
            and label.lower() not in ("football", "home")
        ):
            candidates.append({
                "text": label,
                "href": href,
            })

    if not candidates:
        return "", ""

    # The last breadcrumb is normally the competition.
    league = candidates[-1]["text"]

    country = ""
    if len(candidates) >= 2:
        country = candidates[-2]["text"]

    return league, country


# ---------------------------------------------------------
# CRITERIA
# ---------------------------------------------------------

def check_criteria(old_odds, new_odds):
    if old_odds is None or new_odds is None:
        return False, ""

    # Criterion A:
    # Opening odds 4.00-5.00 -> 2.00 or lower.
    if 4.00 <= old_odds <= 5.00 and new_odds <= 2.00:
        return True, "4.00-5.00_to_2.00_or_lower"

    # Criterion B:
    # Opening odds 2.00-3.00 -> 1.80 or lower.
    if 2.00 <= old_odds <= 3.00 and new_odds <= 1.80:
        return True, "2.00-3.00_to_1.80_or_lower"

    return False, ""


# ---------------------------------------------------------
# DATABASE
# ---------------------------------------------------------

def init_database():
    conn = sqlite3.connect(DATABASE)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS odds_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            record_key TEXT UNIQUE,
            scraped_at TEXT,
            sport TEXT,
            league TEXT,
            country TEXT,
            home_team TEXT,
            away_team TEXT,
            teams TEXT,
            match_time TEXT,
            market TEXT,
            market_group TEXT,
            selection TEXT,
            handicap_line REAL,
            drop_pct REAL,
            odds_old REAL,
            odds_new REAL,
            best_current_odds REAL,
            qualifies INTEGER,
            criterion TEXT,
            match_url TEXT,
            market_url TEXT
        )
    """)

    conn.commit()
    return conn


def save_history(conn, rows):
    inserted = []

    for row in rows:
        key_source = "|".join([
            row.get("match_url", ""),
            row.get("market", ""),
            row.get("selection", ""),
            str(row.get("odds_old", "")),
            str(row.get("odds_new", "")),
            str(row.get("drop_pct", "")),
        ])

        record_key = hashlib.sha256(
            key_source.encode("utf-8")
        ).hexdigest()

        values = (
            record_key,
            row.get("scraped_at"),
            row.get("sport"),
            row.get("league"),
            row.get("country"),
            row.get("home_team"),
            row.get("away_team"),
            row.get("teams"),
            row.get("match_time"),
            row.get("market"),
            row.get("market_group"),
            row.get("selection"),
            row.get("handicap_line"),
            row.get("drop_pct"),
            row.get("odds_old"),
            row.get("odds_new"),
            row.get("best_current_odds"),
            int(bool(row.get("qualifies"))),
            row.get("criterion"),
            row.get("match_url"),
            row.get("market_url"),
        )

        cursor = conn.execute("""
            INSERT OR IGNORE INTO odds_history (
                record_key, scraped_at, sport, league, country,
                home_team, away_team, teams, match_time,
                market, market_group, selection, handicap_line,
                drop_pct, odds_old, odds_new, best_current_odds,
                qualifies, criterion, match_url, market_url
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, values)

        if cursor.rowcount > 0:
            inserted.append(row)

    conn.commit()
    return inserted


# ---------------------------------------------------------
# HTML CARD EXTRACTION
# ---------------------------------------------------------

def collect_cards(page):
    """
    Finds match anchors and collects their ancestor elements.

    This avoids the previous bug where an absolute URL was
    incorrectly used to locate a relative href in the DOM.
    """

    return page.locator(
        'a[href*="/football/h2h/"]'
    ).evaluate_all("""
        anchors => anchors.map(anchor => {
            const ancestors = [];
            let node = anchor;

            for (let depth = 0; depth < 9 && node; depth++) {
                node = node.parentElement;
                if (!node) break;

                const links = Array.from(node.querySelectorAll("a"))
                    .map(a => ({
                        text: (a.innerText || a.textContent || "").trim(),
                        href: a.href || ""
                    }));

                ancestors.push({
                    depth: depth + 1,
                    text: node.innerText || node.textContent || "",
                    html: node.outerHTML || "",
                    links: links
                });
            }

            return {
                href: anchor.href || "",
                text: anchor.innerText || anchor.textContent || "",
                ancestors: ancestors
            };
        })
    """)


def choose_card(item):
    """
    Selects the smallest ancestor that contains:
    - the match anchor
    - a drop percentage
    - at least three decimal odds
    - Best Current Odds label
    """

    candidates = []

    for ancestor in item.get("ancestors", []):
        text = ancestor.get("text", "")
        flat = clean_text(text)

        has_drop = bool(re.search(r"-\s*\d{1,3}\s*%", flat))
        has_best = "Best Current Odds" in flat
        odds = extract_odds(flat)

        if has_drop and has_best and len(odds) >= 3:
            candidates.append(ancestor)

    if candidates:
        return min(
            candidates,
            key=lambda x: len(x.get("text", ""))
        )

    # Fallback: use the ancestor with the most useful information.
    for ancestor in item.get("ancestors", []):
        text = ancestor.get("text", "")
        if (
            re.search(r"-\s*\d{1,3}\s*%", text)
            and len(extract_odds(text)) >= 3
        ):
            return ancestor

    return None


def parse_card(item, scraped_at):
    match_url = item.get("href", "")
    anchor_text = item.get("text", "")

    card = choose_card(item)

    if not card:
        return None, {
            "url": match_url,
            "reason": "No suitable ancestor found",
            "anchor_text": anchor_text,
        }

    card_text = card.get("text", "")
    flat = clean_text(card_text)

    # The page presents:
    # drop percentage, old odds, new odds, best current odds.
    drop_match = re.search(
        r"-\s*(\d{1,3}(?:\.\d+)?)\s*%",
        flat
    )

    odds = extract_odds(flat)

    if not drop_match or len(odds) < 3:
        return None, {
            "url": match_url,
            "reason": "Could not extract drop and three odds",
            "text": flat[:3000],
            "odds_found": odds,
        }

    drop_pct = float(drop_match.group(1))

    odds_old = odds[0]
    odds_new = odds[1]
    best_current_odds = odds[2]

    home, away = extract_match(anchor_text)
    match_time = extract_match_time(anchor_text)

    league, country = extract_league(
        card.get("links", [])
    )

    market_info = classify_market(
        match_url,
        card_text
    )

    selection = extract_selection(
        card_text,
        market_info
    )

    qualifies, criterion = check_criteria(
        odds_old,
        odds_new
    )

    # This parser is for football only.
    # Non-football rows are not collected because the selector
    # specifically targets /football/h2h/ URLs.

    row = {
        "scraped_at": scraped_at,
        "sport": "Football",
        "league": league,
        "country": country,
        "home_team": home,
        "away_team": away,
        "teams": f"{home} - {away}".strip(" -"),
        "match_time": match_time,
        "market": market_info["market"],
        "market_group": market_info["market_group"],
        "selection": selection,
        "handicap_line": market_info["handicap_line"],
        "drop_pct": drop_pct,
        "odds_old": odds_old,
        "odds_new": odds_new,
        "best_current_odds": best_current_odds,
        "qualifies": qualifies,
        "criterion": criterion,
        "match_url": match_url.split("#")[0],
        "market_url": match_url,
    }

    debug = {
        "url": match_url,
        "anchor_text": anchor_text,
        "selected_card_text": card_text[:5000],
        "selected_card_html": card.get("html", "")[:50000],
        "odds_found": odds,
        "drop_pct": drop_pct,
        "parsed": row,
    }

    return row, debug


# ---------------------------------------------------------
# SIGNAL GROUPING
# ---------------------------------------------------------

def build_signals(rows):
    grouped = {}

    for row in rows:
        if not row.get("qualifies"):
            continue

        match_key = row.get("match_url", "")

        if not match_key:
            continue

        grouped.setdefault(match_key, []).append(row)

    signals = []

    for match_url, match_rows in grouped.items():
        groups = {
            row.get("market_group")
            for row in match_rows
        }

        has_1x2 = "1x2" in groups
        has_handicap = "negative_handicap" in groups
        has_total = "total" in groups

        core_groups = sum([
            has_1x2,
            has_handicap,
            has_total,
        ])

        # Count unique qualifying market groups.
        market_count = len(groups)

        if core_groups == 3:
            priority = "TOP"
        elif market_count >= 2:
            priority = "HIGH"
        else:
            priority = "NORMAL"

        first = match_rows[0]

        details = []

        for row in match_rows:
            details.append(
                f"{row['market']} / {row['selection']}: "
                f"{row['odds_old']} -> {row['odds_new']} "
                f"({row['drop_pct']}%)"
            )

        signals.append({
            "scraped_at": first["scraped_at"],
            "sport": first["sport"],
            "league": first["league"],
            "teams": first["teams"],
            "match_time": first["match_time"],
            "priority": priority,
            "qualifying_markets": ", ".join(sorted(groups)),
            "market_count": market_count,
            "has_1x2": has_1x2,
            "has_negative_handicap": has_handicap,
            "has_total": has_total,
            "all_three_markets": core_groups == 3,
            "details": " | ".join(details),
            "match_url": match_url,
        })

    priority_order = {
        "TOP": 0,
        "HIGH": 1,
        "NORMAL": 2,
    }

    signals.sort(
        key=lambda x: (
            priority_order.get(x["priority"], 9),
            x["league"],
            x["teams"],
        )
    )

    return signals


# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------

def main():
    started = time.time()
    scraped_at = now_utc()

    debug = {
        "started_at": scraped_at,
        "url": URL,
        "cards_found": 0,
        "parsed_rows": 0,
        "qualifying_rows": 0,
        "signals": 0,
        "errors": [],
        "card_debug": [],
    }

    rows = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ]
        )

        context = browser.new_context(
            viewport={"width": 1440, "height": 1100},
            locale="en-US",
            timezone_id="Europe/Kyiv",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )

        page = context.new_page()
        page.set_default_timeout(TIMEOUT)

        try:
            response = page.goto(
                URL,
                wait_until="domcontentloaded",
                timeout=TIMEOUT
            )

            debug["http_status"] = (
                response.status if response else None
            )

            page.wait_for_timeout(4000)

            debug["page_title"] = page.title()
            debug["final_url"] = page.url

            # Save the full page HTML for future inspection.
            full_html = page.content()

            (DEBUG_DIR / "oddsportal_page.html").write_text(
                full_html,
                encoding="utf-8"
            )

            debug["html_length"] = len(full_html)

            cards = collect_cards(page)
            debug["cards_found"] = len(cards)

            for index, item in enumerate(cards):
                try:
                    row, card_debug = parse_card(
                        item,
                        scraped_at
                    )

                    # Save first 10 card HTML samples.
                    if index < 10 and card_debug:
                        debug["card_debug"].append(
                            card_debug
                        )

                        html = card_debug.get(
                            "selected_card_html",
                            ""
                        )

                        if html:
                            filename = (
                                f"card_{index + 1}_"
                                f"{safe_filename(str(index))}.html"
                            )

                            (
                                DEBUG_DIR / filename
                            ).write_text(
                                html,
                                encoding="utf-8"
                            )

                    if row:
                        rows.append(row)

                except Exception as exc:
                    debug["errors"].append(
                        f"Card {index}: {type(exc).__name__}: {exc}"
                    )

        except Exception as exc:
            debug["errors"].append(
                f"Page error: {type(exc).__name__}: {exc}"
            )

        finally:
            browser.close()

    # Save latest complete snapshot.
    write_csv(
        LATEST_CSV,
        rows,
        CSV_FIELDS,
        append=False
    )

    # Save only qualifying rows to the signals file.
    qualifying_rows = [
        row for row in rows
        if row.get("qualifies")
    ]

    conn = init_database()

    try:
        new_history_rows = save_history(
            conn,
            rows
        )
    finally:
        conn.close()

    # Append only genuinely new database records.
    write_csv(
        HISTORY_CSV,
        new_history_rows,
        CSV_FIELDS,
        append=True
    )

    signals = build_signals(rows)

    write_csv(
        SIGNALS_CSV,
        signals,
        SIGNAL_FIELDS,
        append=False
    )

    finished = time.time()

    debug.update({
        "parsed_rows": len(rows),
        "qualifying_rows": len(qualifying_rows),
        "signals": len(signals),
        "new_database_records": len(new_history_rows),
        "finished_at": now_utc(),
        "duration_seconds": round(finished - started, 2),
        "result": "completed",
        "files": {
            "latest": str(LATEST_CSV),
            "history": str(HISTORY_CSV),
            "signals": str(SIGNALS_CSV),
            "database": str(DATABASE),
            "page_html": str(DEBUG_DIR / "oddsportal_page.html"),
        },
    })

    DEBUG_JSON.write_text(
        json.dumps(
            debug,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    print(json.dumps(
        {
            "result": debug["result"],
            "cards_found": debug["cards_found"],
            "parsed_rows": debug["parsed_rows"],
            "qualifying_rows": debug["qualifying_rows"],
            "signals": debug["signals"],
            "new_database_records": debug["new_database_records"],
            "errors": debug["errors"],
        },
        ensure_ascii=False,
        indent=2
    ))


if __name__ == "__main__":
    main()
