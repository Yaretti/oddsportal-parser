
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import csv
import json
import logging
import os
import re

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError
)

# ============================================================
# CONFIGURATION
# ============================================================

URL = "https://www.oddsportal.com/dropping-odds/3/1/1/football/"
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

DATA_DIR.mkdir(parents=True, exist_ok=True)

LATEST_FILE = DATA_DIR / "oddsportal_latest.csv"
HISTORY_FILE = DATA_DIR / "oddsportal_history.csv"
STATE_FILE = DATA_DIR / "oddsportal_state.json"

MAX_PAGES = int(os.getenv("MAX_PAGES", "5"))

FIELDS = [
    "scraped_at",
    "league",
    "market",
    "selection",
    "teams",
    "match_time",
    "drop_pct",
    "odds_old",
    "odds_new",
    "best_current_odds",
    "match_url",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)


# ============================================================
# HELPERS
# ============================================================

def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def number(value):
    try:
        return float(str(value).replace(",", "."))
    except (ValueError, TypeError):
        return None


def current_time():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ============================================================
# PARSE MATCH CARD
# ============================================================

def parse_card(card):

    raw_label = clean(card.get("teams", ""))
    text = card.get("text", "")

    if not raw_label or not text:
        return None

    # --------------------------------------------------------
    # Match date and time
    # --------------------------------------------------------

    event_match = re.match(
        r"^(Today|Tomorrow|\d{1,2}\s+\w+)\s+(\d{1,2}:\d{2})\s+(.+)$",
        raw_label,
        re.I
    )

    if event_match:
        match_time = (
            f"{event_match.group(1)} "
            f"{event_match.group(2)}"
        )
        teams = event_match.group(3).strip()
    else:
        match_time = ""
        teams = raw_label

    # --------------------------------------------------------
    # Odds drop percentage
    # --------------------------------------------------------

    drop_match = re.search(
        r"[-−]\s*(\d+(?:[.,]\d+)?)\s*%",
        text
    )

    if not drop_match:
        return None

    drop_pct = number(drop_match.group(1))

    if drop_pct is None:
        return None

    # --------------------------------------------------------
    # Odds extraction
    # --------------------------------------------------------

    after_drop = text[drop_match.end():]

    odds_matches = re.findall(
        r"(?<![\w:])(\d{1,3}[.,]\d{1,3})(?!\d)",
        after_drop
    )

    odds = [
        number(x)
        for x in odds_matches
    ]

    odds = [
        x for x in odds
        if x is not None and 1.0 <= x <= 1000
    ]

    if len(odds) < 3:
        return None

    odds_old = odds[0]
    odds_new = odds[1]
    best_current = odds[2]

    # --------------------------------------------------------
    # Market and league
    # --------------------------------------------------------

    lines = [
        clean(line)
        for line in text.splitlines()
        if clean(line)
    ]

    market = ""
    selection = ""
    league = ""

    market_pattern = re.compile(
        r"^(1X2|Over/Under|Over\s*/\s*Under|"
        r"Both Teams To Score|Asian Handicap|"
        r"Draw No Bet|Double Chance|"
        r"HT/FT|Handicap|Total Goals)"
        r"(?:,.*)?$",
        re.I
    )

    market_index = -1

    for i, line in enumerate(lines):

        if market_pattern.match(line):
            market = line
            market_index = i
            break

    if market_index >= 0:

        if market_index > 0:
            league = lines[market_index - 1]

        for line in lines[market_index + 1:]:

            if line in ("1", "X", "2"):
                selection = line
                break

            if re.match(
                r"^(Over|Under)\s+\d",
                line,
                re.I
            ):
                selection = line
                break

    # --------------------------------------------------------
    # Result
    # --------------------------------------------------------

    return {
        "scraped_at": current_time(),
        "league": league,
        "market": market,
        "selection": selection,
        "teams": teams,
        "match_time": match_time,
        "drop_pct": drop_pct,
        "odds_old": odds_old,
        "odds_new": odds_new,
        "best_current_odds": best_current,
        "match_url": urljoin(
            URL,
            card.get("url", "")
        ),
    }


# ============================================================
# EXTRACT CANDIDATE BLOCKS FROM PAGE
# ============================================================

async def extract_cards(page):

    return await page.locator("a[href]").evaluate_all("""
        anchors => {

            const result = [];
            const seen = new Set();

            for (const a of anchors) {

                const label = (a.innerText || "").trim();
                const href = a.getAttribute("href") || "";

                if (!label || label.length < 3) continue;

                if (
                    !href.includes("/football/") &&
                    !href.includes("/match/")
                ) continue;

                let node = a;
                let selected = null;

                for (let i = 0; i < 10 && node; i++) {

                    const text = (node.innerText || "")
                        .replace(/\\s+/g, " ")
                        .trim();

                    const hasDrop =
                        /[-−]\\s*\\d+(?:[.,]\\d+)?\\s*%/.test(text);

                    const hasOdds =
                        /\\d+[.,]\\d+/.test(text);

                    const hasMarket =
                        /1X2|Over\\s*\\/\\s*Under|HT\\/FT|Best Current Odds/i
                        .test(text);

                    if (
                        hasDrop &&
                        hasOdds &&
                        hasMarket &&
                        text.length < 2500
                    ) {
                        selected = text;
                        break;
                    }

                    node = node.parentElement;
                }

                if (!selected) continue;

                const key = href + "|" + selected.slice(0, 150);

                if (seen.has(key)) continue;

                seen.add(key);

                result.push({
                    teams: label,
                    url: href,
                    text: selected
                });
            }

            return result;
        }
    """)


