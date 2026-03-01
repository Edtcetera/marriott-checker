# Plan: Award Stay (Points) Monitoring

## Context

The app monitors Marriott cash rates. The user wants two new capabilities:
1. **Award stay monitoring**: track a points-booked reservation and alert when points cost drops
2. **Cash + points comparison**: show points/night as an extra column on cash reservations

## Key Discovery

The existing GraphQL API **already returns `HotelRoomRateModesPoints` rooms** in the same response as cash rooms — no query changes required. The current parser skips them at `if price is None: continue` because `averageNightlyRatePerUnit` doesn't exist on award rooms.

Points room structure (confirmed from live API response):
```json
"rateModes": {
    "__typename": "HotelRoomRateModesPoints",
    "pointsPerUnit": {
        "__typename": "RateAmountPoints",
        "freeNights": 1,
        "points": 144000,
        "pointsSaved": null
    }
}
```
- `points` — integer, already in points units (no decimal conversion needed)
- `rate_name` is always `"Redemption"` for award rooms
- `freeCancellationUntil` is populated on refundable award rooms (same as cash)

---

## Files to Modify

- `checker.py` — room parser, `find_best_match`
- `app.py` — config validation, `run_checks()`, SETTINGS template, DASHBOARD template, CSS
- `notify.py` — notification formatting

---

## Changes

### 1. `checker.py`

**1a. Update room parsing loop** (lines 255–299)

Replace the current early-exit on cash price with a branch on `rateModes.__typename`:

```python
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
```

Add `"points_per_night": points_per_night` to the `rooms.append({...})` dict.

**1b. Update `find_best_match()`** to branch on `stay_type`:

```python
def find_best_match(rooms, config):
    if not rooms:
        return None

    stay_type   = config.get("stay_type", "cash")
    cancel_type = config.get("cancellation_type", "any")

    if stay_type == "award":
        eligible = [r for r in rooms if r.get("points_per_night") is not None]
    else:
        eligible = [r for r in rooms if r.get("price_per_night") is not None]

    if cancel_type == "refundable":
        candidates = [r for r in eligible if r.get("is_refundable") is True]
    elif cancel_type == "nonrefundable":
        candidates = [r for r in eligible if r.get("is_refundable") is False]
    else:
        candidates = eligible

    if not candidates:
        return None

    if stay_type == "award":
        return min(candidates, key=lambda r: r["points_per_night"])
    else:
        return min(candidates, key=lambda r: r["price_per_night"])
```

---

### 2. `app.py` — Config Validation (`api_save_config`, ~line 162)

Replace `assert float(h["original_rate_per_night"]) > 0`:
```python
stay_type = h.get("stay_type", "cash")
if stay_type == "award":
    assert int(h.get("original_points_per_night") or 0) > 0
else:
    assert float(h.get("original_rate_per_night") or 0) > 0
```

---

### 3. `app.py` — `run_checks()`

Add `stay_type` branch. Award path mirrors cash path with points units:
- `original = config.get("original_points_per_night")`
- `best_val = best["points_per_night"] if best else None`
- Diffs/pct/total use identical math (savings = original - best)
- Dedup rate_rows by `(rate_name, room_type_code)`, keyed on `points_per_night` for award stays
- Result dict includes `"stay_type": "award"` so templates can branch

For **cash stays**: include `points_per_night` from the room dict in each `rate_row` (no extra work — it's already in the room dict). Set `"has_points_data": any(r.get("points_per_night") for r in rate_rows)` so the dashboard can conditionally show the points column.

---

### 4. `app.py` — SETTINGS Template

In `renderHotels()` JS function:

**New stay-type selector** (above the existing rate fields):
```javascript
`<div class="form-row">
  <div>
    <label>Stay Type</label>
    <select onchange="setStayType(${i}, this.value)">
      <option value="cash"  ${stayType==='cash'  ? 'selected':''}>Cash / Paid Rate</option>
      <option value="award" ${stayType==='award' ? 'selected':''}>Award Stay (Points)</option>
    </select>
  </div>
</div>`
```

**Conditional rate fields** — show either the cash rate+currency block or the points block:
- Cash: existing `original_rate_per_night` + `currency` fields
- Award: `original_points_per_night` number input (step 1000, placeholder "e.g. 50000"), no currency field

**`setStayType(i, val)` JS helper** toggles `display` on the two field groups.

**`addHotel()` defaults** — add:
```javascript
stay_type: 'cash',
original_points_per_night: null,
```

---

### 5. `app.py` — DASHBOARD Template

Use `{% if h.stay_type == 'award' %}` in these locations:

| Location | Cash | Award |
|---|---|---|
| Booked bar | `CAD $229.00/night · N nights` | `50,000 pts/night · N nights` |
| Table header col 3 | `Price/night` | `Points/night` |
| Table header col 5–6 | `Save/night`, `Trip Savings` | `Save/night (pts)`, `Trip Savings (pts)` |
| Price cell | `$229.00` | `50,000 pts` |
| Savings cells | dollar format | integer with `,` separator + ` pts` |
| Badge text | existing | `↓ X% fewer pts — rebook` or `✓ Best pts rate` |
| Best deal bar | existing | `Best: 44,000 pts/night — saves 6,000 pts/night = 30,000 pts total` |
| `data-price` on `<tr>` | `price_per_night` | `points_per_night` |

For **cash stays with points data** (`h.has_points_data`): add 8th `<th>Points</th>` column and render `{{ "{:,}".format(r.points_per_night|int) }} pts` (or `—`) per row. No sorting needed for this column initially.

---

### 6. `notify.py`

In `send_cheaper_rate_alert`: branch on `h.get("stay_type") == "award"`:
```
🏨 Cheaper award rate found — {name}
{rate_name}
{pts:,} pts/night  (↓ {pct:.1f}% vs your {original:,} pts)
Saves {diff_pn:,} pts/night · {diff_trip:,} pts over N nights
Check-in YYYY-MM-DD → YYYY-MM-DD
```

In `send_summary`: branch similarly to format `{pts:,} pts savings` for award stays.

---

### 7. CSS (minor)

Add to shared `CSS` string:
```css
.badge.award { background: rgba(93,173,226,0.15); color: #5dade2; }
.tag.points  { background: rgba(93,173,226,0.12); color: #5dade2; }
```

---

## New Config Schema (backward compatible)

```python
{
  # existing (unchanged, all with .get() defaults)
  "name", "property_code", "check_in", "check_out",
  "adults", "num_rooms", "original_rate_per_night",
  "currency", "cancellation_type",

  # new (optional)
  "stay_type": "cash" | "award",           # default: "cash"
  "original_points_per_night": int | None  # required for award stays
}
```

---

## Verification

1. **Existing cash configs unaffected**: run a check with current config, confirm dashboard renders identically and `points_per_night: null` appears in rate_rows without breaking anything.

2. **Points rows visible**: after the checker.py change, trigger a check and confirm rooms with `rate_name = "Redemption"` now appear in the rates table with a pts value (rather than being silently skipped).

3. **Cash + points table**: for a cash reservation, confirm the rates table shows a "Points" column with award rows populated.

4. **Award stay config round-trip**: add a reservation in Settings with "Award Stay" type and a points value → save → reload → verify `stay_type` and `original_points_per_night` persist.

5. **Award dashboard**: verify booked bar shows pts, table headers show pts labels, badge uses pts language, best-deal bar formats in pts.

6. **Notifications**: trigger a check where award pts < original booked pts, verify HA notification uses pts formatting.
