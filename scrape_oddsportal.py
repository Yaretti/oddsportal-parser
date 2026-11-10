
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
GREENLINE ODDS — OddsPortal Dropping Odds Parser

ДОЗВОЛЕНІ РИНКИ:
- 1X2
- Over/Under (Total)

КРИТЕРІЇ ПЕРШОГО СИГНАЛУ:
1) Старий коефіцієнт 4.00–6.00 включно, новий <= 2.50.
2) Старий коефіцієнт 2.00–3.00 включно, новий <= 1.80.

ПОВТОРНИЙ СИГНАЛ:
- Додаткове падіння поточного коефіцієнта на 10% або більше
  від останнього успішно надісланого коефіцієнта.
- Окремий облік кожного ринку, вибору та лінії тоталу.
- Після успішної відправки новий коефіцієнт стає базовим.

TELEGRAM:
- Один об'єднаний допис на матч за один цикл.
- Без NORMAL.
- Без посилання на матч.
- Формат GREENLINE ODDS.

ВАЖЛИВО:
- Час OddsPortal інтерпретується як UTC. Це припущення,
  яке необхідно перевірити на відомому матчі.
- T-30 використовує найближчий збережений snapshot,
  а не незалежне оновлення сторінки матчу.
"""

import csv
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import time
import urllib.request
import urllib.error

from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

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
TIMEOUT_MS = 60000

HORIZON_DAYS = 7
REPEAT_DROP = 0.10

T30_MINUTES = 30
T30_TOLERANCE = 15

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

UTC = timezone.utc
KYIV = ZoneInfo("Europe/Kyiv")

DB_FILE = DATA_DIR / "oddsportal_history.sqlite"

LATEST_CSV = DATA_DIR / "oddsportal_latest.csv"
HISTORY_CSV = DATA_DIR / "oddsportal_history.csv"
SIGNALS_CSV = DATA_DIR / "oddsportal_signals.csv"
SNAPSHOTS_CSV = DATA_DIR / "oddsportal_snapshots.csv"
MOVEMENT_CSV = DATA_DIR / "oddsportal_movement.csv"
T30_CSV = DATA_DIR / "oddsportal_t30.csv"

DEBUG_JSON = DATA_DIR / "oddsportal_debug.json"
DEBUG_HTML = DATA_DIR / "oddsportal_cards_debug.html"

ALLOWED_MARKETS = {
    "1x2",
    "over-under",
    "ou",
    "total",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

log = logging.getLogger("GREENLINE_ODDS")


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
    "match_date",
    "match_time",
    "match_datetime",
    "market",
    "market_code",
    "market_group",
    "period",
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
    "match_date",
    "match_time",
    "market_count",
    "markets",
    "match_key",
]

SNAPSHOT_FIELDS = [
    "observed_at_utc",
    "match_key",
    "teams",
    "market_code",
    "market_group",
    "period",
    "selection",
    "handicap_line",
    "odds_old",
    "odds_new",
    "best_current_odds",
    "match_datetime",
]

MOVEMENT_FIELDS = [
    "calculated_at_utc",
    "match_key",
    "teams",
    "market_code",
    "period",
    "selection",
    "current_odds",
    "change_15m_pct",
    "change_30m_pct",
    "change_60m_pct",
]

T30_FIELDS = [
    "match_key",
    "teams",
    "kickoff_utc",
    "target_t30_utc",
    "snapshot_utc",
    "checkpoint_status",
    "market_code",
    "selection",
    "checkpoint_odds",
    "qualifies_at_t30",
]


# ============================================================
# GENERAL HELPERS
# ============================================================

def now_utc():
    return datetime.now(UTC)


def iso_utc(value=None):
    value = value or now_utc()
    return value.astimezone(UTC).isoformat(timespec="seconds")


def parse_datetime(value):
    if not value:
        return None

    try:
        result = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )

        if result.tzinfo is None:
            result = result.replace(tzinfo=UTC)

        return result.astimezone(UTC)

    except (ValueError, TypeError):
        return None


def clean(value):
    if value is None:
        return ""

    return re.sub(
        r"\s+",
        " ",
        str(value).replace("\xa0", " ")
    ).strip()


def to_float(value):
    try:
        if value is None:
            return None

        value = str(value).replace(",", ".").strip()

        if not value:
            return None

        return float(value)

    except (TypeError, ValueError):
        return None


def sha256(*parts):
    raw = "|".join(
        "" if item is None else str(item)
        for item in parts
    )

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()


def normalize_url(url):
    if not url:
        return ""

    parsed = urlsplit(url)

    return (
        parsed.scheme
        + "://"
        + parsed.netloc
        + parsed.path.rstrip("/")
    )


def make_match_key(teams, url):
    normalized = normalize_url(url)

    if normalized:
        return normalized.lower()

    return clean(teams).lower()


def make_series_key(row):
    """
    Окремий ключ для кожного:
    - матчу;
    - ринку;
    - періоду;
    - вибору;
    - лінії тоталу.
    """

    return sha256(
        row.get("match_key"),
        row.get("market_code"),
        row.get("period"),
        row.get("selection"),
        row.get("handicap_line"),
    )


# ============================================================
# ODDS CRITERIA
# ============================================================

def calculate_drop(old, new):
    if old is None or new is None or old <= 0:
        return None

    return ((new - old) / old) * 100


def qualifies_odds(old, new):
    if old is None or new is None:
        return False

    if 4.00 <= old <= 6.00 and new <= 2.50:
        return True

    if 2.00 <= old <= 3.00 and new <= 1.80:
        return True

    return False


def parse_drop(text):
    found = re.search(
        r"[-−]\s*(\d+(?:[.,]\d+)?)\s*%",
        text or ""
    )

    if not found:
        return None

    value = to_float(found.group(1))

    if value is None:
        return None

    return -abs(value)


# ============================================================
# MARKET PARSING
# ============================================================

def market_from_url(url):
    result = {
        "market_code": "",
        "market": "",
        "market_group": "",
        "selection_code": "",
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

    result["market_code"] = code

    if len(tokens) > 1:
        result["selection_code"] = tokens[1].strip()

    if code == "1x2":
        result["market"] = "1X2"
        result["market_group"] = "1X2"

    elif code in ("over-under", "ou", "total"):
        result["market"] = "Total"
        result["market_group"] = "total"

        if len(tokens) > 2:
            result["handicap_line"] = clean(tokens[2])

    return result


def normalize_selection(value):
    value = clean(value).lower()

    mapping = {
        "1": "1",
        "home": "1",
        "home team": "1",
        "x": "X",
        "draw": "X",
        "2": "2",
        "away": "2",
        "away team": "2",
        "over": "Over",
        "under": "Under",
    }

    return mapping.get(value, clean(value))


def detect_period(label, selection_code):
    label = (label or "").lower()

    if "1st half" in label:
        return "1H"

    if "2nd half" in label:
        return "2H"

    if str(selection_code) == "3":
        return "1H"

    if str(selection_code) == "4":
        return "2H"

    return "FT"


# ============================================================
# COUNTRY AND TIME
# ============================================================

COUNTRIES = {
    "england": ("England", "🇬🇧"),
    "scotland": ("Scotland", "🏴"),
    "wales": ("Wales", "🏴"),
    "spain": ("Spain", "🇪🇸"),
    "france": ("France", "🇫🇷"),
    "germany": ("Germany", "🇩🇪"),
    "italy": ("Italy", "🇮🇹"),
    "portugal": ("Portugal", "🇵🇹"),
    "netherlands": ("Netherlands", "🇳🇱"),
    "belgium": ("Belgium", "🇧🇪"),
    "brazil": ("Brazil", "🇧🇷"),
    "argentina": ("Argentina", "🇦🇷"),
    "ukraine": ("Ukraine", "🇺🇦"),
    "poland": ("Poland", "🇵🇱"),
    "turkey": ("Turkey", "🇹🇷"),
    "greece": ("Greece", "🇬🇷"),
    "usa": ("USA", "🇺🇸"),
    "united-states": ("USA", "🇺🇸"),
    "mexico": ("Mexico", "🇲🇽"),
    "japan": ("Japan", "🇯🇵"),
    "australia": ("Australia", "🇦🇺"),
}


MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def parse_country_league(links):
    for link in links:
        href = link.get("href", "")

        parts = urlsplit(href).path.strip("/").split("/")

        if len(parts) == 3 and parts[0] == "football":
            slug = parts[1]

            if slug in COUNTRIES:
                country = COUNTRIES[slug][0]
            else:
                country = slug.replace("-", " ").title()

            league = clean(link.get("text", ""))

            if not league:
                league = parts[2].replace("-", " ").title()

            return country, slug, league

    return "", "", ""


def parse_match_time(text, scraped_at):
    text = clean(text)

    relative = re.search(
        r"\b(Today|Tomorrow|Yesterday)\s+(\d{1,2}:\d{2})",
        text,
        re.I
    )

    base = parse_datetime(scraped_at)

    if not base:
        return "", "", ""

    if relative:
        word = relative.group(1).lower()
        clock = relative.group(2)

        days = {
            "today": 0,
            "tomorrow": 1,
            "yesterday": -1,
        }

        date_value = (
            base.date()
            + timedelta(days=days[word])
        )

    else:
        found = re.search(
            r"\b(\d{1,2})\s+([A-Za-z]+)\s+(\d{1,2}:\d{2})",
            text
        )

        if not found:
            clock = re.search(
                r"\b(\d{1,2}:\d{2})\b",
                text
            )

            if not clock:
                return "", "", ""

            return "", clock.group(1), ""

        day = int(found.group(1))
        month = MONTHS.get(found.group(2).lower())

        if not month:
            return "", "", ""

        clock = found.group(3)

        year = base.year

        try:
            date_value = datetime(
                year,
                month,
                day
            ).date()

        except ValueError:
            return "", "", ""

    try:
        naive = datetime.strptime(
            f"{date_value.isoformat()} {clock}",
            "%Y-%m-%d %H:%M"
        )

        # ПРИПУЩЕННЯ: час OddsPortal є UTC.
        kickoff_utc = naive.replace(tzinfo=UTC)

        kickoff_kyiv = kickoff_utc.astimezone(KYIV)

        return (
            kickoff_kyiv.date().isoformat(),
            kickoff_kyiv.strftime("%H:%M"),
            kickoff_utc.isoformat(timespec="seconds"),
        )

    except ValueError:
        return "", "", ""


# ============================================================
# ODDS PORTAL DOM EXTRACTION
# ============================================================

EXTRACT_JS = r"""
() => {
    const anchors = Array.from(
        document.querySelectorAll('a[href*="/football/h2h/"]')
    );

    const allowed = new Set([
        "1x2",
        "over-under",
        "ou",
        "total"
    ]);

    const clean = el => (
        ((el && (el.innerText || el.textContent)) || "")
        .replace(/\s+/g, " ")
        .trim()
    );

    const isOdd = value => {
        const n = Number(String(value).replace(",", "."));
        return Number.isFinite(n) && n >= 1.01 && n <= 1000;
    };

    const output = [];
    const seen = new Set();

    for (const anchor of anchors) {
        const href = anchor.href || "";
        const fragment = href.split("#")[1] || "";

        const market = fragment.includes(":")
            ? fragment.split(":").slice(1).join(":")
                .split(";")[0].toLowerCase()
            : "";

        if (!allowed.has(market)) continue;

        let container = null;
        let dropping = null;
        let odds = [];

        let node = anchor;

        for (let depth = 0; depth < 16 && node; depth++) {
            const text = clean(node);

            if (
                /Best Current Odds/i.test(text) &&
                /[-−]\s*\d+(?:[.,]\d+)?\s*%/.test(text)
            ) {
                const lis = Array.from(node.querySelectorAll("li"));

                const candidate = lis.find(li => {
                    const marker = li.querySelector(
                        'img[src*="dropping_rec"], ' +
                        'img[class*="dropping_rec"], ' +
                        '.dropping_rec'
                    );

                    const values = Array.from(
                        li.querySelectorAll("span")
                    ).map(clean).filter(isOdd);

                    return marker && values.length >= 2;
                });

                if (candidate) {
                    container = node;
                    dropping = candidate;

                    odds = Array.from(
                        candidate.querySelectorAll("span")
                    ).map(clean).filter(isOdd);

                    break;
                }
            }

            node = node.parentElement;
        }

        if (!container || !dropping || odds.length < 2) {
            continue;
        }

        const key = href + "|" + dropping.outerHTML;

        if (seen.has(key)) continue;

        seen.add(key);

        const lis = Array.from(container.querySelectorAll("li"));
        const next = lis[lis.indexOf(dropping) + 1];

        let best = "";

        if (next) {
            const values = Array.from(
                next.querySelectorAll("a, span")
            ).map(clean).filter(isOdd);

            if (values.length) {
                best = values[values.length - 1];
            }
        }

        const teamNames = Array.from(
            anchor.querySelectorAll("p")
        ).map(clean).filter(Boolean);

        const labelElement = container.querySelector(
            "div.truncate.pl-2"
        );

        let selection = "";

        for (const element of container.querySelectorAll("[title]")) {
            const value = element.getAttribute("title") || "";

            if (
                ["1", "X", "2", "Over", "Under",
                 "Home", "Away", "Draw"].includes(value)
            ) {
                selection = value;
                break;
            }
        }

        output.push({
            href: href,
            anchorText: anchor.innerText || "",
            text: container.innerText || "",
            html: container.outerHTML || "",
            links: Array.from(
                container.querySelectorAll("a")
            ).map(a => ({
                href: a.href || "",
                text: a.innerText || ""
            })),
            marketLabel: labelElement ? clean(labelElement) : "",
            selection: selection,
            oldOdds: odds[0],
            newOdds: odds[odds.length - 1],
            bestCurrentOdds: best,
            home: teamNames[0] || "",
            away: teamNames[1] || ""
        });
    }

    return {
        cards: output,
        match_links_found: anchors.length,
        unique_match_links: [...new Set(
            anchors.map(a => a.href)
        )].length
    };
}
"""


def parse_card(card, scraped_at):
    href = card.get("href", "")

    if not href:
        return None

    market = market_from_url(href)

    if market["market_code"] not in ALLOWED_MARKETS:
        return None

    old = to_float(card.get("oldOdds"))
    new = to_float(card.get("newOdds"))
    best = to_float(card.get("bestCurrentOdds"))

    if old is None or new is None:
        return None

    displayed_drop = parse_drop(card.get("text", ""))

    calculated_drop = calculate_drop(old, new)

    if displayed_drop is None or calculated_drop is None:
        return None

    if abs(displayed_drop - calculated_drop) > 2:
        return None

    selection = normalize_selection(card.get("selection"))

    if market["market_group"] == "1X2":
        if selection not in ("1", "X", "2"):
            return None

    if market["market_group"] == "total":
        if selection not in ("Over", "Under"):
            return None

    period = detect_period(
        market["market"],
        market["selection_code"]
    )

    country, country_slug, league = parse_country_league(
        card.get("links", [])
    )

    teams = ""

    home = clean(card.get("home"))
    away = clean(card.get("away"))

    if home and away:
        teams = f"{home} - {away}"
    else:
        text = clean(card.get("anchorText", ""))

        parts = re.split(
            r"\s+(?:-|–|—|vs\.?)\s+",
            text,
            maxsplit=1,
            flags=re.I
        )

        if len(parts) == 2:
            teams = f"{parts[0]} - {parts[1]}"

    date_text, time_text, kickoff = parse_match_time(
        card.get("anchorText", ""),
        scraped_at
    )

    # Якщо час не був знайдений в anchor, пробуємо текст картки.
    if not kickoff:
        date_text, time_text, kickoff = parse_match_time(
            card.get("text", ""),
            scraped_at
        )

    return {
        "scraped_at": scraped_at,
        "sport": "Football",
        "country": country,
        "country_slug": country_slug,
        "league": league,
        "teams": teams,
        "match_date": date_text,
        "match_time": time_text,
        "match_datetime": kickoff,
        "market": market["market"],
        "market_code": market["market_code"],
        "market_group": market["market_group"],
        "period": period,
        "selection": selection,
        "selection_code": market["selection_code"],
        "handicap_line": market["handicap_line"],
        "drop_pct": displayed_drop,
        "calculated_drop_pct": round(calculated_drop, 2),
        "odds_old": old,
        "odds_new": new,
        "best_current_odds": best,
        "qualifies": qualifies_odds(old, new),
        "match_url": href,
        "match_key": make_match_key(teams, href),
    }


# ============================================================
# DATABASE
# ============================================================

def initialize_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS odds_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            record_key TEXT UNIQUE,
            scraped_at TEXT,
            country TEXT,
            league TEXT,
            teams TEXT,
            match_date TEXT,
            match_time TEXT,
            match_datetime TEXT,
            market TEXT,
            market_code TEXT,
            market_group TEXT,
            period TEXT,
            selection TEXT,
            handicap_line TEXT,
            odds_old REAL,
            odds_new REAL,
            best_current_odds REAL,
            drop_pct REAL,
            qualifies INTEGER,
            match_url TEXT,
            match_key TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS odds_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at_utc TEXT,
            series_key TEXT,
            match_key TEXT,
            teams TEXT,
            match_datetime TEXT,
            market_code TEXT,
            market_group TEXT,
            period TEXT,
            selection TEXT,
            handicap_line TEXT,
            odds_old REAL,
            odds_new REAL,
            best_current_odds REAL,
            qualifies INTEGER,
            UNIQUE(observed_at_utc, series_key)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS telegram_signal_state (
            series_key TEXT PRIMARY KEY,
            match_key TEXT,
            last_sent_odds REAL NOT NULL,
            last_sent_at TEXT NOT NULL,
            market_code TEXT,
            market_group TEXT,
            period TEXT,
            selection TEXT,
            handicap_line TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS telegram_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT UNIQUE,
            match_key TEXT,
            payload TEXT,
            state_updates TEXT,
            created_at TEXT,
            sent_at TEXT,
            status TEXT DEFAULT 'PENDING',
            last_error TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS tracked_matches (
            match_key TEXT PRIMARY KEY,
            teams TEXT,
            kickoff_utc TEXT,
            t30_status TEXT DEFAULT 'PENDING'
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS final_odds_t30 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_key TEXT,
            series_key TEXT,
            teams TEXT,
            kickoff_utc TEXT,
            target_t30_utc TEXT,
            snapshot_utc TEXT,
            checkpoint_status TEXT,
            market_code TEXT,
            selection TEXT,
            checkpoint_odds REAL,
            qualifies_at_t30 INTEGER,
            UNIQUE(match_key, series_key)
        )
    """)

    conn.commit()


