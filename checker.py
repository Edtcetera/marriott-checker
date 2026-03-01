"""
checker.py — Marriott price checking logic
Fetches all available rates in a single GraphQL call and returns them raw.
Configuration is loaded from /data/config.json (managed via the web UI).
"""

import json
import os
import logging
import re
import time
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/data/config.json")

GRAPHQL_QUERY = """fragment PhoenixBookDTTAmountFragment on MonetaryAmount {
  amount currency decimalPoint __typename
}
query PhoenixBookDTTSearchProductsByProperty($search: ProductByPropertySearchInput!, $offset: Int, $limit: Int) {
  commerce {
    product {
      searchProductsByProperty(search: $search, offset: $offset, limit: $limit) @contentError(options: {channel: "web", overrides: [{codesToReplace: ["fallback"], replaceWithCode: "WEB-BOOK-GEN001"}]}) {
        ... on ProductSearchByPropertyConnection {
          edges {
            node {
              ... on HotelRoom {
                id
                rates {
                  name
                  rateModes {
                    ... on HotelRoomRateModesCash {
                      averageNightlyRatePerUnit {
                        amount { ...PhoenixBookDTTAmountFragment __typename }
                        __typename
                      }
                      __typename
                    }
                    __typename
                  }
                  __typename
                }
                basicInformation {
                  ratePlan { ratePlanCode marketCode __typename }
                  type name description isMembersOnly depositRequired
                  freeCancellationUntil sourceOfRate __typename
                }
                __typename
              }
              id __typename
            }
            __typename
          }
          total __typename
        }
        __typename
      }
      __typename
    }
    __typename
  }
}"""


def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                return json.load(f)
        except Exception as e:
            log.error(f"Failed to load config: {e}")
    return {"hotels": [], "browser_cookies": ""}


def save_config(config: dict) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
    log.info("Config saved.")


def get_hotels() -> list[dict]:
    return load_config().get("hotels", [])


def get_browser_cookies() -> str:
    return load_config().get("browser_cookies", "")


def parse_price(amount_obj: dict) -> float | None:
    if not amount_obj:
        return None
    try:
        return float(amount_obj["amount"]) / (10 ** int(amount_obj.get("decimalPoint", 2)))
    except (KeyError, TypeError, ValueError):
        return None


def extract_customer_id(browser_cookies: str) -> str | None:
    try:
        token_match = re.search(r'UserIdToken=([^;]+)', browser_cookies)
        if not token_match:
            return None
        import base64
        parts   = token_match.group(1).split('.')
        padding = 4 - len(parts[1]) % 4
        payload = json.loads(base64.b64decode(parts[1] + '=' * padding))
        cust_id = payload.get('AltCustID')
        if cust_id:
            log.info(f"Extracted customerId: {cust_id}")
        return cust_id
    except Exception as e:
        log.warning(f"Could not extract customerId: {e}")
        return None


