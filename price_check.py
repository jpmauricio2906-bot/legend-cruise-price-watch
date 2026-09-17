from __future__ import annotations

import csv
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from playwright.sync_api import sync_playwright

BOOKING_URL = (
    "https://www.royalcaribbean.com/room-selection/room-location?"
    "groupId=LE06FLL-4244356062&sailDate=2027-03-28&shipCode=LE&"
    "packageCode=LE06W296&destinationCode=CARIB&selectedCurrencyCode=USD&"
    "country=USA&cabinClassType=BALCONY&roomIndex=0&r0a=3&r0c=1&"
    "r0b=n&r0r=n&r0s=n&r0q=n&r0t=n&r0d=BALCONY&r0D=y&rgVisited=true&"
    "r0C=y&r0e=IB&r0f=IB&r0L=n&r0J=n"
)

BASELINE_CRUISE_FARE = Decimal("13880.70")
BASELINE_DISCOUNTS = Decimal("4145.00")
BASELINE_NET_SUBTOTAL = Decimal("9735.70")
BASELINE_TAXES = Decimal("596.08")
PREPAID_GRATUITIES = Decimal("444.00")
BASELINE_TOTAL = Decimal("10775.78")

HISTORY_FILE = Path(os.getenv("PRICE_HISTORY_FILE", "price_history.csv"))
DEBUG_DIR = Path(os.getenv("DEBUG_DIR", "debug"))


@dataclass
class Observation:
    date: str
    cruise_fare: str = ""
    discounts: str = ""
    net_cruise_subtotal: str = ""
    taxes_fees: str = ""
    website_total: str = ""
    comparable_total_incl_gratuities: str = ""
    difference_vs_baseline: str = ""
    status: str = "UNVERIFIED"
    promotion_notes: str = ""
    source_url: str = BOOKING_URL


MONEY_RE = re.compile(r"\$?\s*([0-9][0-9,]*\.[0-9]{2})")


def money(value: str | Decimal | float | int | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).replace("$", "").replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def fmt(value: Decimal | None, signed: bool = False) -> str:
    if value is None:
        return ""
    if signed:
        sign = "+" if value > 0 else ""
        return f"{sign}${value:,.2f}"
    if value < 0:
        return f"-${abs(value):,.2f}"
    return f"${value:,.2f}"