# ============================================================
# HISTORY
# ============================================================

def save_history(conn, rows):
    added = []

    for row in rows:
        if row["market_code"] not in ALLOWED_MARKETS:
            continue

        record_key = sha256(
            row["match_key"],
            row["market_code"],
            row["period"],
            row["selection"],
            row["handicap_line"],
            row["odds_old"],
            row["odds_new"],
        )

        cursor = conn.execute("""
            INSERT OR IGNORE INTO odds_history (
                record_key, scraped_at, country, league, teams,
                match_date, match_time, match_datetime, market,
                market_code, market_group, period, selection,
                handicap_line, odds_old, odds_new, best_current_odds,
                drop_pct, qualifies, match_url, match_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            record_key,
            row["scraped_at"],
            row["country"],
            row["league"],
            row["teams"],
            row["match_date"],
            row["match_time"],
            row["match_datetime"],
            row["market"],
            row["market_code"],
            row["market_group"],
            row["period"],
            row["selection"],
            row["handicap_line"],
            row["odds_old"],
            row["odds_new"],
            row["best_current_odds"],
            row["drop_pct"],
            int(row["qualifies"]),
            row["match_url"],
            row["match_key"],
        ))

        if cursor.rowcount:
            added.append(row)

    conn.commit()

    return added


# ============================================================
# SNAPSHOTS
# ============================================================

def save_snapshots(conn, rows, observed_at):
    for row in rows:
        kickoff = parse_datetime(row["match_datetime"])

        if not kickoff:
            continue

        if not (
            now_utc() <= kickoff <= now_utc() + timedelta(days=HORIZON_DAYS)
        ):
            continue

        conn.execute("""
            INSERT OR IGNORE INTO odds_snapshots (
                observed_at_utc, series_key, match_key, teams,
                match_datetime, market_code, market_group, period,
                selection, handicap_line, odds_old, odds_new,
                best_current_odds, qualifies
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            observed_at,
            make_series_key(row),
            row["match_key"],
            row["teams"],
            row["match_datetime"],
            row["market_code"],
            row["market_group"],
            row["period"],
            row["selection"],
            row["handicap_line"],
            row["odds_old"],
            row["odds_new"],
            row["best_current_odds"],
            int(row["qualifies"]),
        ))

        conn.execute("""
            INSERT INTO tracked_matches (
                match_key, teams, kickoff_utc, t30_status
            ) VALUES (?, ?, ?, 'PENDING')
            ON CONFLICT(match_key) DO UPDATE SET
                teams=excluded.teams,
                kickoff_utc=excluded.kickoff_utc
        """, (
            row["match_key"],
            row["teams"],
            row["match_datetime"],
        ))

    conn.commit()


