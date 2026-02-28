# Marriott Price Checker — TrueNAS SCALE Custom App

A self-hosted web dashboard that monitors Marriott hotel rates across all available rate plans and compares them against your booked reservation price. Alerts you via Home Assistant when a cheaper rate is found.

---

## Features

- Fetches all available rates in a single GraphQL call (public, member, AAA, prepay, packages, etc.)
- Compares rates against your booked price — highlights savings per night and for the full trip
- Cancellation type filter: compare only refundable, only non-refundable, or all rates
- Sortable, filterable rate table per hotel
- Automatic background checks on a configurable schedule (default: every 3 hours)
- Home Assistant push notifications when a cheaper rate is found, plus a post-check summary
- All configuration managed through the built-in web UI — no file editing required

---

## Files

| File                 | Purpose                                          |
|----------------------|--------------------------------------------------|
| `app.py`             | Flask web server, dashboard UI, and scheduler    |
| `checker.py`         | Marriott GraphQL price-fetching logic            |
| `notify.py`          | Home Assistant notification integration          |
| `Dockerfile`         | Container build instructions                     |
| `docker-compose.yml` | TrueNAS / local deployment config                |
| `requirements.txt`   | Python dependencies                              |

Configuration is stored in `/data/config.json` and managed entirely through the Settings page.

---

## Deploy on TrueNAS SCALE

### Option A: Custom App (recommended)

TrueNAS SCALE 24.04+ supports Docker Compose custom apps natively.

1. In the TrueNAS web UI go to **Apps → Discover Apps → Custom App**
2. Choose **"Install via Docker Compose"**
3. Upload or paste the contents of `docker-compose.yml`
4. Click **Install**

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

## Setup

### Step 1 — Add your reservations

1. Open `http://<your-server-ip>:8080/settings`
2. Click **+ Add Reservation** and fill in:
   - **Reservation Name** — a label for the hotel (e.g. "Marriott Vancouver")
   - **Property Code** — the 5-letter code from the Marriott URL (e.g. `YVRMC`)
   - **Check-in / Check-out dates**
   - **Adults** and **Rooms**
   - **Your Booked Rate / night** — the rate you're already holding
   - **Currency** — the currency your rate is in
   - **Cancellation Type** — whether your booked rate is refundable, non-refundable, or either; used to compare like-for-like rates

### Step 2 — Add your Marriott cookies (for Member & AAA rates)

Cookies authenticate your session so Marriott returns member-only and AAA rates. Without them, only public rates are shown.

1. Log into [marriott.com](https://www.marriott.com) in your browser
2. Open DevTools (F12) → **Network** tab → reload the page
3. Click any request to `marriott.com` → **Headers** → copy the full `Cookie:` header value
4. Paste it into the **Browser Cookies** field in Settings and save

Cookies expire every few days. If Member/AAA rates stop appearing, refresh them.

### Step 3 — Configure Home Assistant notifications (optional)

1. In Home Assistant go to **Profile → Security → Long-lived access tokens → Create token**
2. Find your notify service name under **Developer Tools → Services** (search `notify`) — use the part after `notify.`, e.g. `mobile_app_your_phone`, or use `notify` to send to all devices
3. In the Settings page, enter your **HA URL**, **Notify Service Name**, and **Access Token**
4. Click **Send Test Notification** to confirm it works

Notifications are sent:
- Immediately when a cheaper rate is found for any hotel
- As a summary after every scheduled check

---

## Using the dashboard

Open `http://<your-server-ip>:8080` in your browser.

- Press **Check Now** to fetch all current rates immediately
- Each hotel card shows a summary badge — green if your booked rate is still best, or a savings percentage if a cheaper rate exists
- Click a hotel card to expand the full rate table
- Use the **filter buttons** to show only free-cancellation or non-refundable rates
- Click any **column header** to sort the table
- The **Re-book →** link goes directly to Marriott's availability page for that hotel
- The header shows the last check time and a live countdown to the next automatic check

---

## Schedule

The checker runs automatically in the background. The default interval is **3 hours**. To change it:

1. Go to Settings → **Schedule**
2. Set the interval in hours (minimum 0.5h / 30 minutes)
3. Save — the new interval takes effect after the current cycle completes

---

## Updating your config

Changes made in the Settings UI take effect immediately. After editing, click **💾 Save Settings**.

To rebuild the container after a code update:

```bash
docker compose up -d --build
```

Or on TrueNAS SCALE UI: Apps → your app → **Update / Redeploy**.

---

## Notes

- Marriott's site occasionally blocks automated requests. If you see "No rates returned", refresh your browser cookies in Settings.
- The property code is the 5-letter code visible in the Marriott URL when viewing a hotel, e.g. `YVRMC` in `.../reservation/rateListMenu.mi?propertyCode=YVRMC&...`
- Rate results are deduplicated by rate name + room type, keeping the cheapest variant of each combination.
