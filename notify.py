"""
notify.py — Home Assistant notification integration.
Sends push notifications via HA's notify service using a long-lived access token.
"""

import logging
import requests

log = logging.getLogger(__name__)


def _ha_notify(ha_url: str, token: str, service: str, title: str, message: str) -> bool:
    """POST to HA notify service. Returns True on success."""
    url = f"{ha_url.rstrip('/')}/api/services/notify/{service}"
    try:
        resp = requests.post(
            url,
            json={"title": title, "message": message},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type":  "application/json",
            },
            timeout=10,
        )
        if resp.status_code in (200, 201):
            log.info(f"[HA] Notification sent: {title}")
            return True
        else:
            log.error(f"[HA] Notify failed: HTTP {resp.status_code} — {resp.text[:200]}")
            return False
    except Exception as e:
        log.error(f"[HA] Notify error: {e}")
        return False


def send_cheaper_rate_alert(cfg: dict, hotel_result: dict) -> None:
    """Send an alert when a cheaper same-category rate is found."""
    ha_url     = cfg.get("ha_url", "").strip()
    token      = cfg.get("ha_token", "").strip()
    service    = cfg.get("ha_service", "notify").strip() or "notify"
    if not ha_url or not token:
        return

    h          = hotel_result
    currency   = h["currency"]
    savings_pn = h["best_diff"]
    savings_t  = h["best_total"]
    pct        = h["best_pct"]
    nights     = h["num_nights"]

    title   = f"🏨 Cheaper rate found — {h['name']}"
    message = (
        f"{h['best_name']}\n"
        f"{currency} ${h['best_price']:.2f}/night  (↓ {pct:.1f}% vs your {currency} ${h['original']:.2f})\n"
        f"Saves {currency} ${savings_pn:.2f}/night · {currency} ${savings_t:.2f} over {nights} night{'s' if nights != 1 else ''}\n"
        f"Check-in {h['check_in']}  →  {h['check_out']}"
    )
    _ha_notify(ha_url, token, service, title, message)


def send_summary(cfg: dict, results: list, last_run: str) -> None:
    """Send a summary notification after every check.
    Only lists hotels where a cheaper rate was found to keep the message short.
    """
    ha_url  = cfg.get("ha_url", "").strip()
    token   = cfg.get("ha_token", "").strip()
    service = cfg.get("ha_service", "notify").strip() or "notify"
    if not ha_url or not token:
        return

    drops = []
    for h in results:
        if h.get("best_diff") is not None and h["best_diff"] > 0:
            currency = h["currency"]
            drops.append(
                f"• {h['name']}: ↓{h['best_pct']:.1f}%"
                f" ({currency} ${h['best_total']:.2f} savings)"
            )

    if drops:
        title   = f"🏨 Cheaper rates found ({len(drops)})"
        message = "\n".join(drops)
    else:
        title   = "🏨 Marriott Check"
        message = f"All booked rates are still the best ✓\n{last_run}"

    _ha_notify(ha_url, token, service, title, message)