# ============================================================
# MOVEMENT
# ============================================================

def get_snapshot(conn, series_key, target, tolerance=8):
    start = iso_utc(target - timedelta(minutes=tolerance))
    end = iso_utc(target + timedelta(minutes=tolerance))

    rows = conn.execute("""
        SELECT observed_at_utc, best_current_odds, odds_new
        FROM odds_snapshots
        WHERE series_key=?
          AND observed_at_utc BETWEEN ? AND ?
    """, (series_key, start, end)).fetchall()

    candidates = []

    for observed, best, new in rows:
        dt = parse_datetime(observed)
        odds = best if best is not None else new

        if dt and odds:
            candidates.append((
                abs((dt - target).total_seconds()),
                dt,
                odds
            ))

    if not candidates:
        return None

    _, dt, odds = min(candidates, key=lambda x: x[0])

    return dt, odds


def calculate_movement(conn, rows):
    output = []
    now = now_utc()

    for row in rows:
        current = (
            row["best_current_odds"]
            if row["best_current_odds"] is not None
            else row["odds_new"]
        )

        if current is None:
            continue

        snapshots = {}

        for minutes in (15, 30, 60):
            snapshots[minutes] = get_snapshot(
                conn,
                make_series_key(row),
                now - timedelta(minutes=minutes)
            )

        def change(snapshot):
            if not snapshot or snapshot[1] <= 0:
                return ""

            return round(
                ((current - snapshot[1]) / snapshot[1]) * 100,
                2
            )

        output.append({
            "calculated_at_utc": iso_utc(now),
            "match_key": row["match_key"],
            "teams": row["teams"],
            "market_code": row["market_code"],
            "period": row["period"],
            "selection": row["selection"],
            "current_odds": current,
            "change_15m_pct": change(snapshots[15]),
            "change_30m_pct": change(snapshots[30]),
            "change_60m_pct": change(snapshots[60]),
        })

    return output