def flatten(obj: Any, path: str = "") -> Iterable[tuple[str, Any]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            yield from flatten(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from flatten(v, f"{path}[{i}]")
    else:
        yield path, obj


def first_money_near_label(text: str, labels: list[str]) -> Decimal | None:
    lower = text.lower()
    for label in labels:
        start = 0
        needle = label.lower()
        while True:
            idx = lower.find(needle, start)
            if idx < 0:
                break
            line_end = text.find("\n", idx)
            if line_end < 0:
                line_end = min(len(text), idx + 250)
            window = text[idx : min(len(text), max(line_end, idx + 250))]
            m = MONEY_RE.search(window)
            if m:
                return money(m.group(1))
            start = idx + len(needle)
    return None


def infer_from_json(candidates: list[Any]) -> dict[str, Decimal | None]:
    wanted = {
        "cruise_fare": ["cruisefare", "basefare", "grossfare"],
        "discounts": ["discount", "savings"],
        "net_cruise_subtotal": ["subtotal", "netfare", "netcruisefare"],
        "taxes_fees": ["tax", "portexpense", "fees"],
        "website_total": ["totalprice", "grandtotal", "totalamount", "carttotal"],
    }
    found: dict[str, list[Decimal]] = {k: [] for k in wanted}

    for payload in candidates:
        for path, value in flatten(payload):
            p = path.lower().replace("_", "").replace("-", "")
            val = money(value)
            if val is None or val <= 0:
                continue
            for field, keys in wanted.items():
                if any(key in p for key in keys):
                    found[field].append(val)

    result: dict[str, Decimal | None] = {k: None for k in wanted}
    for field, vals in found.items():
        unique = sorted(set(vals))
        if not unique:
            continue
        target = {
            "cruise_fare": BASELINE_CRUISE_FARE,
            "discounts": BASELINE_DISCOUNTS,
            "net_cruise_subtotal": BASELINE_NET_SUBTOTAL,
            "taxes_fees": BASELINE_TAXES,
            "website_total": BASELINE_TOTAL - PREPAID_GRATUITIES,
        }[field]
        result[field] = min(unique, key=lambda x: abs(x - target))
    return result


def extract_promotions(text: str) -> str:
    hits = []
    patterns = [
        r"BOGO\s*60[^\n,;]*",
        r"Savings\s+NRD",
        r"Kicker\s+NRD",
        r"Early Booking[^\n,;]*",
        r"Crown\s*&\s*Anchor[^\n,;]*",
        r"Double Points[^\n,;]*",
        r"Kids Sail Free[^\n,;]*",
    ]
    for pat in patterns:
        for m in re.finditer(pat, text, flags=re.I):
            val = re.sub(r"\s+", " ", m.group(0)).strip(" -:")
            if val and val.lower() not in {x.lower() for x in hits}:
                hits.append(val)
    return "; ".join(hits[:8])


def derive_observation(text: str, json_payloads: list[Any]) -> Observation:
    today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    j = infer_from_json(json_payloads)

    cruise_fare = first_money_near_label(
        text, ["Cruise Fare", "Cruise fare", "Original cruise fare", "Fare"]
    ) or j["cruise_fare"]

    discounts = first_money_near_label(
        text, ["Discounts", "Savings", "Total savings", "Promo savings"]
    ) or j["discounts"]

    subtotal = first_money_near_label(
        text, ["Net cruise subtotal", "Cruise subtotal", "Subtotal"]
    ) or j["net_cruise_subtotal"]

    taxes = first_money_near_label(
        text,
        [
            "Taxes, fees, and port expenses",
            "Taxes, fees & port expenses",
            "Taxes and fees",
            "Taxes & fees",
        ],
    ) or j["taxes_fees"]

    website_total = first_money_near_label(
        text, ["Total price", "Trip total", "Total", "Amount due"]
    ) or j["website_total"]

    if subtotal is not None and taxes is not None:
        computed = subtotal + taxes
        if website_total is None or abs(website_total - computed) > Decimal("5.00"):
            website_total = computed

    if subtotal is None and cruise_fare is not None and discounts is not None:
        subtotal = cruise_fare - discounts

    if website_total is not None and subtotal is not None and taxes is None:
        candidate = website_total - subtotal
        if Decimal("0") < candidate < Decimal("3000"):
            taxes = candidate

    promos = extract_promotions(text)

    valid = subtotal is not None and Decimal("4000") < subtotal < Decimal("25000")
    if taxes is not None:
        valid = valid and Decimal("100") < taxes < Decimal("3000")
    if website_total is not None:
        valid = valid and Decimal("4000") < website_total < Decimal("30000")

    if not valid:
        note = "Could not reliably extract an exact 4-person category-IB USD quote from Royal Caribbean."
        if promos:
            note += f" Promotions seen: {promos}."
        return Observation(date=today, promotion_notes=note)

    comparable_total = website_total + PREPAID_GRATUITIES if website_total is not None else None
    diff = (comparable_total - BASELINE_TOTAL) if comparable_total is not None else (subtotal - BASELINE_NET_SUBTOTAL)

    if subtotal < BASELINE_NET_SUBTOTAL:
        status = "LOWER"
    elif subtotal > BASELINE_NET_SUBTOTAL:
        status = "HIGHER"
    else:
        status = "SAME"

    return Observation(
        date=today,
        cruise_fare=fmt(cruise_fare),
        discounts=fmt(-discounts if discounts is not None else None),
        net_cruise_subtotal=fmt(subtotal),
        taxes_fees=fmt(taxes),
        website_total=fmt(website_total),
        comparable_total_incl_gratuities=fmt(comparable_total),
        difference_vs_baseline=fmt(diff, signed=True),
        status=status,
        promotion_notes=promos or "Exact U.S./USD category-IB quote checked on Royal Caribbean.",
    )


def update_history(obs: Observation) -> None:
    fields = [
        "Date",
        "Cruise Fare",
        "Discounts",
        "Net Cruise Subtotal",
        "Taxes/Fees",
        "Website Total",
        "Comparable Total incl. Gratuities",
        "Difference vs Baseline",
        "Status",
        "Promotion/Notes",
        "Source URL",
    ]
    row = {
        "Date": obs.date,
        "Cruise Fare": obs.cruise_fare,
        "Discounts": obs.discounts,
        "Net Cruise Subtotal": obs.net_cruise_subtotal,
        "Taxes/Fees": obs.taxes_fees,
        "Website Total": obs.website_total,
        "Comparable Total incl. Gratuities": obs.comparable_total_incl_gratuities,
        "Difference vs Baseline": obs.difference_vs_baseline,
        "Status": obs.status,
        "Promotion/Notes": obs.promotion_notes,
        "Source URL": obs.source_url,
    }

    rows: list[dict[str, str]] = []
    if HISTORY_FILE.exists():
        with HISTORY_FILE.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

    rows = [r for r in rows if r.get("Date") != obs.date]
    rows.append(row)
    rows.sort(key=lambda r: r.get("Date", ""))

    with HISTORY_FILE.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    captured_json: list[Any] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = browser.new_context(
            locale="en-US",
            timezone_id="America/New_York",
            viewport={"width": 1440, "height": 1200},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
            ),
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        page = context.new_page()

        def capture_response(response):
            u = response.url.lower()
            if not any(k in u for k in ["price", "pricing", "checkout", "room", "stateroom", "cart", "offer"]):
                return
            ctype = (response.headers.get("content-type") or "").lower()
            if "json" not in ctype:
                return
            try:
                captured_json.append(response.json())
            except Exception:
                pass

        page.on("response", capture_response)

        try:
            page.goto("https://www.royalcaribbean.com/?country=USA", wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(3000)
            page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=90000)
            try:
                page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:
                pass
            page.wait_for_timeout(8000)

            for label in ["Accept", "Accept All", "I Agree", "Got it", "Close"]:
                try:
                    page.get_by_role("button", name=re.compile(f"^{re.escape(label)}$", re.I)).first.click(timeout=1200)
                except Exception:
                    pass

            text = page.locator("body").inner_text(timeout=15000)
            (DEBUG_DIR / "page_text.txt").write_text(text, encoding="utf-8")
            (DEBUG_DIR / "captured_json.json").write_text(json.dumps(captured_json, indent=2, default=str), encoding="utf-8")
            page.screenshot(path=str(DEBUG_DIR / "page.png"), full_page=True)
        except Exception as exc:
            text = f"Browser exception: {exc}"
            (DEBUG_DIR / "page_text.txt").write_text(text, encoding="utf-8")
        finally:
            browser.close()

    obs = derive_observation(text, captured_json)
    update_history(obs)
    print(json.dumps(asdict(obs), indent=2))
    return 0 if obs.status != "UNVERIFIED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
