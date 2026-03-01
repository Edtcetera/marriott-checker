# Marriott Checker — Claude Project Notes

## Project Overview
Flask web app that monitors Marriott hotel reservation prices and alerts when cheaper rates appear. Single-file Python app with HTML/CSS/JS embedded as template strings in `app.py`.

## Key Files
- **`app.py`** — entire web app: Flask routes, HTML templates (`DASHBOARD`, `SETTINGS`), `CSS` string, JS
- **`checker.py`** — price fetching logic, Marriott API calls
- **`notify.py`** — Home Assistant push notification integration
- **`requirements.txt`** — minimal deps (Flask, requests, etc.)
- **`Dockerfile` / `docker-compose.yml`** — containerized deployment
- Runs on port **8080**

## Template Architecture
Three Python string constants in `app.py`:
- `CSS` — shared styles injected into both page templates via string concatenation
- `DASHBOARD` — main page: status bar, collapsible hotel cards with rates tables
- `SETTINGS` — settings page: JS-rendered hotel entries (`renderHotels()`), cookies, schedule, HA config

## CSS Gotchas

### 1. SETTINGS cascade override
`SETTINGS` appends its own CSS *after* the shared `CSS` block inside the same `<style>` tag. Rules with equal specificity that appear later always win — so any SETTINGS-specific rules override the `@media(max-width:640px)` block in `CSS`.

**Fix pattern:** Wrap SETTINGS desktop-only grid rules in `@media(min-width:641px)` so they don't compete with mobile media queries.

### 2. Inline style vs class override
The `hotel-header-right` div has `flex-shrink:0` as an **inline style**. Class-based CSS (including media queries) cannot override inline styles without `!important`.

**Fix pattern:** Use `!important` in the media query when overriding inline styles.

## Responsive Design (mobile breakpoint: 640px)
`@media(max-width:640px)` block lives in the `CSS` variable. Key rules:
- Header wraps, `.header-time` (Last/Next timestamps) hidden on mobile
- `.hotel-header-right` uses `flex-shrink:1!important` to override inline style
- `.hotel-body` is horizontally scrollable for the 7-column rates table
- `.filter-bar` horizontally scrollable with `flex-wrap:nowrap`
- `.form-row.c4` → 2×2 grid on mobile; `.form-row.c2` → 1 column on mobile
- Grid children need `min-width:0` to prevent overflow within `1fr` cells
- `.form-row.c2.no-collapse` stays 2-col on mobile (used for HA URL + service name)

## User Preferences
- Concise responses
- No emojis unless requested