# ============================================================
# T-30
# ============================================================

def process_t30(conn):
    now = now_utc()

    matches = conn.execute("""
        SELECT match_key, teams, kickoff_utc
        FROM tracked_matches
        WHERE t30_status='PENDING'
    """).fetchall()

    processed = 0

    for match_key, teams, kickoff_text in matches:
        kickoff = parse_datetime(kickoff_text)

        if not kickoff:
            continue

        target = kickoff - timedelta(minutes=T30_MINUTES)

        if now < target + timedelta(minutes=T30_TOLERANCE):
            continue

        series = conn.execute("""
            SELECT DISTINCT series_key
            FROM odds_snapshots
            WHERE match_key=?
        """, (match_key,)).fetchall()

        for (series_key,) in series:
            rows = conn.execute("""
                SELECT observed_at_utc, market_code, selection,
                       odds_old, best_current_odds, odds_new
                FROM odds_snapshots
                WHERE series_key=?
                  AND observed_at_utc BETWEEN ? AND ?
            """, (
                series_key,
                iso_utc(target - timedelta(minutes=T30_TOLERANCE)),
                iso_utc(target + timedelta(minutes=T30_TOLERANCE)),
            )).fetchall()

            if not rows:
                conn.execute("""
                    INSERT OR IGNORE INTO final_odds_t30 (
                        match_key, series_key, teams, kickoff_utc,
                        target_t30_utc, checkpoint_status
                    ) VALUES (?, ?, ?, ?, ?, 'UNAVAILABLE')
                """, (
                    match_key,
                    series_key,
                    teams,
                    iso_utc(kickoff),
                    iso_utc(target),
                ))

                continue

            chosen = min(
                rows,
                key=lambda r: abs(
                    (
                        parse_datetime(r[0]) - target
                    ).total_seconds()
                )
            )

            observed, code, selection, old, best, new = chosen

            current = best if best is not None else new

            qualifies = (
                int(qualifies_odds(old, current))
                if current is not None
                else None
            )

            conn.execute("""
                INSERT OR IGNORE INTO final_odds_t30 (
                    match_key, series_key, teams, kickoff_utc,
                    target_t30_utc, snapshot_utc,
                    checkpoint_status, market_code, selection,
                    checkpoint_odds, qualifies_at_t30
                ) VALUES (?, ?, ?, ?, ?, ?, 'AVAILABLE', ?, ?, ?, ?)
            """, (
                match_key,
                series_key,
                teams,
                iso_utc(kickoff),
                iso_utc(target),
                observed,
                code,
                selection,
                current,
                qualifies,
            ))

        conn.execute("""
            UPDATE tracked_matches
            SET t30_status='FINALIZED'
            WHERE match_key=?
        """, (match_key,))

        processed += 1

    conn.commit()

    return processed