# ============================================================
# SCRAPER
# ============================================================

async def scrape():

    rows = []
    seen = set()

    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox"]
        )

        page = await browser.new_page(
            locale="en-GB",
            timezone_id="Europe/Kyiv",
            viewport={
                "width": 1440,
                "height": 1200
            }
        )

        await page.set_extra_http_headers({
            "Accept-Language": "en-GB,en;q=0.9"
        })

        logging.info("Opening OddsPortal: %s", URL)

        response = await page.goto(
            URL,
            wait_until="domcontentloaded",
            timeout=90000
        )

        if response and response.status >= 400:
            await browser.close()
            raise RuntimeError(
                f"OddsPortal HTTP error: {response.status}"
            )

        try:
            await page.wait_for_load_state(
                "networkidle",
                timeout=20000
            )
        except PlaywrightTimeoutError:
            logging.warning(
                "Network idle timeout; continuing"
            )

        await page.wait_for_timeout(7000)

        title = await page.title()

        logging.info("Page title: %s", title)

        for page_no in range(MAX_PAGES):

            logging.info(
                "Reading page %s",
                page_no + 1
            )

            cards = await extract_cards(page)

            logging.info(
                "Page %s: found %s candidate blocks",
                page_no + 1,
                len(cards)
            )

            accepted = 0
            rejected = 0

            for card in cards:

                row = parse_card(card)

                if not row:
                    rejected += 1
                    continue

                key = (
                    row["match_url"],
                    row["market"],
                    row["selection"],
                    row["odds_new"]
                )

                if key in seen:
                    continue

                seen.add(key)
                rows.append(row)
                accepted += 1

            logging.info(
                "Page %s: accepted=%s rejected=%s",
                page_no + 1,
                accepted,
                rejected
            )

            # Diagnostic sample when parsing fails
            if not accepted and cards:

                logging.warning(
                    "SAMPLE CANDIDATE TEXT: %s",
                    clean(cards[0].get("text", ""))[:1200]
                )

            if page_no + 1 >= MAX_PAGES:
                break

            next_button = page.get_by_role(
                "button",
                name=re.compile(r"^Next$", re.I)
            )

            if await next_button.count() == 0:
                logging.info("Next button not found")
                break

            if not await next_button.first.is_enabled():
                logging.info("Next button disabled")
                break

            await next_button.first.click()
            await page.wait_for_timeout(4000)

        await browser.close()

    return rows


# ============================================================
# STATE
# ============================================================

def load_state():

    if not STATE_FILE.exists():
        return {}

    try:
        return json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )

    except (json.JSONDecodeError, OSError):
        return {}


# ============================================================
# SAVE CSV
# ============================================================

def save_results(rows):

    state = load_state()
    changed = []

    for row in rows:

        key = "|".join([
            row["match_url"],
            row["market"],
            row["selection"]
        ])

        signature = "|".join(
            str(row[k])
            for k in (
                "drop_pct",
                "odds_old",
                "odds_new",
                "best_current_odds"
            )
        )

        if state.get(key) != signature:

            changed.append(row)
            state[key] = signature

    # Latest data
    with LATEST_FILE.open(
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=FIELDS
        )

        writer.writeheader()
        writer.writerows(rows)

    # Historical changes
    if changed:

        exists = HISTORY_FILE.exists()

        with HISTORY_FILE.open(
            "a",
            newline="",
            encoding="utf-8-sig"
        ) as f:

            writer = csv.DictWriter(
                f,
                fieldnames=FIELDS
            )

            if not exists:
                writer.writeheader()

            writer.writerows(changed)

    STATE_FILE.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    logging.info(
        "TOTAL ROWS: %s",
        len(rows)
    )

    logging.info(
        "NEW OR CHANGED ROWS: %s",
        len(changed)
    )


# ============================================================
# MAIN
# ============================================================

async def main():

    logging.info("Starting OddsPortal parser")

    rows = await scrape()

    if not rows:

        raise RuntimeError(
            "No rows parsed. "
            "Check SAMPLE CANDIDATE TEXT above. "
            "The page may have changed or the odds layout "
            "does not match the current parser."
        )

    save_results(rows)

    logging.info(
        "Parser finished successfully"
    )


if __name__ == "__main__":

    asyncio.run(main())
