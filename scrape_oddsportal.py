
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
GREENLINE / OddsPortal Dropping Odds Parser

РИНКИ:
- 1X2
- Over/Under (Total)

КРИТЕРІЇ:
1) Початковий коефіцієнт 4.00–6.00 включно -> новий <= 2.50.
2) Початковий коефіцієнт 2.00–3.00 включно -> новий <= 1.80.

СИГНАЛИ:
- достатньо одного критерію;
- один NORMAL-сигнал на матч;
- тільки FT;
- 1X2 і Total не створюють два сигнали на один матч.

МОНІТОРИНГ:
- запускається workflow кожні 15 хвилин;
- зберігає часові знімки коефіцієнтів;
- горизонт матчів: 7 днів;
- розраховує зміну за 15, 30 і 60 хвилин;
- T-30: найближчий знімок у межах +/-15 хвилин від цільового часу;
- якщо знімка немає — UNAVAILABLE.

ЧАС:
- внутрішнє зберігання UTC;
- відображення Europe/Kyiv.

ВАЖЛИВО:
- T-30 використовує тільки знімки, отримані з Dropping Odds.
- Це не незалежне оновлення коефіцієнтів зі сторінки матчу.
- Наявність матчів на 7 днів залежить від того, що показує джерело.
"""

import csv
import hashlib
import json
import logging
import os
import re
import sqlite3
import time

from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
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
TIMEOUT_MS = int(os.getenv("ODDSPORTAL_TIMEOUT_MS", "60000"))

HORIZON_DAYS = 7
SNAPSHOT_INTERVAL_MINUTES = 15
T30_TARGET_MINUTES = 30
T30_TOLERANCE_MINUTES = 15

DROP_TOLERANCE_PP = 2.0

UTC = timezone.utc
KYIV = ZoneInfo("Europe/Kyiv")

LATEST_CSV = DATA_DIR / "oddsportal_latest.csv"
HISTORY_CSV = DATA_DIR / "oddsportal_history.csv"
SIGNALS_CSV = DATA_DIR / "oddsportal_signals.csv"
SNAPSHOTS_CSV = DATA_DIR / "oddsportal_snapshots.csv"
MOVEMENT_CSV = DATA_DIR / "oddsportal_movement.csv"
T30_CSV = DATA_DIR / "oddsportal_t30.csv"

DATABASE_FILE = DATA_DIR / "oddsportal_history.sqlite"

DEBUG_JSON = DATA_DIR / "oddsportal_debug.json"
DEBUG_HTML = DATA_DIR / "oddsportal_cards_debug.html"


# ============================================================
# MARKETS / SIGNAL RULES
# ============================================================

ALLOWED_MARKET_CODES = {
    "1x2",
    "over-under",
    "ou",
    "total",
}

TARGET_SIGNAL_GROUPS = {"1X2", "total"}
SIGNAL_PERIODS = {"FT"}


# ============================================================
# LOGGING
# ============================================================

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
    "match_date",
    "match_time",
    "match_datetime",
    "market",
    "market_code",
    "market_group",
    "period",
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
    "scraped_at", "teams", "country", "country_slug", "league",
    "match_date", "match_time", "match_datetime",
    "market_count", "market_groups", "priority", "markets",
    "match_url", "match_key",
]

SNAPSHOT_FIELDS = [
    "observed_at_utc", "observed_at_kyiv",
    "match_key", "teams", "country", "league",
    "match_datetime_utc", "match_datetime_kyiv",
    "market_code", "market_group", "period",
    "selection", "handicap_line",
    "odds_old", "odds_new", "best_current_odds",
    "drop_pct", "qualifies", "match_url",
]

MOVEMENT_FIELDS = [
    "calculated_at_utc", "match_key", "teams",
    "market_code", "period", "selection",
    "reference_odds", "current_odds",
    "change_15m_pct", "change_30m_pct", "change_60m_pct",
    "snapshot_15m_utc", "snapshot_30m_utc", "snapshot_60m_utc",
]

T30_FIELDS = [
    "match_key", "teams", "country", "league",
    "kickoff_utc", "kickoff_kyiv",
    "target_t30_utc", "target_t30_kyiv",
    "snapshot_utc", "snapshot_kyiv",
    "actual_checkpoint", "checkpoint_status",
    "market_code", "market_group", "period", "selection",
    "odds_old", "checkpoint_odds", "qualifies_at_t30",
    "match_url",
]


# ============================================================
# GENERAL HELPERS
# ============================================================

def utc_now_dt():
    return datetime.now(UTC)


def utc_now():
    return utc_now_dt().isoformat(timespec="seconds")


def parse_iso(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except (ValueError, TypeError):
        return None


def iso_utc(value):
    if not value:
        return ""
    return value.astimezone(UTC).isoformat(timespec="seconds")


def iso_kyiv(value):
    if not value:
        return ""
    return value.astimezone(KYIV).isoformat(timespec="seconds")


def clean_text(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).replace("\xa0", " ")).strip()


def safe_float(value):
    try:
        if value is None:
            return None
        return float(str(value).replace(",", ".").strip())
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
        parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""
    ))


def make_match_key(teams, match_url):
    normalized_url = normalize_match_url(match_url)
    if normalized_url:
        return normalized_url.lower()
    return normalize_team_name(teams)


def make_series_key(row):
    raw = "|".join(str(row.get(k, "")) for k in (
        "match_key", "market_code", "period",
        "selection", "handicap_line"
    ))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def make_record_key(row):
    raw = "|".join(str(row.get(k, "")) for k in (
        "match_key", "market_group", "market_code", "market",
        "period", "selection", "selection_code", "handicap_line",
        "odds_old", "odds_new", "drop_pct"
    ))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ============================================================
# MARKET FILTER
# ============================================================

def get_market_code_from_url(url):
    if not url or "#" not in url:
        return ""
    fragment = url.split("#", 1)[1]
    if ":" not in fragment:
        return ""
    tokens = fragment.split(":", 1)[1].split(";")
    return tokens[0].strip().lower() if tokens else ""


def is_allowed_market(url):
    return get_market_code_from_url(url) in ALLOWED_MARKET_CODES


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
    scope_code = tokens[1].strip() if len(tokens) > 1 else ""

    result["market_code"] = code
    result["selection_code"] = scope_code

    if code == "1x2":
        result.update({"market": "1X2", "market_group": "1X2"})

    elif code in ("over-under", "ou", "total"):
        result.update({"market": "Total", "market_group": "total"})
        if len(tokens) > 2:
            result["handicap_line"] = tokens[2]

    return result


# ============================================================
# ODDS / QUALIFICATION
# ============================================================

def calculate_drop(old_odds, new_odds):
    if old_odds is None or new_odds is None or old_odds <= 0:
        return None
    return ((new_odds - old_odds) / old_odds) * 100


def validate_drop(old_odds, new_odds, displayed_drop):
    calculated = calculate_drop(old_odds, new_odds)
    if calculated is None or displayed_drop is None:
        return False, calculated
    return abs(calculated - displayed_drop) <= DROP_TOLERANCE_PP, calculated


def qualifies_odds(old_odds, new_odds):
    if old_odds is None or new_odds is None:
        return False

    if 4.00 <= old_odds <= 6.00:
        return new_odds <= 2.50

    if 2.00 <= old_odds <= 3.00:
        return new_odds <= 1.80

    return False


def parse_drop_percentage(text):
    matches = re.findall(
        r"[-−]\s*(\d{1,3}(?:[.,]\d+)?)\s*%",
        text or ""
    )
    if not matches:
        return None
    value = safe_float(matches[0])
    return -abs(value) if value is not None else None


def parse_best_odds_from_text(text):
    found = re.search(
        r"Best Current Odds\D{0,20}(\d{1,4}[.,]\d{1,3})",
        text or "", re.IGNORECASE
    )
    if found:
        return safe_float(found.group(1))

    found = re.search(
        r"(\d{1,4}[.,]\d{1,3})\D{0,20}Best Current Odds",
        text or "", re.IGNORECASE
    )
    return safe_float(found.group(1)) if found else None


# ============================================================
# MARKET / PERIOD / SELECTION
# ============================================================

def detect_market_label(text, fallback):
    for pattern in (r"O/U\s*\d+(?:[.,]\d+)?", r"Over\s*/\s*Under", r"1X2"):
        found = re.search(pattern, text or "", re.IGNORECASE)
        if found:
            return clean_text(found.group(0))
    return fallback


def detect_period(label, scope_code):
    low = (label or "").lower()
    if "1st half" in low:
        return "1H"
    if "2nd half" in low:
        return "2H"

    # Коди scope 3/4 у вихідному парсері не були підтверджені.
    # Залишаємо попередню логіку сумісності.
    return {"3": "1H", "4": "2H"}.get(str(scope_code), "FT")


def normalize_selection(value):
    value = clean_text(value)
    aliases = {
        "home": "Home", "home team": "Home",
        "away": "Away", "away team": "Away",
        "draw": "X", "over": "Over", "under": "Under",
        "1": "1", "x": "X", "2": "2",
    }
    return aliases.get(value.lower(), value)


def detect_flow_direction(selection, market_group, home, away):
    selection = normalize_selection(selection)
    result = {
        "flow_to_team": "Невизначено",
        "flow_direction": "Невизначено",
        "flow_confidence": "low",
    }

    if selection in ("1", "Home") and home:
        result.update({
            "flow_to_team": home,
            "flow_direction": "На господарів",
            "flow_confidence": "high",
        })
    elif selection in ("2", "Away") and away:
        result.update({
            "flow_to_team": away,
            "flow_direction": "На гостей",
            "flow_confidence": "high",
        })
    elif selection == "X" and market_group == "1X2":
        result.update({
            "flow_to_team": "Нічия",
            "flow_direction": "На нічию",
            "flow_confidence": "high",
        })
    elif market_group == "total" and selection == "Over":
        result.update({
            "flow_to_team": "Не застосовується",
            "flow_direction": "Тотал більше",
            "flow_confidence": "high",
        })
    elif market_group == "total" and selection == "Under":
        result.update({
            "flow_to_team": "Не застосовується",
            "flow_direction": "Тотал менше",
            "flow_confidence": "high",
        })

    return result


# ============================================================
# COUNTRY / LEAGUE
# ============================================================

COUNTRY_NAMES = {
    "england": "England", "scotland": "Scotland",
    "wales": "Wales", "northern-ireland": "Northern Ireland",
    "colombia": "Colombia", "spain": "Spain",
    "france": "France", "germany": "Germany",
    "italy": "Italy", "portugal": "Portugal",
    "brazil": "Brazil", "argentina": "Argentina",
    "netherlands": "Netherlands", "belgium": "Belgium",
    "turkey": "Turkey", "greece": "Greece",
    "ukraine": "Ukraine", "poland": "Poland",
    "czech-republic": "Czech Republic", "croatia": "Croatia",
    "serbia": "Serbia", "romania": "Romania",
    "bulgaria": "Bulgaria", "denmark": "Denmark",
    "sweden": "Sweden", "norway": "Norway",
    "finland": "Finland", "usa": "USA",
    "united-states": "USA", "mexico": "Mexico",
    "japan": "Japan", "south-korea": "South Korea",
    "china": "China", "australia": "Australia",
    "india": "India", "international": "International",
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

        parts = urlsplit(href).path.lower().strip("/").split("/")
        if len(parts) != 3 or parts[0] != "football":
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

    chosen = max(candidates, key=lambda item: len(item["path"]))
    return (
        slug_to_title(chosen["country_slug"]),
        chosen["country_slug"],
        chosen["league"],
    )


# ============================================================
# MATCH TIME / TEAMS
# ============================================================

TIME_PATTERNS = [
    r"\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{1,2}:\d{2}\b",
    r"\b(?:Today|Tomorrow|Yesterday)\s+\d{1,2}:\d{2}\b",
    r"\b\d{1,2}:\d{2}\b",
]


def parse_match_time_and_teams(anchor_text, card_text=""):
    anchor_text = str(anchor_text or "")
    card_text = str(card_text or "")

    # Спочатку шукаємо повну дату, потім відносну, і лише потім час.
    match_time = ""
    for pattern in TIME_PATTERNS:
        for source in (anchor_text, card_text):
            found = re.search(pattern, source, re.IGNORECASE)
            if found:
                match_time = clean_text(found.group(0))
                break
        if match_time:
            break

    lines = [clean_text(x) for x in anchor_text.splitlines()]
    lines = [x for x in lines if x]
    cleaned = []

    for line in lines:
        line = re.sub(
            r"\b(?:Today|Tomorrow|Yesterday)\b",
            "", line, flags=re.IGNORECASE
        )
        line = re.sub(
            r"\b\d{1,2}\s+[A-Za-z]{3,9}\b",
            "", line, flags=re.IGNORECASE
        )
        line = re.sub(r"\b\d{1,2}:\d{2}\b", "", line)
        line = clean_text(line)
        if line and not re.fullmatch(r"[-–—]+", line):
            cleaned.append(line)

    for line in cleaned:
        parts = re.split(
            r"\s+(?:-|–|—|vs\.?)\s+",
            line, maxsplit=1, flags=re.IGNORECASE
        )
        if len(parts) == 2 and all(clean_text(x) for x in parts):
            return f"{clean_text(parts[0])} - {clean_text(parts[1])}", match_time

    if len(cleaned) >= 2:
        return f"{cleaned[-2]} - {cleaned[-1]}", match_time
    if len(cleaned) == 1:
        return cleaned[0], match_time
    return "", match_time


# ============================================================
# MATCH DATETIME
# ============================================================

def parse_match_datetime(match_time, scraped_at):
    """
    Час на OddsPortal у цьому парсері трактується як UTC.
    Далі UTC конвертується у Europe/Kyiv з урахуванням DST.

    Це відповідає перевіреному користувачем прикладу:
    19:45 на джерелі -> 21:45 у Києві 10.11.2026.
    Якщо OddsPortal змінить часову зону відображення,
    потрібно буде змінити SOURCE_TIMEZONE.
    """
    if not match_time:
        return "", "", ""

    value = clean_text(match_time)
    base_dt = parse_iso(scraped_at)
    if not base_dt:
        return "", "", ""

    base_date = base_dt.date()

    relative = re.search(
        r"\b(Today|Tomorrow|Yesterday)\s+(\d{1,2}:\d{2})\b",
        value, re.IGNORECASE
    )

    if relative:
        word = relative.group(1).lower()
        clock = relative.group(2)

        if word == "today":
            date_value = base_date
        elif word == "tomorrow":
            date_value = base_date + timedelta(days=1)
        else:
            date_value = base_date - timedelta(days=1)

    else:
        found = re.search(
            r"\b(\d{1,2})\s+"
            r"(January|February|March|April|May|June|July|August|"
            r"September|October|November|December|Jan|Feb|Mar|Apr|"
            r"Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
            r"\s+(\d{1,2}:\d{2})\b",
            value, re.IGNORECASE
        )

        if not found:
            clock_only = re.search(r"\b(\d{1,2}:\d{2})\b", value)
            if clock_only:
                return "", clock_only.group(1), ""
            return "", "", ""

        day = int(found.group(1))
        month_text = found.group(2)
        clock = found.group(3)

        month_number = None
        for fmt in ("%B", "%b"):
            try:
                month_number = datetime.strptime(month_text, fmt).month
                break
            except ValueError:
                pass

        if month_number is None:
            return "", clock, ""

        try:
            date_value = datetime(
                base_date.year, month_number, day
            ).date()
        except ValueError:
            return "", clock, ""

        difference = (date_value - base_date).days
        if difference < -180:
            date_value = date_value.replace(year=date_value.year + 1)
        elif difference > 180:
            date_value = date_value.replace(year=date_value.year - 1)

    try:
        naive = datetime.strptime(
            f"{date_value.isoformat()} {clock}",
            "%Y-%m-%d %H:%M"
        )
        # Джерельний час — UTC; не додаємо фіксовані +2 години.
        match_utc = naive.replace(tzinfo=UTC)
        match_kyiv = match_utc.astimezone(KYIV)

        return (
            match_kyiv.date().isoformat(),
            match_kyiv.strftime("%H:%M"),
            match_utc.isoformat(timespec="seconds"),
        )
    except (ValueError, TypeError):
        return "", "", ""


# ============================================================
# DOM EXTRACTION
# ============================================================

EXTRACT_CARDS_JS = r"""
() => {
  const anchors = Array.from(
    document.querySelectorAll('a[href*="/football/h2h/"]')
  );
  const output = [];
  const seenCards = new Set();

  const allowedMarkets = new Set([
    "1x2", "over-under", "ou", "total"
  ]);

  const txt = el => (
    el && (el.innerText || el.textContent) || ""
  ).replace(/\s+/g, " ").trim();

  const isDecimal = value => {
    const n = Number(String(value).replace(",", "."));
    return Number.isFinite(n) && n >= 1.01 && n <= 1000;
  };

  const selectionAllowed = new Set([
    "1", "X", "2", "Over", "Under", "Home", "Away", "Draw"
  ]);

  const emptyRow = (href, anchor) => ({
    href,
    anchorText: anchor.innerText || "",
    text: "",
    html: "",
    links: [],
    oddsCount: 0,
    marketLabel: "",
    selection: "",
    oldOdds: "",
    newOdds: "",
    bestCurrentOdds: "",
    home: "",
    away: ""
  });

  for (const anchor of anchors) {
    const href = anchor.href || anchor.getAttribute("href") || "";
    if (!href) continue;

    const fragment = href.split("#")[1] || "";
    const marketPart = fragment.includes(":")
      ? fragment.split(":").slice(1).join(":").split(";")[0].toLowerCase()
      : "";

    if (!allowedMarkets.has(marketPart)) continue;

    let card = null;
    let movementLi = null;
    let movementSpans = [];
    let node = anchor;

    for (let depth = 0; depth < 16 && node; depth++, node = node.parentElement) {
      const text = txt(node);
      if (!/Best Current Odds/i.test(text)) continue;
      if (!/[-−]\s*\d+(?:[.,]\d+)?\s*%/.test(text)) continue;

      const lis = Array.from(node.querySelectorAll("li"));
      const candidate = lis.find(li => {
        const marker = li.querySelector(
          'img[src*="dropping_rec"], img[class*="dropping_rec"], .dropping_rec'
        );
        if (!marker) return false;
        const spans = Array.from(li.querySelectorAll("span"))
          .map(txt).filter(isDecimal);
        return spans.length >= 2;
      });

      if (candidate) {
        card = node;
        movementLi = candidate;
        movementSpans = Array.from(
          candidate.querySelectorAll("span")
        ).map(txt).filter(isDecimal);
        break;
      }
    }

    if (!card || !movementLi || movementSpans.length < 2) {
      output.push(emptyRow(href, anchor));
      continue;
    }

    // Не використовуємо Set по всьому контейнеру:
    // різні ринки/вибори можуть мати спільного предка.
    const uniqueCardKey = href + "|" + movementLi.outerHTML;
    if (seenCards.has(uniqueCardKey)) continue;
    seenCards.add(uniqueCardKey);

    const oldOdds = movementSpans[0];
    const newOdds = movementSpans[movementSpans.length - 1];

    let bestOdds = "";
    const lis = Array.from(card.querySelectorAll("li"));
    const movementIndex = lis.indexOf(movementLi);
    const nextLi = movementIndex >= 0 ? lis[movementIndex + 1] : null;

    if (nextLi) {
      const nextNumbers = Array.from(
        nextLi.querySelectorAll("a, span")
      ).map(txt).filter(isDecimal);
      if (nextNumbers.length) {
        bestOdds = nextNumbers[nextNumbers.length - 1];
      }
    }

    const teamNames = Array.from(
      anchor.querySelectorAll("p")
    ).map(txt).filter(Boolean);

    const home = teamNames[0] || "";
    const away = teamNames[1] || "";

    let marketLabel = "";
    const labelEl = card.querySelector("div.truncate.pl-2");
    if (labelEl) marketLabel = txt(labelEl);

    let selection = "";
    for (const el of Array.from(card.querySelectorAll("[title]"))) {
      const value = (el.getAttribute("title") || "").trim();
      if (!value || /Best Current Odds/i.test(value)) continue;

      const normalized = value.charAt(0).toUpperCase()
        + value.slice(1).toLowerCase();

      if (
        selectionAllowed.has(value) ||
        selectionAllowed.has(value.toUpperCase()) ||
        selectionAllowed.has(normalized)
      ) {
        selection = value;
        break;
      }
    }

    const links = Array.from(card.querySelectorAll("a")).map(a => ({
      href: a.href || a.getAttribute("href") || "",
      text: a.innerText || ""
    }));

    output.push({
      href,
      anchorText: anchor.innerText || "",
      text: card.innerText || "",
      html: card.outerHTML || "",
      links,
      oddsCount: movementSpans.length,
      marketLabel,
      selection,
      oldOdds,
      newOdds,
      bestCurrentOdds: bestOdds,
      home,
      away
    });
  }

  const all = anchors.map(a => a.href || a.getAttribute("href") || "");
  const pagination = Array.from(document.querySelectorAll("a[href]"))
    .filter(a =>
      /next|more|load more/i.test((a.innerText || "").trim()) ||
      /page=\d+/i.test(a.href || "")
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
    if not is_allowed_market(href):
        return None, "unsupported_market"

    text = card.get("text", "")
    anchor_text = card.get("anchorText", "")

    if not href or not text:
        return None, "card_container_or_exact_odds_not_found"

    displayed_drop = parse_drop_percentage(text)
    if displayed_drop is None:
        return None, "drop_percentage_not_found"

    old_odds = safe_float(card.get("oldOdds"))
    new_odds = safe_float(card.get("newOdds"))
    best_odds = safe_float(card.get("bestCurrentOdds"))

    if best_odds is None:
        best_odds = parse_best_odds_from_text(text)

    if old_odds is None or new_odds is None:
        return None, "exact_old_new_odds_not_found"

    valid_drop, calculated_drop = validate_drop(
        old_odds, new_odds, displayed_drop
    )
    if not valid_drop:
        return None, "displayed_and_calculated_drop_mismatch"

    market_data = parse_market_from_url(href)
    if market_data["market_code"] not in ALLOWED_MARKET_CODES:
        return None, "unsupported_market"

    visible_market = clean_text(card.get("marketLabel", ""))
    if visible_market:
        market_data["market"] = visible_market
    else:
        market_data["market"] = detect_market_label(
            text, market_data["market"]
        )

    market_data["selection"] = normalize_selection(
        card.get("selection", "")
    )

    if market_data["market_group"] == "1X2":
        if market_data["selection"] not in ("1", "X", "2"):
            return None, "invalid_1x2_selection"

    elif market_data["market_group"] == "total":
        if market_data["selection"] not in ("Over", "Under"):
            return None, "invalid_total_selection"

    period = detect_period(
        market_data["market"], market_data["selection_code"]
    )

    country, country_slug, league = extract_country_league(
        card.get("links", [])
    )

    fallback_teams, raw_match_time = parse_match_time_and_teams(
        anchor_text, text
    )

    match_date, match_time, match_datetime = parse_match_datetime(
        raw_match_time, scraped_at
    )

    home = clean_text(card.get("home", ""))
    away = clean_text(card.get("away", ""))

    if home and away:
        teams = f"{home} - {away}"
    else:
        teams = fallback_teams
        sides = teams.split(" - ", 1) if " - " in teams else []
        home = sides[0] if len(sides) == 2 else ""
        away = sides[1] if len(sides) == 2 else ""

    flow = detect_flow_direction(
        market_data["selection"],
        market_data["market_group"],
        home, away
    )

    match_key = make_match_key(teams, href)

    row = {
        "scraped_at": scraped_at,
        "sport": "Football",
        "country": country,
        "country_slug": country_slug,
        "league": league,
        "teams": teams,
        "match_date": match_date,
        "match_time": match_time,
        "match_datetime": match_datetime,
        "market": market_data["market"],
        "market_code": market_data["market_code"],
        "market_group": market_data["market_group"],
        "period": period,
        "selection": market_data["selection"],
        "selection_code": market_data["selection_code"],
        "flow_to_team": flow["flow_to_team"],
        "flow_direction": flow["flow_direction"],
        "flow_confidence": flow["flow_confidence"],
        "handicap_line": market_data["handicap_line"],
        "drop_pct": displayed_drop,
        "calculated_drop_pct": round(calculated_drop, 2),
        "odds_old": old_odds,
        "odds_new": new_odds,
        "best_current_odds": best_odds,
        "qualifies": qualifies_odds(old_odds, new_odds),
        "match_url": href,
        "match_key": match_key,
    }

    return row, None


# ============================================================
# CSV HELPERS
# ============================================================

def write_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(
            file, fieldnames=fields, extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(rows)


def ensure_csv_header(path, fields):
    if not path.exists() or path.stat().st_size == 0:
        return

    with open(path, newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames == fields:
            return
        old_rows = list(reader)

    write_csv(path, old_rows, fields)


def append_csv(path, rows, fields):
    if not rows:
        return

    ensure_csv_header(path, fields)
    exists = path.exists() and path.stat().st_size > 0

    with open(path, "a", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(
            file, fieldnames=fields, extrasaction="ignore"
        )
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


# ============================================================
# SQLITE SCHEMA / MIGRATION
# ============================================================

DB_COLUMNS = {
    "record_key": "TEXT",
    "scraped_at": "TEXT",
    "sport": "TEXT",
    "country": "TEXT",
    "country_slug": "TEXT",
    "league": "TEXT",
    "teams": "TEXT",
    "match_date": "TEXT",
    "match_time": "TEXT",
    "match_datetime": "TEXT",
    "market": "TEXT",
    "market_code": "TEXT",
    "market_group": "TEXT",
    "period": "TEXT",
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
    columns_sql = ",\n".join(
        f'"{name}" {ctype}' for name, ctype in DB_COLUMNS.items()
    )

    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS odds_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            {columns_sql}
        )
    """)

    existing = {
        item[1] for item in conn.execute(
            "PRAGMA table_info(odds_history)"
        ).fetchall()
    }

    for column, column_type in DB_COLUMNS.items():
        if column not in existing:
            conn.execute(
                f'ALTER TABLE odds_history ADD COLUMN "{column}" {column_type}'
            )

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_odds_history_record_key
        ON odds_history(record_key)
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS odds_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at_utc TEXT NOT NULL,
            series_key TEXT NOT NULL,
            match_key TEXT NOT NULL,
            teams TEXT,
            country TEXT,
            league TEXT,
            match_datetime_utc TEXT,
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
            match_url TEXT
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_snapshots_series_time
        ON odds_snapshots(series_key, observed_at_utc)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_snapshots_match
        ON odds_snapshots(match_key, observed_at_utc)
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS tracked_matches (
            match_key TEXT PRIMARY KEY,
            teams TEXT,
            country TEXT,
            league TEXT,
            match_url TEXT,
            kickoff_utc TEXT,
            first_seen_utc TEXT,
            last_seen_utc TEXT,
            t30_status TEXT DEFAULT 'PENDING'
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS final_odds_t30 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_key TEXT NOT NULL,
            series_key TEXT NOT NULL,
            teams TEXT,
            country TEXT,
            league TEXT,
            kickoff_utc TEXT,
            target_t30_utc TEXT,
            snapshot_utc TEXT,
            actual_checkpoint TEXT,
            checkpoint_status TEXT,
            market_code TEXT,
            market_group TEXT,
            period TEXT,
            selection TEXT,
            odds_old REAL,
            checkpoint_odds REAL,
            qualifies_at_t30 INTEGER,
            match_url TEXT,
            UNIQUE(match_key, series_key)
        )
    """)

    conn.commit()


def save_history(conn, rows):
    new_rows = []

    table_info = conn.execute(
        "PRAGMA table_info(odds_history)"
    ).fetchall()
    columns = {item[1] for item in table_info}

    for row in rows:
        if row.get("market_code") not in ALLOWED_MARKET_CODES:
            continue

        record_key = make_record_key(row)
        exists = conn.execute(
            "SELECT 1 FROM odds_history WHERE record_key=? LIMIT 1",
            (record_key,)
        ).fetchone()

        if exists:
            continue

        values = {"record_key": record_key, **row}
        insert_columns = [key for key in values if key in columns]

        for item in table_info:
            name, not_null, default_value, primary_key = (
                item[1], item[3], item[4], item[5]
            )
            if (
                name not in insert_columns and not_null
                and not primary_key and default_value is None
            ):
                insert_columns.append(name)
                values[name] = ""

        column_sql = ", ".join(f'"{c}"' for c in insert_columns)
        placeholders = ", ".join("?" for _ in insert_columns)
        params = [values.get(c) for c in insert_columns]

        conn.execute(
            f"INSERT INTO odds_history ({column_sql}) VALUES ({placeholders})",
            params
        )
        new_rows.append(row)

    conn.commit()
    return new_rows


# ============================================================
# TRACKED MATCHES / SNAPSHOTS
# ============================================================

def in_horizon(kickoff_utc, now_utc):
    if not kickoff_utc:
        return False
    kickoff = parse_iso(kickoff_utc)
    if not kickoff:
        return False
    return now_utc <= kickoff <= now_utc + timedelta(days=HORIZON_DAYS)


def save_tracked_matches(conn, rows, now_utc):
    now_text = iso_utc(now_utc)

    for row in rows:
        kickoff = parse_iso(row.get("match_datetime"))
        if not kickoff or not in_horizon(iso_utc(kickoff), now_utc):
            continue

        conn.execute("""
            INSERT INTO tracked_matches (
                match_key, teams, country, league, match_url,
                kickoff_utc, first_seen_utc, last_seen_utc, t30_status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
            ON CONFLICT(match_key) DO UPDATE SET
                teams=excluded.teams,
                country=excluded.country,
                league=excluded.league,
                match_url=excluded.match_url,
                kickoff_utc=excluded.kickoff_utc,
                last_seen_utc=excluded.last_seen_utc
        """, (
            row["match_key"], row["teams"], row["country"], row["league"],
            normalize_match_url(row["match_url"]),
            iso_utc(kickoff), now_text, now_text
        ))

    conn.commit()


def save_snapshots(conn, rows, now_utc):
    now_text = iso_utc(now_utc)

    for row in rows:
        # Зберігаємо знімки тільки дозволених ринків.
        if row.get("market_code") not in ALLOWED_MARKET_CODES:
            continue

        kickoff = parse_iso(row.get("match_datetime"))
        if not kickoff or not in_horizon(iso_utc(kickoff), now_utc):
            continue

        series_key = make_series_key(row)

        conn.execute("""
            INSERT INTO odds_snapshots (
                observed_at_utc, series_key, match_key, teams, country, league,
                match_datetime_utc, market_code, market_group, period,
                selection, handicap_line, odds_old, odds_new,
                best_current_odds, drop_pct, qualifies, match_url
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            now_text, series_key, row["match_key"], row["teams"],
            row["country"], row["league"], iso_utc(kickoff),
            row["market_code"], row["market_group"], row["period"],
            row["selection"], row["handicap_line"], row["odds_old"],
            row["odds_new"], row["best_current_odds"], row["drop_pct"],
            int(bool(row["qualifies"])),
            normalize_match_url(row["match_url"])
        ))

    conn.commit()


# ============================================================
# MOVEMENT 15 / 30 / 60 MINUTES
# ============================================================

def nearest_snapshot(conn, series_key, target_utc, tolerance_minutes=8):
    target_text = iso_utc(target_utc)
    rows = conn.execute("""
        SELECT observed_at_utc, best_current_odds, odds_new
        FROM odds_snapshots
        WHERE series_key=?
          AND observed_at_utc BETWEEN ? AND ?
        ORDER BY observed_at_utc
    """, (
        series_key,
        iso_utc(target_utc - timedelta(minutes=tolerance_minutes)),
        iso_utc(target_utc + timedelta(minutes=tolerance_minutes))
    )).fetchall()

    candidates = []
    for observed, best, new in rows:
        dt = parse_iso(observed)
        if dt:
            candidates.append((abs((dt - target_utc).total_seconds()), dt, best or new))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])
    _, dt, odds = candidates[0]
    return dt, odds


def calculate_movement(conn, rows, now_utc):
    output = []

    for row in rows:
        series_key = make_series_key(row)
        current = row.get("best_current_odds") or row.get("odds_new")
        if current is None:
            continue

        values = {}
        for minutes in (15, 30, 60):
            target = now_utc - timedelta(minutes=minutes)
            found = nearest_snapshot(
                conn, series_key, target, tolerance_minutes=8
            )
            values[minutes] = found

        def pct(old, new):
            if old is None or new is None or old <= 0:
                return ""
            return round(((new - old) / old) * 100, 2)

        snap15 = values[15]
        snap30 = values[30]
        snap60 = values[60]

        output.append({
            "calculated_at_utc": iso_utc(now_utc),
            "match_key": row["match_key"],
            "teams": row["teams"],
            "market_code": row["market_code"],
            "period": row["period"],
            "selection": row["selection"],
            "reference_odds": row["odds_old"],
            "current_odds": current,
            "change_15m_pct": pct(snap15[1], current) if snap15 else "",
            "change_30m_pct": pct(snap30[1], current) if snap30 else "",
            "change_60m_pct": pct(snap60[1], current) if snap60 else "",
            "snapshot_15m_utc": iso_utc(snap15[0]) if snap15 else "",
            "snapshot_30m_utc": iso_utc(snap30[0]) if snap30 else "",
            "snapshot_60m_utc": iso_utc(snap60[0]) if snap60 else "",
        })

    return output


# ============================================================
# T-30 CHECKPOINT
# ============================================================

def process_t30_checkpoints(conn, now_utc):
    """
    T-30 завершується після закриття вікна допуску:
    ціль T-30 +/- 15 хвилин.
    Тобто фіналізуємо після kickoff - 15 хвилин.

    Для кожного tracked match зберігаємо окремий результат
    по кожній серії ринку/вибору.
    """
    finalized = []

    tracked = conn.execute("""
        SELECT match_key, teams, country, league, match_url, kickoff_utc, t30_status
        FROM tracked_matches
        WHERE t30_status='PENDING'
    """).fetchall()

    for item in tracked:
        match_key, teams, country, league, match_url, kickoff_text, _ = item
        kickoff = parse_iso(kickoff_text)
        if not kickoff:
            continue

        target = kickoff - timedelta(minutes=T30_TARGET_MINUTES)
        window_end = target + timedelta(minutes=T30_TOLERANCE_MINUTES)

        if now_utc < window_end:
            continue

        series_rows = conn.execute("""
            SELECT DISTINCT series_key
            FROM odds_snapshots
            WHERE match_key=?
        """, (match_key,)).fetchall()

        if not series_rows:
            # Немає жодного знімка для цього матчу.
            conn.execute("""
                INSERT OR IGNORE INTO final_odds_t30 (
                    match_key, series_key, teams, country, league,
                    kickoff_utc, target_t30_utc, checkpoint_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'UNAVAILABLE')
            """, (
                match_key, "NO_SNAPSHOT", teams, country, league,
                iso_utc(kickoff), iso_utc(target)
            ))
            conn.execute(
                "UPDATE tracked_matches SET t30_status='FINALIZED' WHERE match_key=?",
                (match_key,)
            )
            finalized.append({
                "match_key": match_key,
                "teams": teams,
                "checkpoint_status": "UNAVAILABLE",
            })
            continue

        for (series_key,) in series_rows:
            candidates = conn.execute("""
                SELECT observed_at_utc, market_code, market_group, period,
                       selection, odds_old, best_current_odds, odds_new,
                       match_url
                FROM odds_snapshots
                WHERE series_key=?
                  AND observed_at_utc BETWEEN ? AND ?
                ORDER BY observed_at_utc
            """, (
                series_key,
                iso_utc(target - timedelta(minutes=T30_TOLERANCE_MINUTES)),
                iso_utc(target + timedelta(minutes=T30_TOLERANCE_MINUTES))
            )).fetchall()

            if not candidates:
                conn.execute("""
                    INSERT OR IGNORE INTO final_odds_t30 (
                        match_key, series_key, teams, country, league,
                        kickoff_utc, target_t30_utc, checkpoint_status,
                        match_url
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'UNAVAILABLE', ?)
                """, (
                    match_key, series_key, teams, country, league,
                    iso_utc(kickoff), iso_utc(target), match_url
                ))
                continue

            def distance(candidate):
                dt = parse_iso(candidate[0])
                return abs((dt - target).total_seconds()) if dt else float("inf")

            chosen = min(candidates, key=distance)
            (
                snapshot_text, market_code, market_group, period,
                selection, odds_old, best_current, odds_new, snapshot_url
            ) = chosen

            snapshot_dt = parse_iso(snapshot_text)
            checkpoint_odds = best_current if best_current is not None else odds_new

            if checkpoint_odds is None:
                status = "UNAVAILABLE"
                qualifies = None
            else:
                status = "AVAILABLE"
                qualifies = int(qualifies_odds(odds_old, checkpoint_odds))

            actual_minutes = (
                round((kickoff - snapshot_dt).total_seconds() / 60)
                if snapshot_dt else None
            )
            actual_checkpoint = (
                f"T-{actual_minutes}"
                if actual_minutes is not None else ""
            )

            conn.execute("""
                INSERT OR IGNORE INTO final_odds_t30 (
                    match_key, series_key, teams, country, league,
                    kickoff_utc, target_t30_utc, snapshot_utc,
                    actual_checkpoint, checkpoint_status,
                    market_code, market_group, period, selection,
                    odds_old, checkpoint_odds, qualifies_at_t30, match_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                match_key, series_key, teams, country, league,
                iso_utc(kickoff), iso_utc(target), snapshot_text,
                actual_checkpoint, status, market_code, market_group,
                period, selection, odds_old, checkpoint_odds,
                qualifies, snapshot_url
            ))

        conn.execute(
            "UPDATE tracked_matches SET t30_status='FINALIZED' WHERE match_key=?",
            (match_key,)
        )
        finalized.append({
            "match_key": match_key,
            "teams": teams,
            "checkpoint_status": "PROCESSED",
        })

    conn.commit()
    return finalized


def export_t30_csv(conn):
    rows = conn.execute("""
        SELECT match_key, teams, country, league, kickoff_utc,
               target_t30_utc, snapshot_utc, actual_checkpoint,
               checkpoint_status, market_code, market_group, period,
               selection, odds_old, checkpoint_odds,
               qualifies_at_t30, match_url
        FROM final_odds_t30
        ORDER BY kickoff_utc DESC
    """).fetchall()

    output = []
    for r in rows:
        (
            match_key, teams, country, league, kickoff, target, snapshot,
            actual, status, market_code, market_group, period, selection,
            old, checkpoint, qualifies, url
        ) = r

        kickoff_dt = parse_iso(kickoff)
        target_dt = parse_iso(target)
        snapshot_dt = parse_iso(snapshot)

        output.append({
            "match_key": match_key,
            "teams": teams,
            "country": country,
            "league": league,
            "kickoff_utc": kickoff or "",
            "kickoff_kyiv": iso_kyiv(kickoff_dt),
            "target_t30_utc": target or "",
            "target_t30_kyiv": iso_kyiv(target_dt),
            "snapshot_utc": snapshot or "",
            "snapshot_kyiv": iso_kyiv(snapshot_dt),
            "actual_checkpoint": actual or "",
            "checkpoint_status": status or "",
            "market_code": market_code or "",
            "market_group": market_group or "",
            "period": period or "",
            "selection": selection or "",
            "odds_old": old,
            "checkpoint_odds": checkpoint,
            "qualifies_at_t30": qualifies,
            "match_url": url or "",
        })

    write_csv(T30_CSV, output, T30_FIELDS)


# ============================================================
# SIGNALS
# ============================================================

def build_signals(rows, scraped_at):
    grouped = {}

    for row in rows:
        if not row.get("qualifies"):
            continue
        if row.get("market_code") not in ALLOWED_MARKET_CODES:
            continue
        if row.get("market_group") not in TARGET_SIGNAL_GROUPS:
            continue
        if row.get("period", "FT") not in SIGNAL_PERIODS:
            continue

        key = row.get("match_key")
        if not key:
            continue

        item = grouped.setdefault(key, {"rows": [], "groups": set()})
        item["rows"].append(row)
        item["groups"].add(row["market_group"])

    signals = []

    for key, item in grouped.items():
        match_rows = item["rows"]
        first = match_rows[0]
        groups = item["groups"]

        market_lines = []
        for row in match_rows:
            market_lines.append(
                "{} [{}] {} -> {} | Вибір: {}".format(
                    row.get("market", ""),
                    row.get("selection", ""),
                    row.get("odds_old", ""),
                    row.get("odds_new", ""),
                    row.get("flow_to_team", "Невизначено"),
                )
            )

        signals.append({
            "scraped_at": scraped_at,
            "teams": first.get("teams", ""),
            "country": first.get("country", ""),
            "country_slug": first.get("country_slug", ""),
            "league": first.get("league", ""),
            "match_date": first.get("match_date", ""),
            "match_time": first.get("match_time", ""),
            "match_datetime": first.get("match_datetime", ""),
            "market_count": len(groups),
            "market_groups": ", ".join(sorted(groups)),
            "priority": "NORMAL",
            "markets": " || ".join(market_lines),
            "match_url": normalize_match_url(first.get("match_url", "")),
            "match_key": key,
        })

    signals.sort(key=lambda item: item["teams"].lower())
    return signals


# ============================================================
# DEBUG
# ============================================================

def save_debug(data):
    DEBUG_JSON.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def save_html_samples(cards):
    fragments = []
    for index, card in enumerate(cards[:15]):
        html = card.get("html", "")
        if html:
            fragments.append(f"\n<!-- CARD {index + 1} -->\n{html}\n")
    DEBUG_HTML.write_text("\n".join(fragments), encoding="utf-8")


# ============================================================
# SCRAPER
# ============================================================

def scrape_page():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=HEADLESS,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )

        context = browser.new_context(
            viewport={"width": 1440, "height": 1000},
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

        try:
            response = page.goto(
                SOURCE_URL,
                wait_until="domcontentloaded",
                timeout=TIMEOUT_MS
            )
            http_status = response.status if response else None

            page.wait_for_timeout(6000)

            try:
                page.wait_for_selector(
                    'a[href*="/football/h2h/"]',
                    timeout=20000
                )
            except Exception:
                log.warning("Match links did not appear before timeout.")

            extracted = page.evaluate(EXTRACT_CARDS_JS)

            return {
                "http_status": http_status,
                "page_title": page.title(),
                "final_url": page.url,
                "cards": extracted.get("cards", []),
                "match_links_found": extracted.get("match_links_found", 0),
                "unique_match_links": extracted.get("unique_match_links", 0),
                "pagination_links": extracted.get("pagination_links", []),
                "body_text": clean_text(
                    page.locator("body").inner_text()
                )[:3000],
            }
        finally:
            context.close()
            browser.close()


# ============================================================
# MAIN
# ============================================================

def main():
    started = time.time()
    now_dt = utc_now_dt()
    scraped_at = iso_utc(now_dt)

    parse_failures = []
    rows = []

    log.info("Starting OddsPortal parser")
    log.info("Source: %s", SOURCE_URL)
    log.info("Horizon: next %s days", HORIZON_DAYS)
    log.info("Snapshot interval expected: %s minutes", SNAPSHOT_INTERVAL_MINUTES)
    log.info("Timezone output: Europe/Kyiv")
    log.info("Allowed markets: %s", sorted(ALLOWED_MARKET_CODES))
    log.info("Criterion 1: 4.00-6.00 -> <= 2.50")
    log.info("Criterion 2: 2.00-3.00 -> <= 1.80")

    result = scrape_page()
    cards = result["cards"]
    save_html_samples(cards)

    seen = set()

    for index, card in enumerate(cards):
        try:
            row, error = parse_card(card, scraped_at)

            if error:
                parse_failures.append({
                    "card": index + 1,
                    "reason": error,
                    "anchor_text": clean_text(card.get("anchorText", ""))[:300],
                    "card_text": clean_text(card.get("text", ""))[:700],
                })
                continue

            # Поточний список може містити матчі поза горизонтом.
            # У latest залишаємо всі розпарсені рядки, а історію знімків
            # і tracked_matches обмежуємо наступними 7 днями.
            duplicate_key = (
                row["match_key"], row["market_code"], row["market"],
                row["market_group"], row["period"], row["selection"],
                row["handicap_line"], row["odds_old"], row["odds_new"]
            )

            if duplicate_key in seen:
                parse_failures.append({
                    "card": index + 1,
                    "reason": "exact_duplicate_row",
                    "anchor_text": row["teams"],
                })
                continue

            seen.add(duplicate_key)
            rows.append(row)

        except Exception as exc:
            parse_failures.append({
                "card": index + 1,
                "reason": repr(exc),
                "anchor_text": clean_text(card.get("anchorText", ""))[:300],
            })

    # Для часової історії — тільки матчі з відомим часом у межах 7 днів.
    horizon_rows = []
    for row in rows:
        kickoff = parse_iso(row.get("match_datetime"))
        if kickoff and in_horizon(iso_utc(kickoff), now_dt):
            horizon_rows.append(row)

    qualifying_rows = [r for r in rows if r.get("qualifies")]
    signals = build_signals(rows, scraped_at)

    # Основний CSV поточного запуску.
    write_csv(LATEST_CSV, rows, CSV_FIELDS)

    conn = sqlite3.connect(DATABASE_FILE)
    try:
        initialize_database(conn)

        # Стара історія не видаляється.
        new_history_rows = save_history(conn, rows)
        append_csv(HISTORY_CSV, new_history_rows, CSV_FIELDS)

        # Зберігаємо знайдені матчі та кожне спостереження.
        save_tracked_matches(conn, horizon_rows, now_dt)
        save_snapshots(conn, horizon_rows, now_dt)

        # Розрахунок руху коефіцієнтів.
        movement_rows = calculate_movement(
            conn, horizon_rows, now_dt
        )
        write_csv(MOVEMENT_CSV, movement_rows, MOVEMENT_FIELDS)

        # Фіналізація T-30 для матчів, у яких закрилося вікно +/-15 хв.
        t30_processed = process_t30_checkpoints(conn, now_dt)
        export_t30_csv(conn)

        # Експорт усіх знімків у CSV.
        snapshot_db_rows = conn.execute("""
            SELECT observed_at_utc, match_key, teams, country, league,
                   match_datetime_utc, market_code, market_group, period,
                   selection, handicap_line, odds_old, odds_new,
                   best_current_odds, drop_pct, qualifies, match_url
            FROM odds_snapshots
            ORDER BY observed_at_utc DESC
        """).fetchall()

        snapshot_export = []
        for r in snapshot_db_rows:
            (
                observed, match_key, teams, country, league, kickoff,
                market_code, market_group, period, selection, line,
                old, new, best, drop, qualifies, url
            ) = r

            observed_dt = parse_iso(observed)
            kickoff_dt = parse_iso(kickoff)

            snapshot_export.append({
                "observed_at_utc": observed or "",
                "observed_at_kyiv": iso_kyiv(observed_dt),
                "match_key": match_key or "",
                "teams": teams or "",
                "country": country or "",
                "league": league or "",
                "match_datetime_utc": kickoff or "",
                "match_datetime_kyiv": iso_kyiv(kickoff_dt),
                "market_code": market_code or "",
                "market_group": market_group or "",
                "period": period or "",
                "selection": selection or "",
                "handicap_line": line or "",
                "odds_old": old,
                "odds_new": new,
                "best_current_odds": best,
                "drop_pct": drop,
                "qualifies": qualifies,
                "match_url": url or "",
            })

        write_csv(SNAPSHOTS_CSV, snapshot_export, SNAPSHOT_FIELDS)

    finally:
        conn.close()

    # Поточні сигнали — один NORMAL на матч.
    write_csv(SIGNALS_CSV, signals, SIGNAL_FIELDS)

    group_counts = {}
    period_counts = {}

    for row in rows:
        group = row.get("market_group", "other")
        period = row.get("period", "FT")
        group_counts[group] = group_counts.get(group, 0) + 1
        period_counts[period] = period_counts.get(period, 0) + 1

    missing = {
        "country": sum(not r.get("country") for r in rows),
        "league": sum(not r.get("league") for r in rows),
        "match_date": sum(not r.get("match_date") for r in rows),
        "match_time": sum(not r.get("match_time") for r in rows),
        "match_datetime": sum(not r.get("match_datetime") for r in rows),
        "teams": sum(not r.get("teams") for r in rows),
        "best_current_odds": sum(
            r.get("best_current_odds") in (None, "") for r in rows
        ),
        "selection": sum(not r.get("selection") for r in rows),
    }

    duration = round(time.time() - started, 2)

    debug = {
        "started_at_utc": scraped_at,
        "started_at_kyiv": iso_kyiv(now_dt),
        "url": SOURCE_URL,
        "http_status": result["http_status"],
        "page_title": result["page_title"],
        "final_url": result["final_url"],
        "match_links_found": result["match_links_found"],
        "unique_match_links": result["unique_match_links"],
        "cards_found": len(cards),
        "parsed_rows": len(rows),
        "horizon_rows": len(horizon_rows),
        "qualifying_rows": len(qualifying_rows),
        "signals": len(signals),
        "new_database_records": len(new_history_rows),
        "t30_processed": t30_processed,
        "horizon_days": HORIZON_DAYS,
        "snapshot_interval_minutes": SNAPSHOT_INTERVAL_MINUTES,
        "t30_target_minutes": T30_TARGET_MINUTES,
        "t30_tolerance_minutes": T30_TOLERANCE_MINUTES,
        "timezone": "Europe/Kyiv",
        "source_time_assumption": "OddsPortal displayed match time treated as UTC",
        "criteria": {
            "criterion_1": {
                "old_min": 4.00, "old_max": 6.00, "new_max": 2.50
            },
            "criterion_2": {
                "old_min": 2.00, "old_max": 3.00, "new_max": 1.80
            },
        },
        "allowed_market_codes": sorted(ALLOWED_MARKET_CODES),
        "history_policy": (
            "old odds_history rows preserved; new records only 1X2 and Total"
        ),
        "snapshot_policy": (
            "every run stores an observation for discovered matches in 7-day horizon"
        ),
        "t30_policy": (
            "nearest stored snapshot within +/-15 minutes of kickoff minus 30 minutes; "
            "otherwise UNAVAILABLE"
        ),
        "t30_limitation": (
            "snapshots are sourced from Dropping Odds only; no independent match-page refresh"
        ),
        "signal_policy": (
            "one NORMAL signal per match if at least one qualifying 1X2 or Total FT row exists"
        ),
        "signal_periods": sorted(SIGNAL_PERIODS),
        "missing": missing,
        "market_group_counts": group_counts,
        "period_counts": period_counts,
        "pagination_links": result["pagination_links"],
        "parse_failures": len(parse_failures),
        "parse_failure_samples": parse_failures[:40],
        "signal_samples": signals[:20],
        "body_preview": result["body_text"],
        "duration_seconds": duration,
        "finished_at_utc": utc_now(),
        "result": "completed",
    }

    save_debug(debug)

    log.info(
        "Cards=%s Parsed=%s Horizon=%s Qualifying=%s Signals=%s NewDB=%s",
        len(cards), len(rows), len(horizon_rows),
        len(qualifying_rows), len(signals), len(new_history_rows)
    )
    log.info("Market groups=%s", group_counts)
    log.info("Periods=%s", period_counts)
    log.info("Missing=%s failures=%s", missing, len(parse_failures))
    log.info("T30 processed=%s", len(t30_processed))
    log.info("Finished in %s seconds", duration)


if __name__ == "__main__":
    main()