# ============================================================
# TELEGRAM FORMAT
# ============================================================

def current_odds(row):
    value = row["best_current_odds"]

    if value is None:
        value = row["odds_new"]

    return value


def bet_name(row):
    selection = row["selection"]

    if row["market_group"] == "1X2":
        return {
            "1": "П1",
            "X": "Х",
            "2": "П2",
        }.get(selection, selection)

    line = clean(row["handicap_line"])

    if selection == "Over":
        return f"ТБ {line}".strip()

    return f"ТМ {line}".strip()


def market_name(row):
    return (
        "1X2"
        if row["market_group"] == "1X2"
        else "Тотал"
    )


def build_message(signal):
    country = signal["country"]
    slug = signal["country_slug"]

    flag = COUNTRIES.get(
        slug,
        ("", "🌍")
    )[1]

    competition = (
        f"{flag}{country}"
        if country
        else "🌍"
    )

    if signal["league"]:
        competition += f" • {signal['league']}"

    try:
        date_text = datetime.strptime(
            signal["match_date"],
            "%Y-%m-%d"
        ).strftime("%d.%m.%Y")
    except ValueError:
        date_text = signal["match_date"] or "Дата невідома"

    date_line = f"📅 {date_text}"

    if signal["match_time"]:
        date_line += f", 🕘 {signal['match_time']}"

    lines = [
        "❇️GREENLINE ODDS❇️",
        "━━━━━━━━━━━━━━━━━━",
        html.escape(competition),
        "🏟️" + html.escape(
            signal["teams"].replace(" - ", " — ")
        ),
        html.escape(date_line),
        "━━━━━━━━━━━━━━━━━━",
        "📊 АНАЛІЗ КОЕФІЦІЄНТІВ",
    ]

    for index, item in enumerate(signal["items"]):
        old = item["display_old"]
        new = item["current_odds"]
        drop = item["display_drop"]

        lines.extend([
            f"🎯 Ринок: {html.escape(market_name(item))}",
            f"📉 {old:.2f}  →  {new:.2f} ( {round(abs(drop))}%)",
            "━━━━━━━━━━━━━━━━━━",
            f"💹 Ставка: {html.escape(bet_name(item))}",
        ])

        if index < len(signal["items"]) - 1:
            lines.append("━━━━━━━━━━━━━━━━━━")

    lines.extend([
        "━━━━━━━━━━━━━━━━━━",
        "by yaretti",
    ])

    return "\n".join(lines)


