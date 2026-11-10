#!/usr/bin/env python3
import asyncio
import csv
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

URL = "https://www.oddsportal.com/dropping-odds/3/1/1/football/"
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

LATEST_FILE = DATA_DIR / "oddsportal_latest.csv"
HISTORY_FILE = DATA_DIR / "oddsportal_history.csv"
STATE_FILE = DATA_DIR / "oddsportal_state.json"

MAX_PAGES = int(os.getenv("MAX_PAGES", "5"))
FIELDS = [
    "scraped_at", "league", "market", "selection", "teams",
    "match_time", "drop_pct", "odds_old", "odds_new",
    "best_current_odds", "match_url",
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")


def number(value):
    try:
        return float(str(value).replace(",", "."))
    except (ValueError, TypeError):
        return None


def parse_card(card):
    raw_label = (card.get("teams") or "").strip()
    event_match = re.match(
        r"^(Today|Tomorrow|\\d{1,2}\\s+\\w+)\\s+(\\d{1,2}:\\d{2})\\s+(.+)$",
        raw_label, re.I
    )
    if event_match:
        match_time = f"{event_match.group(1)} {event_match.group(2)}"
        teams = event_match.group(3).strip()
    else:
        match_time = ""
        teams = raw_label

    text = card.get("text", "")
    drop_match = re.search(r"(-\\d+(?:[.,]\\d+)?)\\s*%", text)
    if not drop_match or not teams:
        return None

    drop_pct = number(drop_match.group(1))
    after_drop = text[drop_match.end():]
    odds = re.findall(r"(?<![\\w:])\\d+[.,]\\d+", after_drop)
    if len(odds) < 3:
        return None

    odds_old, odds_new, best_current = map(number, odds[:3])
    if not all(x is not None and x > 0 for x in (odds_old, odds_new, best_current)):
        return None

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    market = ""
    selection = ""
    league = ""
    market_pattern = re.compile(
        r"^(1X2|HT/FT|Over/Under|Both Teams To Score|Asian Handicap|Draw No Bet|Double Chance)(?:,.*)?$",
        re.I
    )
    market_index = -1
    for i, line in enumerate(lines):
        if market_pattern.match(line):
            market, market_index = line, i
            break
    if market_index >= 0:
        for line in lines[market_index + 1:]:
            if line in ("1", "X", "2"):
                selection = line
                break
        if market_index > 0:
            league = lines[market_index - 1]

    return {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "league": league,
        "market": market,
        "selection": selection,
        "teams": teams,
        "match_time": match_time,
        "drop_pct": drop_pct,
        "odds_old": odds_old,
        "odds_new": odds_new,
        "best_current_odds": best_current,
        "match_url": urljoin(URL, card.get("url", "")),
    }


async def extract_cards(page):
    return await page.locator("a[href]").evaluate_all("""
        anchors => {
            const result = [];
            for (const a of anchors) {
                const label = (a.innerText || "").trim();
                const href = a.getAttribute("href") || "";
                if (!label || label.length < 5 || !href.includes("/football/")) continue;
                let node = a;
                let selected = null;
                for (let i = 0; i < 9 && node; i++) {
                    const text = (node.innerText || "").trim();
                    const hasDrop = /-\\d+(?:\\.\\d+)?\\s*%/.test(text);
                    const hasBest = /Best Current Odds/i.test(text);
                    const hasMarket = /1X2|Over\\/Under|HT\\/FT/i.test(text);
                    if (hasDrop && hasBest && hasMarket && text.length < 1800) {
                        selected = text;
                        break;
                    }
                    node = node.parentElement;
                }
                if (selected) result.push({teams: label, url: href, text: selected});
            }
            return result;
        }
    """)


async def scrape():
    rows, seen = [], set()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await browser.new_page(
            locale="en-GB", timezone_id="Europe/Kyiv",
            viewport={"width": 1440, "height": 1000}
        )
        await page.set_extra_http_headers({"Accept-Language": "en-GB,en;q=0.9"})
        response = await page.goto(URL, wait_until="domcontentloaded", timeout=90000)
        if response and response.status >= 400:
            raise RuntimeError(f"OddsPortal HTTP {response.status}")
        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except PlaywrightTimeoutError:
            logging.info("Network idle timeout; continuing")
        await page.wait_for_timeout(5000)

        for page_no in range(MAX_PAGES):
            cards = await extract_cards(page)
            logging.info("Page %s: found %s candidate blocks", page_no + 1, len(cards))
            for card in cards:
                row = parse_card(card)
                if not row:
                    continue
                key = (row["match_url"], row["market"], row["selection"], row["odds_new"])
                if key not in seen:
                    seen.add(key)
                    rows.append(row)

            if page_no + 1 >= MAX_PAGES:
                break
            next_button = page.get_by_role("button", name=re.compile(r"^Next$", re.I))
            if await next_button.count() == 0 or not await next_button.first.is_enabled():
                break
            await next_button.first.click()
            await page.wait_for_timeout(3000)

        await browser.close()
    return rows


def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_results(rows):
    state = load_state()
    changed = []
    for row in rows:
        key = "|".join([row["match_url"], row["market"], row["selection"]])
        signature = "|".join(str(row[k]) for k in (
            "drop_pct", "odds_old", "odds_new", "best_current_odds"
        ))
        if state.get(key) != signature:
            changed.append(row)
            state[key] = signature

    with LATEST_FILE.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    if changed:
        exists = HISTORY_FILE.exists()
        with HISTORY_FILE.open("a", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            if not exists:
                writer.writeheader()
            writer.writerows(changed)

    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info("Rows: %s; changed rows: %s", len(rows), len(changed))


async def main():
    rows = await scrape()
    if not rows:
        raise RuntimeError(
            "No rows parsed. OddsPortal page structure may have changed, "
            "or access may be blocked. Check the GitHub Actions log."
        )
    save_results(rows)
    logging.info("Parser finished successfully")


if __name__ == "__main__":
    asyncio.run(main())
