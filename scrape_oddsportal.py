
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OddsPortal — сильні падіння коефіцієнтів.

Критерії:
1) Початковий коефіцієнт 4.00–5.00 -> поточний <= 2.00
2) Початковий коефіцієнт 2.00–3.00 -> поточний <= 1.80

Пріоритет матчу:
1 ринок  = NORMAL
2 ринки  = HIGH
3 ринки  = TOP

Ринки:
- 1X2
- Мінусова фора
- Загальний тотал

Важливо:
Парсер не вважає довільні числа коефіцієнтами.
Якщо не вдається надійно визначити початковий і поточний
коефіцієнти, рядок записується в debug, але не стає сигналом.
"""

import asyncio
import csv
import json
import os
import re
import sqlite3
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright


# ============================================================
# НАЛАШТУВАННЯ
# ============================================================

BASE_URL = "https://www.oddsportal.com"
START_URL = os.getenv(
    "ODDSPORTAL_URL",
    "https://www.oddsportal.com/dropping-odds/"
)

HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"
TIMEOUT = 60000

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

LATEST_CSV = DATA_DIR / "oddsportal_latest.csv"
HISTORY_CSV = DATA_DIR / "oddsportal_history.csv"
SIGNALS_CSV = DATA_DIR / "oddsportal_signals.csv"
DEBUG_JSON = DATA_DIR / "oddsportal_debug.json"
DB_FILE = DATA_DIR / "oddsportal_history.sqlite"

CSV_FIELDS = [
    "scraped_at",
    "match",
    "league",
    "market",
    "selection",
    "line",
    "odds_old",
    "odds_new",
    "drop_pct",
    "match_url",
    "market_key",
    "strong_drop",
    "markets_count",
    "priority",
]

DEBUG_FIELDS = [
    "scraped_at",
    "match",
    "url",
    "market",
    "selection",
    "odds_old",
    "odds_new",
    "drop_pct",
    "reason",
    "raw_text",
]


# ============================================================
# ДОПОМІЖНІ ФУНКЦІЇ
# ============================================================

def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


def parse_number(value):
    if value is None:
        return None

    value = str(value).strip().replace(",", ".")

    match = re.search(r"\d+(?:\.\d+)?", value)
    if not match:
        return None

    try:
        number = float(match.group())
        if 1.001 <= number <= 1000:
            return number
    except ValueError:
        pass

    return None


def calculate_drop(old, new):
    if not old or not new or old <= 0 or new <= 0:
        return None

    return round((old - new) / old * 100, 2)


def is_strong_drop(old, new):
    """
    Точні критерії користувача.
    """
    if old is None or new is None:
        return False

    if 4.00 <= old <= 5.00 and new <= 2.00:
        return True

    if 2.00 <= old <= 3.00 and new <= 1.80:
        return True

    return False


def classify_market(text):
    """
    Визначаємо ринок лише за явними словами.
    Не використовуємо невідомий числовий ID з URL.
    """
    t = text.lower()

    # Загальний тотал
    total_patterns = [
        r"over\s*/\s*under",
        r"total goals",
        r"total",
        r"over\b",
        r"under\b",
        r"більше",
        r"менше",
        r"тотал",
        r"\bo/u\b",
    ]

    # Фора
    handicap_patterns = [
        r"asian handicap",
        r"handicap",
        r"spread",
        r"фора",
    ]

    # 1X2
    result_patterns = [
        r"\b1x2\b",
        r"match result",
        r"full time result",
        r"1x2 result",
        r"home win",
        r"away win",
        r"draw",
    ]

    if any(re.search(p, t) for p in total_patterns):
        return "TOTAL"

    if any(re.search(p, t) for p in handicap_patterns):
        return "HANDICAP"

    if any(re.search(p, t) for p in result_patterns):
        return "1X2"

    return ""


def extract_selection(text, market):
    """
    Визначає назву вибору, якщо вона явно присутня в тексті.
    """
    t = clean_text(text)
    low = t.lower()

    if market == "1X2":
        if re.search(r"\b(draw|х|нiчия|нічия)\b", low):
            return "X"
        if re.search(r"\b(home win|1|п1)\b", low):
            return "П1"
        if re.search(r"\b(away win|2|п2)\b", low):
            return "П2"

    if market == "TOTAL":
        if re.search(r"\bover\b|більше|тб", low):
            return "ТБ"
        if re.search(r"\bunder\b|менше|тм", low):
            return "ТМ"

    if market == "HANDICAP":
        if re.search(r"home|team 1|п1", low):
            return "Ф1"
        if re.search(r"away|team 2|п2", low):
            return "Ф2"

    return ""


def extract_line(text):
    """
    Лінія тоталу або фори, якщо вона явно вказана.
    Наприклад: -1.5, +0.5, 2.5.
    """
    patterns = [
        r"([+-]\s*\d+(?:[.,]\d+)?)",
        r"(?:over|under|більше|менше|total|тотал)\s*(\d+(?:[.,]\d+)?)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text.lower())
        if match:
            return match.group(1).replace(" ", "").replace(",", ".")

    return ""


# ============================================================
# ВИЛУЧЕННЯ КОЕФІЦІЄНТІВ
# ============================================================

def extract_odds_from_attributes(attributes):
    """
    Шукаємо тільки явно названі атрибути.
    """
    old_keys = [
        "data-old-odds",
        "data-opening-odds",
        "data-initial-odds",
        "data-odds-old",
        "data-odds-opening",
    ]

    new_keys = [
        "data-new-odds",
        "data-current-odds",
        "data-latest-odds",
        "data-odds-new",
        "data-odds-current",
    ]

    old = None
    new = None

    for key in old_keys:
        if key in attributes:
            old = parse_number(attributes[key])
            if old:
                break

    for key in new_keys:
        if key in attributes:
            new = parse_number(attributes[key])
            if new:
                break

    return old, new


def extract_explicit_odds(text):
    """
    Пошук лише явних підписів Opening/Current або Old/New.
    Не бере довільну пару чисел із тексту.
    """
    patterns = [
        (
            r"(?:opening|initial|old)\s*odds?\s*[:=]?\s*(\d+(?:[.,]\d+)?)"
            r".{0,80}?"
            r"(?:current|latest|new)\s*odds?\s*[:=]?\s*(\d+(?:[.,]\d+)?)"
        ),
        (
            r"(?:current|latest|new)\s*odds?\s*[:=]?\s*(\d+(?:[.,]\d+)?)"
            r".{0,80}?"
            r"(?:opening|initial|old)\s*odds?\s*[:=]?\s*(\d+(?:[.,]\d+)?)"
        ),
    ]

    for index, pattern in enumerate(patterns):
        match = re.search(pattern, text, re.IGNORECASE)

        if match:
            first = parse_number(match.group(1))
            second = parse_number(match.group(2))

            if index == 0:
                return first, second

            return second, first

    return None, None


# ============================================================
# MATCH / LEAGUE
# ============================================================

def is_match_url(url):
    path = urlparse(url).path.lower()

    return (
        "/football/h2h/" in path
        or "/match/" in path
    )


def get_match_name(anchor_text):
    text = clean_text(anchor_text)

    if not text:
        return ""

    text = re.sub(r"\s+", " ", text)
    return text[:180]


def get_league_from_text(text, match):
    """
    Прибираємо назву матчу з тексту картки.
    Залишаємо решту як допоміжну назву турніру.
    """
    if not text:
        return ""

    result = text.replace(match, "")
    result = clean_text(result)

    return result[:180]


# ============================================================
# SQLITE — ІСТОРІЯ
# ============================================================

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS odds_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scraped_at TEXT NOT NULL,
                match_key TEXT NOT NULL,
                match TEXT,
                league TEXT,
                market TEXT,
                selection TEXT,
                line TEXT,
                odds_old REAL,
                odds_new REAL,
                drop_pct REAL,
                match_url TEXT,
                market_key TEXT,
                strong_drop INTEGER
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_match_market
            ON odds_history(match_key, market_key)
        """)

        conn.commit()