# ============================================================
# SIGNAL DECISION
# ============================================================

def eligible_rows(rows):
    result = []

    for row in rows:
        if row["market_code"] not in ALLOWED_MARKETS:
            continue

        if row["market_group"] not in ("1X2", "total"):
            continue

        if row["period"] != "FT":
            continue

        if not row["qualifies"]:
            continue

        if current_odds(row) is None:
            continue

        result.append(row)

    return result


def prepare_signals(conn, rows, scraped_at):
    grouped = {}

    for row in eligible_rows(rows):
        series_key = make_series_key(row)
        current = current_odds(row)

        state = conn.execute("""
            SELECT last_sent_odds
            FROM telegram_signal_state
            WHERE series_key=?
        """, (series_key,)).fetchone()

        if state is None:
            # Перший сигнал для цієї серії.
            display_old = row["odds_old"]
            display_drop = calculate_drop(
                display_old,
                current
            )

        else:
            last_sent = float(state[0])

            # Повтор тільки після додаткового падіння >=10%.
            if current > last_sent * (1 - REPEAT_DROP) + 1e-9:
                continue

            display_old = last_sent
            display_drop = calculate_drop(
                last_sent,
                current
            )

        item = dict(row)

        item["series_key"] = series_key
        item["current_odds"] = float(current)
        item["display_old"] = float(display_old)
        item["display_drop"] = float(display_drop or 0)

        grouped.setdefault(
            row["match_key"],
            []
        ).append(item)

    signals = []

    for match_key, items in grouped.items():
        first = items[0]

        signals.append({
            "scraped_at": scraped_at,
            "match_key": match_key,
            "teams": first["teams"],
            "country": first["country"],
            "country_slug": first["country_slug"],
            "league": first["league"],
            "match_date": first["match_date"],
            "match_time": first["match_time"],
            "items": items,
        })

    return signals


# ============================================================
# TELEGRAM OUTBOX
# ============================================================

def enqueue_signals(conn, signals):
    queued = 0

    for signal in signals:
        items = signal["items"]

        event_key = sha256(
            signal["match_key"],
            *sorted(
                f"{item['series_key']}:{item['current_odds']:.4f}"
                for item in items
            )
        )

        state_updates = []

        for item in items:
            state_updates.append({
                "series_key": item["series_key"],
                "match_key": item["match_key"],
                "last_sent_odds": item["current_odds"],
                "market_code": item["market_code"],
                "market_group": item["market_group"],
                "period": item["period"],
                "selection": item["selection"],
                "handicap_line": item["handicap_line"],
            })

        payload = build_message(signal)

        cursor = conn.execute("""
            INSERT OR IGNORE INTO telegram_outbox (
                event_key, match_key, payload, state_updates,
                created_at, status
            ) VALUES (?, ?, ?, ?, ?, 'PENDING')
        """, (
            event_key,
            signal["match_key"],
            payload,
            json.dumps(
                state_updates,
                ensure_ascii=False
            ),
            iso_utc(),
        ))

        if cursor.rowcount:
            queued += 1

    conn.commit()

    return queued


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN або TELEGRAM_CHAT_ID не задано"
        )

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json"
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=30
        ) as response:
            result = json.loads(
                response.read().decode("utf-8")
            )

        if not result.get("ok"):
            raise RuntimeError(
                result.get("description", "Telegram API error")
            )

        return True

    except urllib.error.HTTPError as exc:
        body = exc.read().decode(
            "utf-8",
            errors="replace"
        )

        try:
            description = json.loads(body).get(
                "description",
                f"HTTP {exc.code}"
            )
        except Exception:
            description = f"HTTP {exc.code}"

        raise RuntimeError(description)

    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Telegram connection error: {exc.reason}"
        )