def fetch_all_prices(config: dict) -> list[dict]:
    """
    Fetch all available rates for a hotel in a single GraphQL call.
    Returns a flat list of room dicts, each with rate_name and price_per_night.
    """
    browser_cookies = get_browser_cookies()
    hotel_name      = config.get("name", config["property_code"])
    customer_id     = extract_customer_id(browser_cookies) if browser_cookies.strip() else None

    variables = {
        "search": {
            "options": {
                "startDate":         config["check_in"],
                "endDate":           config["check_out"],
                "quantity":          config["num_rooms"],
                "numberInParty":     config["adults"],
                "childAges":         [],
                "productRoomType":   ["ALL"],
                "productStatusType": ["AVAILABLE"],
                "rateRequestTypes": [
                    {"value": "",    "type": "STANDARD"},
                    {"value": "",    "type": "PREPAY"},
                    {"value": "",    "type": "PACKAGES"},
                    {"value": "MRM", "type": "CLUSTER"},
                    {"value": "AAA", "type": "AAA"},
                ],
                "isErsProperty":     False,
                "disabilityRequest": "ACCESSIBLE_AND_NON_ACCESSIBLE",
            },
            "propertyId": config["property_code"].upper(),
        },
        "offset": 0,
        "limit":  150,
    }
    if customer_id:
        variables["search"]["options"]["customerId"] = customer_id

    payload = {
        "operationName": "PhoenixBookDTTSearchProductsByProperty",
        "variables":     variables,
        "query":         GRAPHQL_QUERY,
    }

    rooms = []

    with sync_playwright() as p:
        log.info(f"[{hotel_name}] Launching Chromium...")
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )

        if browser_cookies.strip():
            log.info(f"[{hotel_name}] Injecting cookies...")
            cookie_list = []
            for part in browser_cookies.strip().split(";"):
                part = part.strip()
                if "=" in part:
                    name, _, value = part.partition("=")
                    cookie_list.append({"name": name.strip(), "value": value.strip(),
                                        "domain": ".marriott.com", "path": "/"})
            if cookie_list:
                context.add_cookies(cookie_list)

        page = context.new_page()
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins',   { get: () => [1, 2, 3] });
        """)

        warmup_url = (
            f"https://www.marriott.com/reservation/rateListMenu.mi"
            f"?propertyCode={config['property_code'].upper()}"
            f"&fromDate={config['check_in']}&toDate={config['check_out']}"
            f"&numberOfRooms={config['num_rooms']}&numberOfAdults={config['adults']}"
            f"&numberOfChildren=0&clusterCode=none&isSearch=true"
        )
        log.info(f"[{hotel_name}] Warming up...")
        try:
            page.goto(warmup_url, wait_until="domcontentloaded", timeout=25000)
            log.info(f"[{hotel_name}] Warm-up: '{page.title()[:60]}'")
            time.sleep(3)
        except PWTimeout:
            log.warning(f"[{hotel_name}] Warm-up timed out, continuing...")

        log.info(f"[{hotel_name}] Calling GraphQL (customerId={'yes' if customer_id else 'no'})...")
        try:
            result = page.evaluate("""
                async (payload) => {
                    const resp = await fetch("https://www.marriott.com/mi/query/PhoenixBookDTTSearchProductsByProperty", {
                        method: "POST", credentials: "include",
                        headers: {
                            "content-type": "application/json",
                            "accept": "*/*",
                            "apollographql-client-name": "phoenix_book",
                            "apollographql-client-version": "1",
                            "application-name": "book",
                            "graphql-force-safelisting": "true",
                            "graphql-require-safelisting": "true",
                            "graphql-operation-name": "PhoenixBookDTTSearchProductsByProperty",
                            "graphql-operation-signature": "a6e07eac0eafd7442668a026c453a5f9fa3964cee02ec45b6e07ad6bc792b260",
                            "dtt": "true", "dnt": "1",
                            "referer": "https://www.marriott.com/reservation/rateListMenu.mi",
                        },
                        body: JSON.stringify(payload),
                    });
                    return { status: resp.status, text: await resp.text() };
                }
            """, payload)

            status = result.get("status")
            text   = result.get("text", "")
            log.info(f"[{hotel_name}] HTTP {status}, {len(text)} chars")

            if status == 200:
                data  = json.loads(text)
                edges = (data.get("data", {}).get("commerce", {}).get("product", {})
                             .get("searchProductsByProperty", {}).get("edges", []))
                log.info(f"[{hotel_name}] {len(edges)} edges returned")

                for edge in edges:
                    node  = edge.get("node", {})
                    if node.get("__typename") != "HotelRoom":
                        continue
                    basic     = node.get("basicInformation", {})
                    rates     = node.get("rates", {})
                    modes     = rates.get("rateModes", {})
                    mode_type = modes.get("__typename", "")

                    if mode_type == "HotelRoomRateModesPoints":
                        pts_unit         = modes.get("pointsPerUnit", {})
                        points_per_night = pts_unit.get("points")  # int, already in pts units
                        price            = None
                        currency         = ""
                    elif mode_type == "HotelRoomRateModesCash":
                        avg              = modes.get("averageNightlyRatePerUnit", {})
                        price            = parse_price(avg.get("amount"))
                        currency         = avg.get("amount", {}).get("currency", "")
                        points_per_night = None
                    else:
                        continue  # unknown mode type, skip

                    if price is None and points_per_night is None:
                        continue

                    rate_plans = basic.get("ratePlan", [{}])
                    plan_code  = rate_plans[0].get("ratePlanCode", "") if rate_plans else ""
                    market     = rate_plans[0].get("marketCode", "")  if rate_plans else ""

                    rate_name   = rates.get("name", "")
                    deposit_req = basic.get("depositRequired", False)
                    free_cancel = basic.get("freeCancellationUntil")

                    # Infer refundability from rate name when Marriott doesn't populate
                    # freeCancellationUntil (common on availability searches vs. modify flows).
                    # Rates containing "flexible" are refundable; "prepay"/"advance"/"non-ref"
                    # indicate non-refundable. depositRequired=True also means non-refundable.
                    # Award (points) rooms are always refundable.
                    rate_lower = rate_name.lower()
                    if mode_type == "HotelRoomRateModesPoints":
                        is_refundable = True
                    elif free_cancel:
                        is_refundable = True
                    elif deposit_req:
                        is_refundable = False
                    elif any(kw in rate_lower for kw in ("prepay", "advance purchase", "non-refund", "non refund", "nonrefund")):
                        is_refundable = False
                    elif any(kw in rate_lower for kw in ("flexible", "flex", "refundable")):
                        is_refundable = True
                    else:
                        is_refundable = None  # unknown — don't assert either way

                    rooms.append({
                        "room_type_code":    basic.get("type", "").upper(),
                        "room_type_name":    basic.get("name", "Room"),
                        "room_desc":         basic.get("description", ""),
                        "rate_name":         rate_name,
                        "rate_plan_code":    plan_code,
                        "market_code":       market,
                        "price_per_night":   price,
                        "points_per_night":  points_per_night,
                        "currency":          currency,
                        "is_members_only":   basic.get("isMembersOnly", False),
                        "deposit_required":  deposit_req,
                        "free_cancellation": free_cancel,
                        "is_refundable":     is_refundable,
                    })

                log.info(f"[{hotel_name}] {len(rooms)} priced rooms found")
                cash_rooms   = [r for r in rooms if r["price_per_night"]   is not None]
                points_rooms = [r for r in rooms if r["points_per_night"]  is not None]
                for r in sorted(cash_rooms, key=lambda x: x["price_per_night"])[:5]:
                    log.info(f"  {r['currency']} ${r['price_per_night']:.2f} — {r['rate_name']} ({r['room_type_name']})")
                for r in sorted(points_rooms, key=lambda x: x["points_per_night"])[:3]:
                    log.info(f"  {r['points_per_night']:,} pts — {r['rate_name']} ({r['room_type_name']})")
            else:
                log.error(f"[{hotel_name}] HTTP {status}: {text[:400]}")

        except Exception as e:
            log.error(f"[{hotel_name}] Error: {e}")

        browser.close()

    return rooms


def find_best_match(rooms: list[dict], config: dict) -> dict | None:
    """Return the cheapest room matching the stay type and cancellation filter."""
    if not rooms:
        return None

    stay_type = config.get("stay_type", "cash")

    if stay_type == "award":
        # Award stays are always refundable — just pick cheapest by points
        candidates = [r for r in rooms if r.get("points_per_night") is not None]
        if not candidates:
            return None
        return min(candidates, key=lambda r: r["points_per_night"])

    # Cash path — filter by cancellation type then pick cheapest
    cancel_type = config.get("cancellation_type", "any")  # "any" | "refundable" | "nonrefundable"
    eligible    = [r for r in rooms if r.get("price_per_night") is not None]

    if cancel_type == "refundable":
        candidates = [r for r in eligible if r.get("is_refundable") is True]
    elif cancel_type == "nonrefundable":
        candidates = [r for r in eligible if r.get("is_refundable") is False]
    else:
        candidates = eligible

    if not candidates:
        return None
    return min(candidates, key=lambda r: r["price_per_night"])
