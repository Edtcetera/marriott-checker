#!/usr/bin/env python3
"""Marriott Price Checker — Web Dashboard"""

import json
import threading
import time
from datetime import datetime, timedelta
from flask import Flask, render_template_string, jsonify, request
from checker import load_config, save_config, get_hotels, get_browser_cookies, fetch_all_prices, find_best_match
from notify  import send_cheaper_rate_alert, send_summary

app = Flask(__name__)

state = {"status": "idle", "last_run": None, "next_check": None, "results": [], "error": None}
state_lock = threading.Lock()


def run_checks():
    with state_lock:
        state["status"] = "checking"
        state["error"]  = None

    results = []
    try:
        for config in get_hotels():
            rooms      = fetch_all_prices(config)
            best       = find_best_match(rooms, config)
            original   = config["original_rate_per_night"]
            ci         = datetime.strptime(config["check_in"],  "%Y-%m-%d")
            co         = datetime.strptime(config["check_out"], "%Y-%m-%d")
            num_nights = (co - ci).days

            best_diff  = (original - best["price_per_night"]) if best else None
            best_pct   = ((best_diff / original) * 100)       if best_diff is not None else None
            best_total = (best_diff * num_nights)              if best_diff is not None else None

            # Find cheapest rate in the OTHER cancellation categories
            cancel_type = config.get("cancellation_type", "any")
            other_bests = []
            other_cats  = []
            if cancel_type == "refundable":
                other_cats = [("nonrefundable", "Non-refundable")]
            elif cancel_type == "nonrefundable":
                other_cats = [("refundable", "Refundable")]
            elif cancel_type == "any":
                other_cats = [("refundable", "Refundable"), ("nonrefundable", "Non-refundable")]

            for cat_key, cat_label in other_cats:
                alt_config = {**config, "cancellation_type": cat_key}
                alt_best   = find_best_match(rooms, alt_config)
                if alt_best:
                    alt_diff  = original - alt_best["price_per_night"]
                    alt_pct   = (alt_diff / original * 100) if original else 0
                    alt_total = alt_diff * num_nights
                    other_bests.append({
                        "label":      cat_label,
                        "price":      alt_best["price_per_night"],
                        "rate_name":  alt_best["rate_name"],
                        "diff":       alt_diff,
                        "pct":        alt_pct,
                        "total":      alt_total,
                    })

            # Build sorted rate rows — deduplicate by (rate_name, room_type_code), keep cheapest
            seen   = {}
            for r in rooms:
                key = (r["rate_name"], r["room_type_code"])
                if key not in seen or r["price_per_night"] < seen[key]["price_per_night"]:
                    seen[key] = r
            rate_rows = sorted(seen.values(), key=lambda r: r["price_per_night"])

            # Annotate each row with savings vs original
            annotated = []
            for r in rate_rows:
                diff = original - r["price_per_night"]
                pct  = (diff / original * 100) if original else 0
                annotated.append({**r, "diff": diff, "pct": pct})

            currency      = config.get("currency", "CAD")
            cancel_labels = {"any": "Any", "refundable": "Refundable only", "nonrefundable": "Non-refundable only"}
            results.append({
                "name":           config.get("name", config["property_code"].upper()),
                "property_code":  config["property_code"].upper(),
                "check_in":       config["check_in"],
                "check_out":      config["check_out"],
                "num_nights":     num_nights,
                "adults":         config["adults"],
                "original":       original,
                "currency":       currency,
                "cancel_type":    cancel_type,
                "cancel_label":   cancel_labels.get(cancel_type, "Any"),
                "best_price":     best["price_per_night"] if best else None,
                "best_name":      best["rate_name"]       if best else None,
                "best_diff":      best_diff,
                "best_pct":       best_pct,
                "best_total":     best_total,
                "other_bests":    other_bests,
                "rate_rows":      annotated,
            })

        last_run     = datetime.now()
        cfg          = load_config()
        interval_hrs = float(cfg.get("schedule_hours", 3))
        # next_check is based on when the check STARTED so the countdown
        # reflects the actual scheduler interval regardless of check duration
        next_check   = last_run + timedelta(hours=interval_hrs)
        with state_lock:
            state["status"]         = "done"
            state["last_run"]       = last_run.strftime("%Y-%m-%d %H:%M:%S")
            state["last_run_epoch"] = int(last_run.timestamp() * 1000)
            state["schedule_hours"] = interval_hrs
            state["next_check"]     = next_check.strftime("%Y-%m-%d %H:%M:%S")
            state["results"]        = results
        last_run = last_run.strftime("%Y-%m-%d %H:%M:%S")

        # Send HA notifications
        cfg = load_config()
        for h in results:
            if h.get("best_diff") is not None and h["best_diff"] > 0:
                send_cheaper_rate_alert(cfg, h)
        send_summary(cfg, results, last_run)

    except Exception as e:
        with state_lock:
            state["status"] = "error"
            state["error"]  = str(e)


@app.route("/")
def index():
    with state_lock:
        s = dict(state)
    cfg = load_config()
    return render_template_string(DASHBOARD, state=s, hotels=cfg.get("hotels", []))


@app.route("/settings")
def settings():
    return render_template_string(SETTINGS, config=load_config())