def process_outbox(conn, signals):
    queued = enqueue_signals(conn, signals)

    pending = conn.execute("""
        SELECT id, match_key, payload, state_updates
        FROM telegram_outbox
        WHERE status='PENDING'
        ORDER BY id
    """).fetchall()

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning(
            "Telegram не налаштовано. Повідомлень у черзі: %s",
            len(pending)
        )

        return {
            "queued": queued,
            "sent": 0,
            "failed": 0,
            "pending": len(pending),
        }

    sent = 0
    failed = 0

    for outbox_id, match_key, payload, updates_json in pending:
        try:
            send_telegram(payload)

            updates = json.loads(
                updates_json or "[]"
            )

            sent_at = iso_utc()

            # Базовий коефіцієнт оновлюється лише після
            # підтвердження успішної відправки Telegram.
            for item in updates:
                conn.execute("""
                    INSERT INTO telegram_signal_state (
                        series_key, match_key, last_sent_odds,
                        last_sent_at, market_code, market_group,
                        period, selection, handicap_line
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)

                    ON CONFLICT(series_key) DO UPDATE SET
                        last_sent_odds=excluded.last_sent_odds,
                        last_sent_at=excluded.last_sent_at
                """, (
                    item["series_key"],
                    item["match_key"],
                    item["last_sent_odds"],
                    sent_at,
                    item["market_code"],
                    item["market_group"],
                    item["period"],
                    item["selection"],
                    item["handicap_line"],
                ))

            conn.execute("""
                UPDATE telegram_outbox
                SET status='SENT',
                    sent_at=?,
                    last_error=NULL
                WHERE id=?
            """, (
                sent_at,
                outbox_id,
            ))

            conn.commit()

            sent += 1

            log.info(
                "Telegram signal sent: %s",
                match_key
            )

        except Exception as exc:
            conn.execute("""
                UPDATE telegram_outbox
                SET last_error=?
                WHERE id=?
            """, (
                str(exc)[:1000],
                outbox_id,
            ))

            conn.commit()

            failed += 1

            log.error(
                "Telegram error for %s: %s",
                match_key,
                exc
            )

    return {
        "queued": queued,
        "sent": sent,
        "failed": failed,
        "pending": conn.execute("""
            SELECT COUNT(*)
            FROM telegram_outbox
            WHERE status='PENDING'
        """).fetchone()[0],
    }


# ============================================================
# CSV
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

    exists = path.exists() and path.stat().st_size > 0

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
# EXPORTS
# ============================================================

def export_snapshots(conn):
    records = conn.execute("""
        SELECT observed_at_utc, match_key, teams,
               market_code, market_group, period,
               selection, handicap_line, odds_old,
               odds_new, best_current_odds, match_datetime
        FROM odds_snapshots
        ORDER BY observed_at_utc DESC
    """).fetchall()

    rows = [
        dict(zip(SNAPSHOT_FIELDS, record))
        for record in records
    ]

    write_csv(
        SNAPSHOTS_CSV,
        rows,
        SNAPSHOT_FIELDS
    )


def export_t30(conn):
    records = conn.execute("""
        SELECT match_key, teams, kickoff_utc,
               target_t30_utc, snapshot_utc,
               checkpoint_status, market_code,
               selection, checkpoint_odds,
               qualifies_at_t30
        FROM final_odds_t30
        ORDER BY kickoff_utc DESC
    """).fetchall()

    rows = [
        dict(zip(T30_FIELDS, record))
        for record in records
    ]

    write_csv(
        T30_CSV,
        rows,
        T30_FIELDS
    )


# ============================================================
# SCRAPER
# ============================================================

def scrape():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        context = browser.new_context(
            viewport={
                "width": 1440,
                "height": 1000,
            },
            locale="en-US",
            timezone_id="UTC",
        )

        page = context.new_page()

        page.set_default_timeout(TIMEOUT_MS)

        try:
            response = page.goto(
                SOURCE_URL,
                wait_until="domcontentloaded",
                timeout=TIMEOUT_MS,
            )

            status = (
                response.status
                if response
                else None
            )

            page.wait_for_timeout(6000)

            data = page.evaluate(EXTRACT_JS)

            cards = data.get("cards", [])

            fragments = []

            for index, card in enumerate(cards[:15]):
                fragments.append(
                    f"<!-- CARD {index + 1} -->\n"
                    + card.get("html", "")
                )

            DEBUG_HTML.write_text(
                "\n".join(fragments),
                encoding="utf-8",
            )

            return {
                "status": status,
                "title": page.title(),
                "cards": cards,
                "links_found": data.get(
                    "match_links_found",
                    0
                ),
                "unique_links": data.get(
                    "unique_match_links",
                    0
                ),
            }

        finally:
            context.close()
            browser.close()