def make_match_key(match, url):
    base = clean_text(match).lower()

    if not base:
        base = urlparse(url).path.lower()

    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def save_history(rows):
    with sqlite3.connect(DB_FILE) as conn:
        for row in rows:
            match_key = make_match_key(
                row["match"],
                row["match_url"]
            )

            market_key = row["market_key"]

            last = conn.execute("""
                SELECT odds_new
                FROM odds_history
                WHERE match_key = ?
                  AND market_key = ?
                ORDER BY id DESC
                LIMIT 1
            """, (match_key, market_key)).fetchone()

            # Не дублюємо незмінні коефіцієнти
            if last and last[0] is not None:
                if abs(float(last[0]) - row["odds_new"]) < 0.0001:
                    continue

            conn.execute("""
                INSERT INTO odds_history (
                    scraped_at, match_key, match, league,
                    market, selection, line, odds_old,
                    odds_new, drop_pct, match_url,
                    market_key, strong_drop
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                row["scraped_at"],
                match_key,
                row["match"],
                row["league"],
                row["market"],
                row["selection"],
                row["line"],
                row["odds_old"],
                row["odds_new"],
                row["drop_pct"],
                row["match_url"],
                market_key,
                int(row["strong_drop"]),
            ))

        conn.commit()


# ============================================================
# CSV
# ============================================================

def write_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
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

    exists = path.exists()

    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=fields,
            extrasaction="ignore"
        )

        if not exists:
            writer.writeheader()

        writer.writerows(rows)


# ============================================================
# ПАРСИНГ СТОРІНКИ
# ============================================================

async def collect_match_cards(page):
    """
    Збираємо реальні посилання на матчі.
    Для кожного посилання шукаємо найближчий контейнер
    з інформацією про матч і ринок.
    """
    anchors = await page.locator("a[href]").evaluate_all("""
        elements => elements.map(a => ({
            href: a.href,
            text: (a.innerText || a.textContent || '').trim()
        }))
    """)

    results = []
    seen = set()

    for anchor in anchors:
        url = anchor.get("href", "")
        name = get_match_name(anchor.get("text", ""))

        if not url or not is_match_url(url):
            continue

        if not name:
            continue

        key = (url.split("#")[0], name.lower())

        if key in seen:
            continue

        seen.add(key)

        # Шукаємо контейнер, який містить посилання на матч.
        card = page.locator(
            f'a[href="{url}"]'
        ).first

        raw_text = ""
        attributes = {}

        try:
            raw_text = await card.evaluate("""
                el => {
                    let node = el;
                    let best = el;

                    for (let i = 0; i < 7 && node; i++) {
                        const text = (node.innerText || '').trim();

                        if (text.length > 30 && text.length < 5000) {
                            best = node;
                        }

                        node = node.parentElement;
                    }

                    return best.innerText || '';
                }
            """)

            attributes = await card.evaluate("""
                el => {
                    let node = el;

                    for (let i = 0; i < 7 && node; i++) {
                        const attrs = {};

                        for (const a of node.attributes || []) {
                            attrs[a.name] = a.value;
                        }

                        if (
                            attrs['data-old-odds'] ||
                            attrs['data-opening-odds'] ||
                            attrs['data-current-odds'] ||
                            attrs['data-new-odds']
                        ) {
                            return attrs;
                        }

                        node = node.parentElement;
                    }

                    return {};
                }
            """)

        except Exception:
            pass

        results.append({
            "match": name,
            "url": url,
            "raw_text": clean_text(raw_text),
            "attributes": attributes or {},
        })

    return results


# ============================================================
# ОБРОБКА РИНКІВ
# ============================================================

def process_cards(cards):
    scraped = now_utc()

    valid_rows = []
    debug_rows = []

    for card in cards:
        match = card["match"]
        url = card["url"]
        raw = card["raw_text"]
        attributes = card["attributes"]

        market = classify_market(raw)

        if not market:
            debug_rows.append({
                "scraped_at": scraped,
                "match": match,
                "url": url,
                "market": "",
                "selection": "",
                "odds_old": "",
                "odds_new": "",
                "drop_pct": "",
                "reason": "Ринок не визначено",
                "raw_text": raw[:1500],
            })
            continue

        selection = extract_selection(raw, market)
        line = extract_line(raw)

        old, new = extract_odds_from_attributes(attributes)

        if old is None or new is None:
            text_old, text_new = extract_explicit_odds(raw)

            if old is None:
                old = text_old

            if new is None:
                new = text_new

        if old is None or new is None:
            debug_rows.append({
                "scraped_at": scraped,
                "match": match,
                "url": url,
                "market": market,
                "selection": selection,
                "odds_old": "",
                "odds_new": "",
                "drop_pct": "",
                "reason": "Не знайдено підтверджену пару old/new odds",
                "raw_text": raw[:1500],
            })
            continue

        if new >= old:
            debug_rows.append({
                "scraped_at": scraped,
                "match": match,
                "url": url,
                "market": market,
                "selection": selection,
                "odds_old": old,
                "odds_new": new,
                "drop_pct": calculate_drop(old, new),
                "reason": "Коефіцієнт не впав",
                "raw_text": raw[:1500],
            })
            continue

        drop = calculate_drop(old, new)
        strong = is_strong_drop(old, new)

        league = get_league_from_text(raw, match)

        market_key = "|".join([
            market,
            selection,
            line,
        ])

        valid_rows.append({
            "scraped_at": scraped,
            "match": match,
            "league": league,
            "market": market,
            "selection": selection,
            "line": line,
            "odds_old": old,
            "odds_new": new,
            "drop_pct": drop,
            "match_url": url,
            "market_key": market_key,
            "strong_drop": strong,
            "markets_count": 0,
            "priority": "",
        })

    return valid_rows, debug_rows


# ============================================================
# ГРУПУВАННЯ МАТЧІВ ТА ПРІОРИТЕТИ
# ============================================================

def group_matches(rows):
    grouped = {}

    for row in rows:
        key = make_match_key(
            row["match"],
            row["match_url"]
        )

        grouped.setdefault(key, []).append(row)

    signals = []

    for key, match_rows in grouped.items():
        strong_markets = {
            row["market"]
            for row in match_rows
            if row["strong_drop"]
        }

        count = len(strong_markets)

        if count == 0:
            continue

        if count >= 3:
            priority = "TOP"
        elif count == 2:
            priority = "HIGH"
        else:
            priority = "NORMAL"

        # Перевіряємо, чи є всі три різні групи ринків
        has_1x2 = "1X2" in strong_markets
        has_handicap = "HANDICAP" in strong_markets
        has_total = "TOTAL" in strong_markets

        all_three = has_1x2 and has_handicap and has_total

        for row in match_rows:
            row["markets_count"] = count
            row["priority"] = priority
            row["all_three_markets"] = all_three

            if row["strong_drop"]:
                signals.append(row)

    return signals


# ============================================================
# ГОЛОВНИЙ ЗАПУСК
# ============================================================

async def main():
    print("=" * 60)
    print("ODDSPORTAL — STRONG ODDS DROP SCANNER")
    print("=" * 60)

    init_db()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ]
        )

        page = await browser.new_page(
            viewport={
                "width": 1440,
                "height": 1000,
            },
            locale="en-US",
        )

        page.set_default_timeout(TIMEOUT)

        print(f"Opening: {START_URL}")

        await page.goto(
            START_URL,
            wait_until="domcontentloaded",
            timeout=TIMEOUT,
        )

        await page.wait_for_timeout(8000)

        # Додаткове прокручування для lazy loading
        for _ in range(5):
            await page.evaluate(
                "window.scrollTo(0, document.body.scrollHeight)"
            )
            await page.wait_for_timeout(1200)

        cards = await collect_match_cards(page)

        print(f"Match cards found: {len(cards)}")

        rows, debug_rows = process_cards(cards)

        # Всі підтверджені падіння
        write_csv(
            LATEST_CSV,
            rows,
            CSV_FIELDS
        )

        # Історія коефіцієнтів
        save_history(rows)

        append_csv(
            HISTORY_CSV,
            rows,
            CSV_FIELDS
        )

        # Сигнали з виконаними критеріями
        signals = group_matches(rows)

        write_csv(
            SIGNALS_CSV,
            signals,
            CSV_FIELDS
        )

        # Діагностика
        with open(DEBUG_JSON, "w", encoding="utf-8") as f:
            json.dump(
                debug_rows,
                f,
                ensure_ascii=False,
                indent=2
            )

        print("-" * 60)
        print(f"All parsed rows: {len(rows)}")
        print(f"Strong signals: {len(signals)}")
        print(f"Debug rows: {len(debug_rows)}")

        print("-" * 60)

        for row in signals:
            print(
                f"[{row['priority']}] "
                f"{row['match']} | "
                f"{row['market']} {row['selection']} "
                f"{row['line']} | "
                f"{row['odds_old']} -> {row['odds_new']} | "
                f"-{row['drop_pct']}% | "
                f"Markets: {row['markets_count']}"
            )

        print("-" * 60)
        print("Files saved:")
        print(LATEST_CSV)
        print(HISTORY_CSV)
        print(SIGNALS_CSV)
        print(DEBUG_JSON)

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