@app.route("/api/config", methods=["GET"])
def api_get_config():
    return jsonify(load_config())


@app.route("/api/config", methods=["POST"])
def api_save_config():
    try:
        data = request.get_json()
        for h in data.get("hotels", []):
            datetime.strptime(h["check_in"],  "%Y-%m-%d")
            datetime.strptime(h["check_out"], "%Y-%m-%d")
            assert float(h["original_rate_per_night"]) > 0
        save_config(data)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/notify/test", methods=["POST"])
def notify_test():
    from notify import _ha_notify
    cfg     = load_config()
    ha_url  = cfg.get("ha_url",     "").strip()
    token   = cfg.get("ha_token",   "").strip()
    service = cfg.get("ha_service", "notify").strip() or "notify"
    if not ha_url or not token:
        return jsonify({"ok": False, "error": "HA URL or token not configured"}), 400
    ok = _ha_notify(ha_url, token, service,
                    "🏨 Marriott Checker — Test",
                    "Home Assistant notifications are working correctly!")
    return jsonify({"ok": ok, "error": None if ok else "Check logs for details"})


@app.route("/check", methods=["POST"])
def check():
    with state_lock:
        if state["status"] == "checking":
            return jsonify({"ok": False, "msg": "Already running"}), 409
        if not get_hotels():
            return jsonify({"ok": False, "msg": "No hotels configured"}), 400
    threading.Thread(target=run_checks, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/status")
def status():
    with state_lock:
        return jsonify(dict(state))


CSS = """
:root{--bg:#0f1117;--card:#1a1d27;--border:#2a2d3a;--accent:#c8a96e;
  --green:#2ecc71;--red:#e74c3c;--text:#e8e8e8;--muted:#7a7d8a;--radius:12px;--input-bg:#12151f;}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;min-height:100vh;}
header{background:var(--card);border-bottom:1px solid var(--border);padding:16px 32px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:10;}
header h1{font-size:1.1rem;font-weight:600;color:var(--accent);}
.nav-links{display:flex;gap:8px;}
.btn{background:transparent;color:var(--muted);border:1px solid var(--border);border-radius:8px;padding:8px 16px;font-size:0.85rem;cursor:pointer;text-decoration:none;transition:all 0.2s;display:inline-flex;align-items:center;gap:6px;}
.btn:hover{color:var(--text);border-color:var(--accent);}
.btn.primary{background:var(--accent);color:#000;border-color:var(--accent);font-weight:600;}
.btn.primary:hover{opacity:0.85;}
.btn:disabled{opacity:0.4;cursor:not-allowed;}
.container{max-width:1000px;margin:0 auto;padding:32px 24px;}
input[type=text],input[type=date],input[type=number],textarea{background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:9px 12px;font-size:0.875rem;width:100%;outline:none;transition:border-color 0.2s;font-family:inherit;}
input:focus,textarea:focus{border-color:var(--accent);}
textarea{resize:vertical;}
label{font-size:0.78rem;color:var(--muted);display:block;margin-bottom:5px;}
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;margin-bottom:20px;}
.card-header{padding:16px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;}
.card-header h2{font-size:0.95rem;font-weight:600;}
.card-body{padding:20px;}
.toast{position:fixed;bottom:24px;right:24px;background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px 18px;font-size:0.875rem;box-shadow:0 4px 20px rgba(0,0,0,0.4);z-index:100;display:none;align-items:center;gap:10px;}
.toast.show{display:flex;}.toast.success{border-color:var(--green);}.toast.error{border-color:var(--red);}
"""

DASHBOARD = """<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Marriott Price Checker</title><style>""" + CSS + """
.status-bar{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:13px 18px;margin-bottom:24px;display:flex;align-items:center;gap:12px;font-size:0.875rem;}
.spinner{width:15px;height:15px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin 0.7s linear infinite;flex-shrink:0;}
@keyframes spin{to{transform:rotate(360deg);}}
.dot{width:9px;height:9px;border-radius:50%;flex-shrink:0;}
.dot.green{background:var(--green);}.dot.red{background:var(--red);}.dot.grey{background:var(--muted);}
.hotel-card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;margin-bottom:24px;}
.hotel-card.drop{border-color:var(--green);}.hotel-card.higher{border-color:var(--red);}
.hotel-header{padding:16px 20px;border-bottom:1px solid var(--border);display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap;}
.hotel-title{font-size:1rem;font-weight:600;margin-bottom:3px;}
.hotel-meta{font-size:0.78rem;color:var(--muted);}
.badge{font-size:0.72rem;font-weight:700;padding:3px 10px;border-radius:20px;white-space:nowrap;}
.badge.drop{background:rgba(46,204,113,0.15);color:var(--green);}
.badge.higher{background:rgba(231,76,60,0.15);color:var(--red);}
.badge.same{background:rgba(122,125,138,0.15);color:var(--muted);}
.badge.alt-cheaper{background:rgba(52,152,219,0.15);color:#5dade2;font-weight:600;}
.badge.alt-higher{background:rgba(122,125,138,0.1);color:var(--muted);}
.hotel-body{padding:0;}
.booked-bar{padding:14px 20px;background:rgba(200,169,110,0.06);border-bottom:1px solid var(--border);font-size:0.83rem;color:var(--muted);}
.booked-bar span{color:var(--text);font-weight:600;}
.filter-bar{padding:10px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px;flex-wrap:wrap;background:rgba(255,255,255,0.02);}
.filter-bar label{font-size:0.78rem;color:var(--muted);margin:0;}
.filter-btn{background:transparent;color:var(--muted);border:1px solid var(--border);border-radius:6px;padding:4px 12px;font-size:0.78rem;cursor:pointer;transition:all 0.15s;}
.filter-btn:hover{color:var(--text);border-color:var(--accent);}
.filter-btn.active{background:rgba(200,169,110,0.15);color:var(--accent);border-color:var(--accent);}
.rates-table{width:100%;border-collapse:collapse;font-size:0.845rem;}
.rates-table th{text-align:left;color:var(--muted);font-weight:500;padding:8px 16px;border-bottom:1px solid var(--border);font-size:0.78rem;text-transform:uppercase;letter-spacing:0.04em;white-space:nowrap;user-select:none;}
.rates-table th.sortable{cursor:pointer;}
.rates-table th.sortable:hover{color:var(--text);}
.rates-table th.sort-asc::after{content:" ↑";color:var(--accent);}
.rates-table th.sort-desc::after{content:" ↓";color:var(--accent);}
.rates-table th:not(:first-child){text-align:right;}
.rates-table td{padding:11px 16px;border-bottom:1px solid var(--border);vertical-align:middle;}
.rates-table td:not(:first-child){text-align:right;}
.rates-table tr:last-child td{border-bottom:none;}
.rates-table tr.best-row td:first-child::before{content:"⭐ ";}
.rate-name{font-weight:500;}
.rate-sub{font-size:0.75rem;color:var(--muted);margin-top:2px;}
.tag{display:inline-block;font-size:0.68rem;padding:1px 6px;border-radius:4px;margin-left:4px;vertical-align:middle;}
.tag.member{background:rgba(200,169,110,0.15);color:var(--accent);}
.tag.deposit{background:rgba(231,76,60,0.12);color:var(--red);}
.tag.refundable{background:rgba(46,204,113,0.12);color:var(--green);}
.saving{color:var(--green);font-weight:600;}.losing{color:var(--red);}.neutral{color:var(--muted);}
.best-deal{margin:0;padding:14px 20px;background:rgba(46,204,113,0.07);border-top:1px solid rgba(46,204,113,0.2);font-size:0.855rem;}
.best-deal strong{color:var(--green);}
.no-rates{padding:32px 20px;text-align:center;color:var(--muted);font-size:0.875rem;}
.idle-msg,.no-hotels{text-align:center;color:var(--muted);padding:60px 20px;}
.idle-msg .big,.no-hotels .big{font-size:2.5rem;margin-bottom:12px;}
.hidden-row{display:none;}
.no-match{display:none;padding:20px;text-align:center;color:var(--muted);font-size:0.85rem;}
.hotel-header{cursor:pointer;transition:background 0.15s;}
.hotel-header:hover{background:rgba(255,255,255,0.03);}
.collapse-icon{font-size:0.75rem;color:var(--muted);transition:transform 0.2s;flex-shrink:0;}
.collapsed .collapse-icon{transform:rotate(-90deg);}
.collapsed .hotel-collapsible{display:none;}
</style></head><body>
<header>
  <h1>🏨 Marriott Price Checker</h1>
  <div style="display:flex;align-items:center;gap:16px;">
    {% if state.last_run %}<span style="font-size:0.78rem;color:var(--muted)">Last: {{ state.last_run }}</span>{% endif %}
    {% if state.last_run_epoch and state.status != 'checking' %}
    <span style="font-size:0.78rem;color:var(--muted)">Next: <span id="countdown"
      data-last-run="{{ state.last_run_epoch }}"
      data-interval-hours="{{ state.schedule_hours or 3 }}"
      style="color:var(--accent);font-variant-numeric:tabular-nums;">—</span></span>
    {% endif %}
    <div class="nav-links">
      <a href="/settings" class="btn">⚙️ Settings</a>
      <button class="btn primary" id="checkBtn" onclick="startCheck()"
        {% if state.status=='checking' or not hotels %}disabled{% endif %}>
        {% if state.status=='checking' %}Checking…{% else %}Check Now{% endif %}
      </button>
    </div>
  </div>
</header>
<div class="container">
  <div class="status-bar">
    {% if state.status=='checking' %}<div class="spinner"></div> Fetching all available rates for {{ hotels|length }} hotel(s)…
    {% elif state.status=='done' %}<div class="dot green"></div> Completed — {{ state.results|length }} hotel(s) checked
    {% elif state.status=='error' %}<div class="dot red"></div> Error: {{ state.error }}
    {% else %}<div class="dot grey"></div>
      {% if not hotels %}No hotels configured — <a href="/settings" style="color:var(--accent)">go to Settings</a> to add your reservations.
      {% else %}Press <strong>Check Now</strong> to fetch all available Marriott rates.{% endif %}
    {% endif %}
  </div>

  {% if not hotels %}
  <div class="no-hotels"><div class="big">🏨</div>No reservations added yet.<br><br>
    <a href="/settings" class="btn primary" style="display:inline-flex;">⚙️ Add Your Hotels</a></div>

  {% elif state.status=='done' and state.results %}
  {% for h in state.results %}
  {% set has_cheaper = h.best_diff is not none and h.best_diff > 0 %}
  <div class="hotel-card {% if has_cheaper %}higher{% else %}drop{% endif %} collapsed" id="hotel-{{ loop.index }}">
    <div class="hotel-header" onclick="toggleCollapse('hotel-{{ loop.index }}')">
      <div style="flex:1;min-width:0;">
        <div class="hotel-title">{{ h.name }}</div>
        <div class="hotel-meta">{{ h.property_code }} &nbsp;·&nbsp; {{ h.check_in }} → {{ h.check_out }} ({{ h.num_nights }} night{% if h.num_nights!=1 %}s{% endif %}) &nbsp;·&nbsp; {{ h.adults }} adult{% if h.adults!=1 %}s{% endif %}</div>
      </div>
      <div style="display:flex;align-items:center;gap:10px;flex-shrink:0;">
      <div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;justify-content:flex-end;">
      {% if h.best_price is none %}<span class="badge same">No Data</span>
      {% elif has_cheaper %}<span class="badge higher">↓ {{ "%.1f"|format(h.best_pct) }}% cheaper {{ h.cancel_label | lower }} — rebook</span>
      {% else %}<span class="badge drop">✓ Best {{ h.cancel_label | lower }} rate</span>{% endif %}
      {% for ob in h.other_bests %}
        {% if ob.diff > 0 %}
          <span class="badge alt-cheaper" title="{{ ob.rate_name }}">{{ ob.label }}: ↓ {{ "%.1f"|format(ob.pct) }}% · saves {{ h.currency }} ${{ "%.0f"|format(ob.total) }} trip</span>
        {% elif ob.diff <= 0 %}
          <span class="badge alt-higher" title="{{ ob.rate_name }}">{{ ob.label }}: ↑ {{ "%.1f"|format(-ob.pct) }}% pricier</span>
        {% endif %}
      {% endfor %}
      </div>
      <span class="collapse-icon">▼</span>
      </div>
    </div>
    <div class="hotel-collapsible">
    <div class="booked-bar">Your booked rate: <span>{{ h.currency }} ${{ "%.2f"|format(h.original) }} / night</span>
      &nbsp;·&nbsp; {{ h.num_nights }} night{% if h.num_nights!=1 %}s{% endif %}
      &nbsp;·&nbsp; Total: <span>{{ h.currency }} ${{ "%.2f"|format(h.original * h.num_nights) }}</span>
      &nbsp;·&nbsp; Comparing: <span>{{ h.cancel_label }}</span>
    </div>

    {% if h.rate_rows %}
    <div class="filter-bar" id="filters-{{ loop.index }}">
      <label>Filter:</label>
      <button class="filter-btn active" data-hotel="{{ loop.index }}" data-filter="all" onclick="setFilter(this)">All</button>
      <button class="filter-btn" data-hotel="{{ loop.index }}" data-filter="refundable" onclick="setFilter(this)">✅ Free Cancellation</button>
      <button class="filter-btn" data-hotel="{{ loop.index }}" data-filter="nonrefundable" onclick="setFilter(this)">🔒 Non-refundable</button>
      <button class="filter-btn" data-hotel="{{ loop.index }}" data-filter="member" onclick="setFilter(this)">⭐ Member Only</button>
    </div>
    <div class="hotel-body">
      <table class="rates-table" id="table-{{ loop.index }}">
        <thead><tr>
          <th class="sortable" data-hotel="{{ loop.index }}" data-col="rate_name" onclick="sortTable(this)">Rate</th>
          <th class="sortable" data-hotel="{{ loop.index }}" data-col="room_type_name" onclick="sortTable(this)">Room Type</th>
          <th class="sortable sort-asc" data-hotel="{{ loop.index }}" data-col="price_per_night" onclick="sortTable(this)">Price/night</th>
          <th class="sortable" data-hotel="{{ loop.index }}" data-col="pct" onclick="sortTable(this)">vs Yours</th>
          <th class="sortable" data-hotel="{{ loop.index }}" data-col="diff" onclick="sortTable(this)">Save/night</th>
          <th class="sortable" data-hotel="{{ loop.index }}" data-col="total_savings" onclick="sortTable(this)">Trip Savings</th>
          <th>Cancellation</th>
        </tr></thead>
        <tbody id="tbody-{{ loop.index }}">
        {% for r in h.rate_rows %}
        <tr data-rate="{{ r.rate_name | e }}"
            data-room="{{ r.room_type_name | e }}"
            data-price="{{ r.price_per_night }}"
            data-pct="{{ r.pct }}"
            data-diff="{{ r.diff }}"
            data-total="{{ r.diff * h.num_nights }}"
            data-member="{{ 'true' if r.is_members_only else 'false' }}"
            data-deposit="{{ 'true' if r.deposit_required else 'false' }}"
            data-refundable="{{ 'true' if r.is_refundable else ('false' if r.is_refundable == false else 'unknown') }}"
            {% if h.best_name == r.rate_name and h.best_price == r.price_per_night %}class="best-row"{% endif %}>
          <td>
            <div class="rate-name">{{ r.rate_name }}
              {% if r.is_members_only %}<span class="tag member">Member</span>{% endif %}
              {% if r.deposit_required %}<span class="tag deposit">Deposit</span>{% endif %}
            </div>
            {% if r.rate_plan_code %}<div class="rate-sub">{{ r.rate_plan_code }}{% if r.market_code %} · {{ r.market_code }}{% endif %}</div>{% endif %}
          </td>
          <td>
            <div>{{ r.room_type_name }}</div>
            {% if r.room_desc %}<div class="rate-sub">{{ r.room_desc }}</div>{% endif %}
          </td>
          <td style="font-weight:600">{{ r.currency or h.currency }} ${{ "%.2f"|format(r.price_per_night) }}</td>
          <td class="{% if r.diff>0 %}saving{% elif r.diff<0 %}losing{% else %}neutral{% endif %}">
            {% if r.diff>0 %}↓ {{ "%.1f"|format(r.pct) }}%{% elif r.diff<0 %}↑ {{ "%.1f"|format(-r.pct) }}%{% else %}—{% endif %}
          </td>
          <td class="{% if r.diff>0 %}saving{% elif r.diff<0 %}losing{% else %}neutral{% endif %}">
            {% if r.diff>0 %}{{ h.currency }} ${{ "%.2f"|format(r.diff) }}{% elif r.diff<0 %}-{{ h.currency }} ${{ "%.2f"|format(-r.diff) }}{% else %}—{% endif %}
          </td>
          <td class="{% if r.diff>0 %}saving{% elif r.diff<0 %}losing{% else %}neutral{% endif %}">
            {% if r.diff>0 %}{{ h.currency }} ${{ "%.2f"|format(r.diff * h.num_nights) }}{% elif r.diff<0 %}-{{ h.currency }} ${{ "%.2f"|format(-r.diff * h.num_nights) }}{% else %}—{% endif %}
          </td>
          <td>
            {% if r.is_refundable == true %}
              <span class="tag refundable">✓ Refundable</span>
            {% elif r.is_refundable == false %}
              {% if r.deposit_required %}
                <span class="tag deposit">Deposit req.</span>
              {% else %}
                <span style="color:var(--red);font-size:0.8rem;">Non-refundable</span>
              {% endif %}
            {% else %}
              <span style="color:var(--muted);font-size:0.8rem;">—</span>
            {% endif %}
          </td>
        </tr>
        {% endfor %}
        </tbody>
      </table>
      <div class="no-match" id="nomatch-{{ loop.index }}">No rates match this filter.</div>
      {% if has_cheaper %}
      <div class="best-deal">
        ✅ <strong>Best available: {{ h.best_name }}</strong> at {{ h.currency }} ${{ "%.2f"|format(h.best_price) }}/night —
        save <strong>{{ h.currency }} ${{ "%.2f"|format(h.best_diff) }}/night</strong> =
        <strong>{{ h.currency }} ${{ "%.2f"|format(h.best_total) }} total</strong> over {{ h.num_nights }} night{% if h.num_nights!=1 %}s{% endif %}.
        &nbsp;<a href="https://www.marriott.com/reservation/rateListMenu.mi?propertyCode={{ h.property_code }}&fromDate={{ h.check_in }}&toDate={{ h.check_out }}" target="_blank" style="color:var(--accent)">Re-book →</a>
      </div>
      {% endif %}
    </div>
    {% else %}
    <div class="no-rates">⚠️ No rates returned — check your cookies in Settings or try again.</div>
    {% endif %}
  </div><!-- hotel-collapsible -->
  </div><!-- hotel-card -->
  {% endfor %}

  {% elif state.status=='idle' %}
  <div class="idle-msg"><div class="big">🏨</div>
    Press <strong>Check Now</strong> to see all available rates for your {{ hotels|length }} reservation{% if hotels|length!=1 %}s{% endif %}.</div>
  {% endif %}
</div>
<script>
// ── Filter ────────────────────────────────────────────────────────────────
const hotelFilters = {};
const hotelSortState = {};

function setFilter(btn){
  const hid    = btn.dataset.hotel;
  const filter = btn.dataset.filter;
  hotelFilters[hid] = filter;

  // update button states
  btn.closest('.filter-bar').querySelectorAll('.filter-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.filter === filter);
  });
  applyFilterAndSort(hid);
}

function applyFilterAndSort(hid){
  const tbody  = document.getElementById('tbody-' + hid);
  const nomatch = document.getElementById('nomatch-' + hid);
  const filter = hotelFilters[hid] || 'all';
  const sort   = hotelSortState[hid] || {col:'price_per_night', dir:'asc'};

  let rows = Array.from(tbody.querySelectorAll('tr'));

  // Filter
  rows.forEach(row => {
    let show = true;
    if(filter === 'refundable')    show = row.dataset.refundable === 'true';
    if(filter === 'nonrefundable') show = row.dataset.refundable === 'false';
    if(filter === 'member')        show = row.dataset.member === 'true';
    row.classList.toggle('hidden-row', !show);
  });

  // Sort visible rows
  const visible = rows.filter(r => !r.classList.contains('hidden-row'));
  const numCols = ['price_per_night','pct','diff','total_savings'];
  const dataKey = {
    rate_name:       r => r.dataset.rate.toLowerCase(),
    room_type_name:  r => r.dataset.room.toLowerCase(),
    price_per_night: r => parseFloat(r.dataset.price),
    pct:             r => parseFloat(r.dataset.pct),
    diff:            r => parseFloat(r.dataset.diff),
    total_savings:   r => parseFloat(r.dataset.total),
  };
  const getter = dataKey[sort.col] || (r => r.dataset.rate.toLowerCase());
  const dir    = sort.dir === 'asc' ? 1 : -1;

  visible.sort((a, b) => {
    const av = getter(a), bv = getter(b);
    if(typeof av === 'number') return (av - bv) * dir;
    return av.localeCompare(bv) * dir;
  });

  // Re-append in sorted order (hidden rows go to bottom)
  const hidden = rows.filter(r => r.classList.contains('hidden-row'));
  [...visible, ...hidden].forEach(r => tbody.appendChild(r));

  nomatch.style.display = visible.length === 0 ? 'block' : 'none';
}

// ── Sort ─────────────────────────────────────────────────────────────────
function sortTable(th){
  const hid = th.dataset.hotel;
  const col = th.dataset.col;
  const cur = hotelSortState[hid] || {col:'price_per_night', dir:'asc'};

  const dir = (cur.col === col && cur.dir === 'asc') ? 'desc' : 'asc';
  hotelSortState[hid] = {col, dir};

  // Update all header classes for this table
  document.getElementById('table-' + hid).querySelectorAll('th.sortable').forEach(h => {
    h.classList.remove('sort-asc','sort-desc');
  });
  th.classList.add(dir === 'asc' ? 'sort-asc' : 'sort-desc');

  applyFilterAndSort(hid);
}

// ── Collapse ─────────────────────────────────────────────────────────────
function toggleCollapse(id){
  document.getElementById(id).classList.toggle('collapsed');
}

// ── Check Now ────────────────────────────────────────────────────────────
function startCheck(){
  const btn=document.getElementById('checkBtn');
  btn.disabled=true;btn.textContent='Checking…';
  fetch('/check',{method:'POST'}).then(r=>r.json()).then(d=>{
    if(!d.ok){btn.disabled=false;btn.textContent='Check Now';alert(d.msg);return;}
    pollStatus();
  });
}
function pollStatus(){
  fetch('/status').then(r=>r.json()).then(s=>{
    if(s.status==='checking')setTimeout(pollStatus,2000);
    else window.location.reload();
  }).catch(()=>setTimeout(pollStatus,3000));
}
{% if state.status=='checking' %}setTimeout(pollStatus,2000);{% endif %}

// ── Countdown timer ───────────────────────────────────────────────────────
(function(){
  const el = document.getElementById('countdown');
  if(!el) return;
  // Use epoch ms + interval so browser timezone never causes drift
  const lastRun    = parseInt(el.dataset.lastRun, 10);
  const intervalMs = parseFloat(el.dataset.intervalHours) * 3600000;
  const target     = lastRun + intervalMs;
  function tick(){
    const diff = Math.max(0, target - Date.now());
    if(diff === 0){ el.textContent = 'now'; setTimeout(()=>window.location.reload(), 3000); return; }
    const h = Math.floor(diff / 3600000);
    const m = Math.floor((diff % 3600000) / 60000);
    const s = Math.floor((diff % 60000) / 1000);
    el.textContent = (h ? h+'h ' : '') + String(m).padStart(2,'0')+'m ' + String(s).padStart(2,'0')+'s';
    setTimeout(tick, 1000);
  }
  tick();
})();
</script></body></html>
"""

SETTINGS = """<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Settings — Marriott Price Checker</title><style>""" + CSS + """
.form-row{display:grid;gap:12px;margin-bottom:12px;}
.form-row.c2{grid-template-columns:1fr 1fr;}
.form-row.c4{grid-template-columns:1fr 1fr 1fr 1fr;}
.hotel-entry{background:var(--input-bg);border:1px solid var(--border);border-radius:10px;padding:18px;margin-bottom:14px;}
.hotel-entry-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;}
.hotel-entry-title{font-size:0.875rem;font-weight:600;color:var(--accent);}
.remove-btn{background:transparent;border:1px solid var(--border);color:var(--muted);border-radius:6px;padding:4px 10px;font-size:0.78rem;cursor:pointer;}
.remove-btn:hover{border-color:var(--red);color:var(--red);}
.add-btn{width:100%;padding:12px;background:transparent;border:1px dashed var(--border);border-radius:10px;color:var(--muted);font-size:0.875rem;cursor:pointer;transition:all 0.2s;}
.add-btn:hover{border-color:var(--accent);color:var(--accent);}
.cookie-hint{background:rgba(200,169,110,0.07);border:1px solid rgba(200,169,110,0.2);border-radius:8px;padding:12px 14px;font-size:0.8rem;color:var(--muted);margin-top:10px;line-height:1.6;}
.cookie-hint strong{color:var(--accent);}
</style></head><body>
<header>
  <h1>🏨 Marriott Price Checker</h1>
  <div class="nav-links">
    <a href="/" class="btn">← Dashboard</a>
    <button class="btn primary" onclick="saveAll()">💾 Save Settings</button>
  </div>
</header>
<div class="container">
  <div class="card">
    <div class="card-header"><h2>🏨 Your Reservations</h2></div>
    <div class="card-body">
      <p style="font-size:0.83rem;color:var(--muted);margin-bottom:18px;">Add one entry per Marriott reservation to monitor.</p>
      <div id="hotelList"></div>
      <button class="add-btn" onclick="addHotel()">+ Add Reservation</button>
    </div>
  </div>
  <div class="card">
    <div class="card-header"><h2>🍪 Browser Cookies <span style="font-size:0.75rem;color:var(--muted);font-weight:400;">(required for Member &amp; AAA rates)</span></h2></div>
    <div class="card-body">
      <label>Paste your full Marriott Cookie header value here</label>
      <textarea id="cookieInput" rows="4" placeholder="e.g. JSESSIONID=abc123; mi_site=en_US; ...">{{ config.browser_cookies or '' }}</textarea>
      <div class="cookie-hint">
        <strong>How to get your cookies:</strong><br>
        1. Log into <a href="https://www.marriott.com" target="_blank" style="color:var(--accent)">marriott.com</a> in your browser<br>
        2. Open DevTools (F12) → <strong>Network</strong> tab → reload the page<br>
        3. Click any request to marriott.com → <strong>Headers</strong> → find <code>Cookie:</code> and copy the full value<br>
        4. Paste above and save. <em>Cookies expire every few days — refresh them if Member/AAA rates stop appearing.</em>
      </div>
    </div>
  </div>
  <div class="card">
    <div class="card-header"><h2>⏱️ Schedule</h2></div>
    <div class="card-body">
      <p style="font-size:0.83rem;color:var(--muted);margin-bottom:18px;">How often the price checker runs automatically in the background.</p>
      <div class="form-row" style="max-width:280px;">
        <div><label>Check interval (hours)</label>
          <input type="number" id="scheduleHours" min="0.5" max="168" step="0.5"
            value="{{ config.schedule_hours or 3 }}" placeholder="e.g. 3">
        </div>
      </div>
      <p style="font-size:0.78rem;color:var(--muted);margin-top:8px;">Minimum 0.5h (30 min). Changes take effect after the current interval completes.</p>
    </div>
  </div>
  <div class="card">
    <div class="card-header"><h2>🔔 Home Assistant Notifications</h2></div>
    <div class="card-body">
      <p style="font-size:0.83rem;color:var(--muted);margin-bottom:18px;">
        Receive push notifications on your phone via Home Assistant when a cheaper rate is found, and a summary after every check.
      </p>
      <div class="form-row c2" style="margin-bottom:12px;">
        <div>
          <label>Home Assistant URL</label>
          <input type="text" id="haUrl" value="{{ config.ha_url or '' }}" placeholder="e.g. http://homeassistant.local:8123">
        </div>
        <div>
          <label>Notify Service Name</label>
          <input type="text" id="haService" value="{{ config.ha_service or 'notify' }}" placeholder="e.g. notify or mobile_app_your_phone">
        </div>
      </div>
      <div class="form-row" style="margin-bottom:12px;">
        <div>
          <label>Long-lived Access Token</label>
          <input type="password" id="haToken" value="{{ config.ha_token or '' }}" placeholder="Paste your HA long-lived access token here">
        </div>
      </div>
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
        <button class="btn" onclick="testNotify()" id="testBtn">📲 Send Test Notification</button>
        <span id="testResult" style="font-size:0.82rem;"></span>
      </div>
      <div class="cookie-hint" style="margin-top:14px;">
        <strong>How to set up:</strong><br>
        1. In Home Assistant go to <strong>Profile → Security → Long-lived access tokens → Create token</strong><br>
        2. For the service name, go to <strong>Developer Tools → Services</strong> and search <code>notify</code> — use the part after <code>notify.</code> (e.g. <code>mobile_app_your_phone</code>), or just use <code>notify</code> to send to all devices<br>
        3. Paste the URL, service name, and token above, save, then hit Send Test Notification to confirm it works
      </div>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>
<script>
let hotels = {{ config.hotels | tojson }};
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;');}
function renderHotels(){
  document.getElementById('hotelList').innerHTML=hotels.map((h,i)=>`
  <div class="hotel-entry" id="hotel-${i}">
    <div class="hotel-entry-header">
      <span class="hotel-entry-title">Reservation ${i+1}</span>
      <button class="remove-btn" onclick="removeHotel(${i})">✕ Remove</button>
    </div>
    <div class="form-row c2">
      <div><label>Reservation Name</label><input type="text" value="${esc(h.name)}" onchange="hotels[${i}].name=this.value" placeholder="e.g. Marriott Vancouver"></div>
      <div><label>Property Code (from Marriott URL)</label><input type="text" value="${esc(h.property_code)}" onchange="hotels[${i}].property_code=this.value.toUpperCase()" placeholder="e.g. YKAFI"></div>
    </div>
    <div class="form-row c4">
      <div><label>Check-in</label><input type="date" value="${esc(h.check_in)}" onchange="hotels[${i}].check_in=this.value"></div>
      <div><label>Check-out</label><input type="date" value="${esc(h.check_out)}" onchange="hotels[${i}].check_out=this.value"></div>
      <div><label>Adults</label><input type="number" min="1" max="10" value="${h.adults||1}" onchange="hotels[${i}].adults=parseInt(this.value)||1"></div>
      <div><label>Rooms</label><input type="number" min="1" max="10" value="${h.num_rooms||1}" onchange="hotels[${i}].num_rooms=parseInt(this.value)||1"></div>
    </div>
    <div class="form-row c2">
      <div><label>Your Booked Rate / night</label><input type="number" min="0" step="0.01" value="${h.original_rate_per_night||''}" onchange="hotels[${i}].original_rate_per_night=parseFloat(this.value)||0" placeholder="e.g. 229.00"></div>
      <div><label>Currency</label>
        <select onchange="hotels[${i}].currency=this.value" style="background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:9px 12px;font-size:0.875rem;width:100%;outline:none;cursor:pointer;">
          ${['CAD','USD','EUR','GBP','AUD','JPY','CHF','MXN','BRL','SGD','HKD','NZD','SEK','NOK','DKK','INR','CNY','KRW','AED','THB'].map(c=>
            `<option value="${c}" ${(h.currency||'CAD')===c?'selected':''} >${c}</option>`
          ).join('')}
        </select>
      </div>
    </div>

    <div class="form-row">
      <div><label>My Booked Rate Cancellation Type <span style="color:var(--muted);font-weight:400;">(used to compare like-for-like rates)</span></label>
        <select onchange="hotels[${i}].cancellation_type=this.value" style="background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:9px 12px;font-size:0.875rem;width:100%;outline:none;cursor:pointer;">
          <option value="any"           ${(h.cancellation_type||'any')==='any'           ?'selected':''}>Any — compare against all available rates</option>
          <option value="refundable"    ${(h.cancellation_type||'any')==='refundable'    ?'selected':''}>Refundable / Free Cancellation — only compare flexible rates</option>
          <option value="nonrefundable" ${(h.cancellation_type||'any')==='nonrefundable' ?'selected':''}>Non-refundable / Prepay — only compare prepay rates</option>
        </select>
      </div>
    </div>  </div>`).join('');
}
function addHotel(){
  hotels.push({name:'',property_code:'',check_in:'',check_out:'',adults:2,num_rooms:1,original_rate_per_night:0,currency:'CAD',cancellation_type:'any'});
  renderHotels();
  document.getElementById('hotel-'+(hotels.length-1)).scrollIntoView({behavior:'smooth',block:'center'});
}
function removeHotel(i){hotels.splice(i,1);renderHotels();}
function saveAll(){
  const cookies       = document.getElementById('cookieInput').value.trim();
  const haUrl         = document.getElementById('haUrl').value.trim();
  const haToken       = document.getElementById('haToken').value.trim();
  const haService     = document.getElementById('haService').value.trim();
  const scheduleHours = parseFloat(document.getElementById('scheduleHours').value) || 3;
  fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({hotels, browser_cookies:cookies, ha_url:haUrl, ha_token:haToken, ha_service:haService, schedule_hours:scheduleHours})
  }).then(r=>r.json()).then(d=>{
    const t=document.getElementById('toast');
    t.className='toast show '+(d.ok?'success':'error');
    t.innerHTML=d.ok?'✅ Settings saved':'❌ '+d.error;
    setTimeout(()=>t.className='toast',3500);
  });
}
function testNotify(){
  const btn=document.getElementById('testBtn');
  const res=document.getElementById('testResult');
  btn.disabled=true; res.textContent='Sending…'; res.style.color='var(--muted)';
  // Save first so the test uses current values
  const cookies = document.getElementById('cookieInput').value.trim();
  const haUrl   = document.getElementById('haUrl').value.trim();
  const haToken = document.getElementById('haToken').value.trim();
  const haService = document.getElementById('haService').value.trim();
  const scheduleHours = parseFloat(document.getElementById('scheduleHours').value) || 3;
  fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({hotels, browser_cookies:cookies, ha_url:haUrl, ha_token:haToken, ha_service:haService, schedule_hours:scheduleHours})
  }).then(()=>fetch('/api/notify/test',{method:'POST'}))
    .then(r=>r.json()).then(d=>{
      btn.disabled=false;
      if(d.ok){ res.textContent='✅ Notification sent!'; res.style.color='var(--green)'; }
      else     { res.textContent='❌ '+d.error;           res.style.color='var(--red)';   }
    }).catch(()=>{ btn.disabled=false; res.textContent='❌ Request failed'; res.style.color='var(--red)'; });
}
renderHotels();
</script></body></html>
"""

def scheduler():
    """Background thread: run checks on the configured interval (default 3h)."""
    # Wait for Flask to fully start before first run
    time.sleep(5)
    while True:
        if get_hotels():
            with state_lock:
                already_running = state["status"] == "checking"
            if not already_running:
                threading.Thread(target=run_checks, daemon=True).start()
        # Re-read interval each cycle so changes take effect without restart
        interval_hrs = float(load_config().get("schedule_hours", 3))
        for _ in range(int(interval_hrs * 3600)):
            time.sleep(1)


if __name__ == "__main__":
    threading.Thread(target=scheduler, daemon=True).start()
    app.run(host="0.0.0.0", port=8080, debug=False)