# ============================================================
# MAIN
# ============================================================

def main():
    started = time.time()

    scraped_at = iso_utc()

    log.info("GREENLINE ODDS started")
    log.info("Source: %s", SOURCE_URL)
    log.info("Markets: 1X2 and Total")
    log.info("Horizon: %s days", HORIZON_DAYS)
    log.info("Repeat threshold: 10%%")

    result = scrape()

    rows = []

    for card in result["cards"]:
        try:
            row = parse_card(
                card,
                scraped_at
            )

            if row:
                rows.append(row)

        except Exception:
            log.exception("Card parsing error")

    # Видаляємо дублікати в межах одного циклу.
    unique = {}

    for row in rows:
        key = (
            row["match_key"],
            row["market_code"],
            row["period"],
            row["selection"],
            row["handicap_line"],
            row["odds_old"],
            row["odds_new"],
        )

        unique[key] = row

    rows = list(unique.values())

    write_csv(
        LATEST_CSV,
        rows,
        CSV_FIELDS
    )

    conn = sqlite3.connect(DB_FILE)

    try:
        initialize_db(conn)

        added = save_history(
            conn,
            rows
        )

        append_csv(
            HISTORY_CSV,
            added,
            CSV_FIELDS
        )

        save_snapshots(
            conn,
            rows,
            scraped_at
        )

        movement = calculate_movement(
            conn,
            rows
        )

        write_csv(
            MOVEMENT_CSV,
            movement,
            MOVEMENT_FIELDS
        )

        t30_count = process_t30(conn)

        export_snapshots(conn)
        export_t30(conn)

        signals = prepare_signals(
            conn,
            rows,
            scraped_at
        )

        signal_export = []

        for signal in signals:
            signal_export.append({
                "scraped_at": signal["scraped_at"],
                "teams": signal["teams"],
                "country": signal["country"],
                "league": signal["league"],
                "match_date": signal["match_date"],
                "match_time": signal["match_time"],
                "market_count": len(signal["items"]),
                "markets": " | ".join(
                    f'{x["market"]}: {x["selection"]} '
                    f'{x["display_old"]:.2f}->{x["current_odds"]:.2f}'
                    for x in signal["items"]
                ),
                "match_key": signal["match_key"],
            })

        write_csv(
            SIGNALS_CSV,
            signal_export,
            SIGNAL_FIELDS
        )

        telegram = process_outbox(
            conn,
            signals
        )

        state_count = conn.execute("""
            SELECT COUNT(*)
            FROM telegram_signal_state
        """).fetchone()[0]

        sent_count = conn.execute("""
            SELECT COUNT(*)
            FROM telegram_outbox
            WHERE status='SENT'
        """).fetchone()[0]

        pending_count = conn.execute("""
            SELECT COUNT(*)
            FROM telegram_outbox
            WHERE status='PENDING'
        """).fetchone()[0]

    finally:
        conn.close()

    market_counts = {}

    for row in rows:
        market = row["market_group"]

        market_counts[market] = (
            market_counts.get(market, 0) + 1
        )

    debug = {
        "scraped_at_utc": scraped_at,
        "scraped_at_kyiv": now_utc().astimezone(KYIV).isoformat(),
        "source": SOURCE_URL,
        "http_status": result["status"],
        "page_title": result["title"],
        "links_found": result["links_found"],
        "unique_links": result["unique_links"],
        "parsed_rows": len(rows),
        "qualifying_rows": sum(
            bool(row["qualifies"])
            for row in rows
        ),
        "signal_events": len(signals),
        "new_history_records": len(added),
        "market_counts": market_counts,
        "t30_processed": t30_count,
        "telegram": telegram,
        "telegram_sent_total": sent_count,
        "telegram_pending_total": pending_count,
        "signal_state_series": state_count,
        "allowed_markets": sorted(ALLOWED_MARKETS),
        "repeat_rule": (
            "current_odds <= last_successfully_sent_odds * 0.90"
        ),
        "criteria": [
            "old 4.00-6.00 inclusive, new <=2.50",
            "old 2.00-3.00 inclusive, new <=1.80",
        ],
        "timezone_assumption": (
            "OddsPortal displayed kickoff time is treated as UTC; "
            "this assumption requires verification."
        ),
        "t30_limitation": (
            "T-30 uses nearest stored snapshot within +/-15 minutes; "
            "it does not independently refresh match odds."
        ),
        "elapsed_seconds": round(
            time.time() - started,
            2
        ),
    }

    DEBUG_JSON.write_text(
        json.dumps(
            debug,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8",
    )

    log.info(
        "Cards=%s parsed=%s qualifying=%s signals=%s",
        len(result["cards"]),
        len(rows),
        debug["qualifying_rows"],
        len(signals),
    )

    log.info(
        "Markets=%s",
        market_counts
    )

    log.info(
        "T30=%s Telegram=%s",
        t30_count,
        telegram,
    )

    log.info(
        "Finished in %.2f sec",
        time.time() - started
    )


if __name__ == "__main__":
    main()
