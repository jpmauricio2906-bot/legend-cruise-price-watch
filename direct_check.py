from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from curl_cffi import requests

BOOKING_URL = "https://www.royalcaribbean.com/room-selection/room-location?groupId=LE06FLL-4244356062&sailDate=2027-03-28&shipCode=LE&packageCode=LE06W296&destinationCode=CARIB&selectedCurrencyCode=USD&country=USA&cabinClassType=BALCONY&roomIndex=0&r0a=3&r0c=1&r0b=n&r0r=n&r0s=n&r0q=n&r0t=n&r0d=BALCONY&r0D=y&rgVisited=true&r0C=y&r0e=IB&r0f=IB&r0L=n&r0J=n"
ENDPOINT = "https://www.royalcaribbean.com/room-selection/type-and-subtype"
HISTORY = Path("price_history.csv")
DEBUG = Path("debug")
BASELINE_NET = Decimal("9735.70")
BASELINE_TOTAL = Decimal("10775.78")
GRATUITIES = Decimal("444.00")

PARAMS = {
    "groupId": "LE06FLL-4244356062",
    "sailDate": "2027-03-28",
    "shipCode": "LE",
    "packageCode": "LE06W296",
    "destinationCode": "CARIB",
    "selectedCurrencyCode": "USD",
    "country": "USA",
    "cabinClassType": "BALCONY",
    "roomIndex": "0",
    "r0a": "3",
    "r0c": "1",
    "r0b": "n",
    "r0r": "n",
    "r0s": "n",
    "r0q": "n",
    "r0t": "n",
    "r0d": "BALCONY",
    "r0D": "y",
    "rgVisited": "true",
    "r0C": "y",
    "r0e": "IB",
    "r0f": "IB",
    "r0L": "n",
    "r0J": "n",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:149.0) Gecko/20100101 Firefox/149.0",
    "Accept": "text/x-component",
    "Accept-Language": "en-US,en;q=0.9",
    "RSC": "1",
}

FIELDS = [
    "Date", "Cruise Fare", "Discounts", "Net Cruise Subtotal", "Taxes/Fees",
    "Website Total", "Comparable Total incl. Gratuities", "Difference vs Baseline",
    "Status", "Promotion/Notes", "Source URL",
]


def dec(v):
    if v is None:
        return None
    try:
        return Decimal(str(v).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError):
        return None


def fmt(v, signed=False):
    if v is None:
        return ""
    if signed and v > 0:
        return f"+${v:,.2f}"
    if v < 0:
        return f"-${abs(v):,.2f}"
    return f"${v:,.2f}"


def extract_array(text: str, key: str):
    m = re.search(rf'"{re.escape(key)}"\s*:\s*\[', text)
    if not m:
        return None
    start = text.find("[", m.start())
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i+1])
                except json.JSONDecodeError:
                    return None
    return None


def flatten(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            yield from flatten(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from flatten(v, f"{path}[{i}]")
    else:
        yield path, obj


def pick_amount(obj, keywords):
    vals = []
    for path, value in flatten(obj):
        p = path.lower().replace("_", "").replace("-", "")
        if any(k in p for k in keywords):
            v = dec(value)
            if v is not None and 0 <= v < Decimal("50000"):
                vals.append(v)
    if not vals:
        return None
    return max(vals)


def find_ib(rooms):
    candidates = []
    for room in rooms or []:
        for st in (room.get("options") or {}).get("stateroomTypes", []):
            for sub in st.get("stateroomSubtypes", []):
                blob = json.dumps(sub, ensure_ascii=False).lower()
                score = 0
                if "family infinite ocean view balcony" in blob or "family infinite oceanview balcony" in blob:
                    score += 20
                if re.search(r'"(?:code|categorycode|stateroomcategorycode|name|id)"\s*:\s*"ib"', blob):
                    score += 10
                if '"ib"' in blob:
                    score += 2
                inv = ((sub.get("pricing") or {}).get("invoice") or {})
                total = dec(inv.get("total"))
                if score and total is not None:
                    candidates.append((score, total, st, sub, inv))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (-x[0], x[1]))
    return candidates[0]


def save_row(row):
    rows = []
    if HISTORY.exists():
        with HISTORY.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    rows = [r for r in rows if r.get("Date") != row["Date"]]
    rows.append(row)
    rows.sort(key=lambda r: r.get("Date", ""))
    with HISTORY.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader(); w.writerows(rows)


def main():
    DEBUG.mkdir(exist_ok=True)
    today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    row = {k: "" for k in FIELDS}
    row["Date"] = today
    row["Status"] = "UNVERIFIED"
    row["Source URL"] = BOOKING_URL

    try:
        s = requests.Session(impersonate="chrome")
        # Seed Royal cookies and U.S. market before the RSC request.
        s.get("https://www.royalcaribbean.com/?country=USA", headers={"User-Agent": HEADERS["User-Agent"]}, timeout=30)
        r = s.get(ENDPOINT, params=PARAMS, headers=HEADERS, timeout=45)
        (DEBUG / "direct_status.txt").write_text(f"{r.status_code}\n{r.url}\n{r.headers}\n", encoding="utf-8")
        (DEBUG / "direct_response.txt").write_text(r.text, encoding="utf-8")
        r.raise_for_status()

        rooms = extract_array(r.text, "rooms")
        (DEBUG / "rooms.json").write_text(json.dumps(rooms, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        match = find_ib(rooms)
        if match:
            score, total, st, sub, inv = match
            (DEBUG / "ib_match.json").write_text(json.dumps({"score": score, "stateroomType": st, "subtype": sub, "invoice": inv}, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

            taxes = pick_amount(inv, ["tax", "portexpense", "fees"])
            gross = pick_amount(inv, ["cruisefare", "basefare", "grossfare"])
            discounts = pick_amount(inv, ["discount", "savings"])

            # Royal's invoice total from this endpoint is the full-party website total.
            website_total = total
            subtotal = website_total - taxes if taxes is not None else None
            if subtotal is None and gross is not None and discounts is not None:
                subtotal = gross - discounts
            comparable = website_total + GRATUITIES
            diff = comparable - BASELINE_TOTAL

            if subtotal is not None:
                status = "LOWER" if subtotal < BASELINE_NET else "HIGHER" if subtotal > BASELINE_NET else "SAME"
            else:
                status = "LOWER" if comparable < BASELINE_TOTAL else "HIGHER" if comparable > BASELINE_TOTAL else "SAME"

            row.update({
                "Cruise Fare": fmt(gross),
                "Discounts": fmt(-discounts if discounts is not None else None),
                "Net Cruise Subtotal": fmt(subtotal),
                "Taxes/Fees": fmt(taxes),
                "Website Total": fmt(website_total),
                "Comparable Total incl. Gratuities": fmt(comparable),
                "Difference vs Baseline": fmt(diff, signed=True),
                "Status": status,
                "Promotion/Notes": f"Royal Caribbean RSC endpoint; exact IB match score {score}. Invoice total is for 3 adults + 1 child in USD/USA market.",
            })
        else:
            row["Promotion/Notes"] = "Royal endpoint responded, but exact category IB could not be identified in the returned room inventory."
    except Exception as e:
        row["Promotion/Notes"] = f"Direct Royal pricing request failed: {type(e).__name__}: {e}"
        (DEBUG / "direct_error.txt").write_text(row["Promotion/Notes"], encoding="utf-8")

    save_row(row)
    print(json.dumps(row, indent=2))
    return 0 if row["Status"] != "UNVERIFIED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
