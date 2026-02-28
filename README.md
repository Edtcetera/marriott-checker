# Marriott Price Checker — TrueNAS SCALE Custom App

A web dashboard that checks current Marriott hotel rates across multiple rate
plans (Public, Member, AAA) and compares them against your booked reservation.

---

## Files

| File               | Purpose                                      |
|--------------------|----------------------------------------------|
| `checker.py`       | ✏️ **Edit this** — your hotels & cookies config |
| `app.py`           | Flask web server & dashboard UI              |
| `Dockerfile`       | Container build instructions                 |
| `docker-compose.yml` | TrueNAS / local deployment config          |
| `requirements.txt` | Python dependencies                          |

---

## Step 1 — Configure your reservations

Open `checker.py` and edit the two sections:

### BROWSER_COOKIES
Paste your Marriott session cookies here to unlock Member and AAA rates:
1. Log into [marriott.com](https://www.marriott.com) in your browser
2. Open DevTools → Network tab → reload the page
3. Click any request → Headers → copy the full `Cookie:` header value
4. Paste it into `BROWSER_COOKIES = "..."` in `checker.py`

### HOTELS
Add one entry per reservation:
```python
HOTELS = [
    {
        "name":                    "Marriott Vancouver",
        "property_code":           "YVRMC",   # from the URL on marriott.com
        "check_in":                "2026-07-10",
        "check_out":               "2026-07-13",
        "adults":                  2,
        "num_rooms":               1,
        "room_type_code":          "",         # leave blank for cheapest available
        "original_rate_per_night": 289.00,
    },
]
```

---

## Step 2 — Deploy on TrueNAS SCALE

### Option A: Custom App (recommended)

TrueNAS SCALE 24.04+ supports Docker Compose custom apps natively.

1. In the TrueNAS web UI go to **Apps → Discover Apps → Custom App**
2. Choose **"Install via Docker Compose"**
3. Upload or paste the contents of `docker-compose.yml`
4. Under **App Config**, set the host path for your config files if desired
5. Click **Install**

The app will be available at `http://<your-truenas-ip>:8080`

### Option B: SSH / CLI

```bash
# Copy files to your TrueNAS server
scp -r marriott-checker/ admin@truenas.local:/mnt/tank/apps/

# SSH in and start the app
ssh admin@truenas.local
cd /mnt/tank/apps/marriott-checker
docker compose up -d --build
```

---

## Step 3 — Use the dashboard

Open `http://<your-truenas-ip>:8080` in your browser.

- Press **Check Now** to fetch current rates
- The dashboard shows a side-by-side table per hotel with all rate plans
- Green border = price drop found, with total trip savings highlighted
- A **Re-book →** link takes you directly to Marriott's reservation page

---

## Updating your config

After editing `checker.py`, rebuild and restart the container:

```bash
docker compose up -d --build
```

Or on TrueNAS SCALE UI: Apps → your app → **Update / Redeploy**.

---

## Adding more rate plans

Edit the `RATE_PLANS` list in `checker.py`:

```python
RATE_PLANS = [
    {"code": "",    "label": "Best Public Rate"},
    {"code": "S9R", "label": "Member Rate"},
    {"code": "A9R", "label": "AAA Rate"},
    {"code": "S0R", "label": "Senior Rate"},    # add like this
    {"code": "GOV", "label": "Government Rate"},
]
```

---

## Notes

- Marriott's site occasionally blocks automated requests. If you see "No Data",
  refresh your `BROWSER_COOKIES` — they expire every few days.
- The checker runs in the background when you press Check Now; the page
  auto-refreshes when results are ready.
