# NZ Energy Grid & Compute Infrastructure Map

Interactive isometric map of New Zealand's electricity generation,
consumption, and data centre infrastructure with live data support.

## Project Structure

```
nz-energy-grid/
├── server.py              # Flask proxy server (serves map + API)
├── static/
│   └── index.html         # The interactive map
├── scripts/
│   └── update_cache.py    # Cron script — fetches em6/Ember data
├── data/                  # JSON cache files (written by cron)
│   ├── generation.json    # Current generation by fuel type
│   ├── load.json          # Load by grid zone (if paid em6)
│   ├── carbon.json        # Carbon intensity
│   ├── station_generation.json  # Per-station output (if paid)
│   └── ember.json         # Ember monthly data (daily refresh)
├── logs/                  # Cron log output
├── .env                   # Credentials (not committed)
├── .env.example           # Template
├── requirements.txt
└── README.md              # This file
```

## How It Works

```
┌─────────────────┐  cron  ┌──────────────────┐  HTTP  ┌──────────────┐
│ em6 API         │──5m───>│ update_cache.py   │       │              │
│ (Cognito auth)  │        │ writes JSON to    │       │              │
│                 │        │ data/*.json       │       │              │
│ Free tier:      │        └────────┬──────────┘       │              │
│ - gen by type   │                 │                   │   Browser    │
│ - carbon        │                 ▼                   │   index.html │
│                 │        ┌──────────────────┐        │              │
│ Paid (Basic):   │        │ server.py        │◄──────>│ fetches      │
│ - SCADA load    │        │ reads data/*.json │  JS   │ /api/summary │
│ - per-station   │        │ serves /api/*     │       │ every 5 min  │
└─────────────────┘        │ serves static/    │       │              │
                           └──────────────────┘       └──────────────┘
┌─────────────────┐                ▲
│ Ember API       │──daily────────>│ (via update_cache.py)
│ (Open, CC-BY)   │
└─────────────────┘

If no cache files exist, server.py generates estimates from
MBIE 2024 annual data scaled by NZ time-of-day demand curves.
```

## Quick Start (No API Key Needed)

The map works immediately with estimated data. No em6 credentials required.

```bash
# 1. Clone/copy to your server
scp -r nz-energy-grid/ simon@simonmccallum.org.nz:~/

# 2. Install dependencies
ssh simon@simonmccallum.org.nz
cd ~/nz-energy-grid
pip3 install -r requirements.txt

# 3. Run
python3 server.py
# → http://localhost:5050
```

The map will show generation/consumption data estimated from MBIE 2024
annual statistics, varying by time of day using a typical NZ demand curve.

## Adding Live Data (em6 API)

### Step 1: Get em6 Credentials

1. Register at https://app.em6.co.nz (free account)
2. Email call@ems.co.nz:

   > Subject: API ClientId request — research visualisation
   >
   > Hi, I'd like API access to the free-tier em6 endpoints
   > (Generation Type, Carbon Intensity) for a public energy
   > visualisation project. Could you provide a ClientId?

3. They reply with a ClientId string

### Step 2: Configure

```bash
cd ~/nz-energy-grid
cp .env.example .env
nano .env
# Fill in EM6_USERNAME, EM6_PASSWORD, EM6_CLIENT_ID
chmod 600 .env
```

### Step 3: Test the Update Script

```bash
python3 scripts/update_cache.py
# Should output:
#   em6 auth successful, token cached
#   Wrote data/generation.json (xxx bytes)
#   Generation: 3753 MW total, 85.2% renewable
#   Wrote data/carbon.json (xxx bytes)
#   ...

# Check the cached data
cat data/generation.json | python3 -m json.tool
```

### Step 4: Set Up Cron

```bash
mkdir -p logs

# Edit crontab
crontab -e

# Add this line (updates every 5 minutes):
*/5 * * * * cd /home/simon/nz-energy-grid && /usr/bin/python3 scripts/update_cache.py >> logs/cron.log 2>&1

# Optional: rotate logs weekly
0 0 * * 0 cd /home/simon/nz-energy-grid && : > logs/cron.log
```

### Step 5: Verify Cron is Working

```bash
# Wait 5 minutes, then check
ls -la data/
cat logs/cron.log | tail -20
curl http://localhost:5050/api/health
```

## Production Deployment

### Systemd Service

```bash
sudo tee /etc/systemd/system/nz-energy-grid.service << 'EOF'
[Unit]
Description=NZ Energy Grid Map
After=network.target

[Service]
Type=simple
User=simon
WorkingDirectory=/home/simon/nz-energy-grid
EnvironmentFile=/home/simon/nz-energy-grid/.env
ExecStart=/usr/bin/gunicorn -w 2 -b 127.0.0.1:5050 server:app
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable --now nz-energy-grid
sudo systemctl status nz-energy-grid
```

### Nginx Reverse Proxy

```nginx
# Add to your simonmccallum.org.nz server block:

location /nz-energy/ {
    proxy_pass http://127.0.0.1:5050/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

```bash
sudo nginx -t && sudo systemctl reload nginx
```

**Share link:** `https://simonmccallum.org.nz/nz-energy/`

### Alternative: Standalone (No Nginx)

If you want to serve directly on port 80/443, use gunicorn with SSL:

```bash
gunicorn -w 2 -b 0.0.0.0:443 \
  --certfile=/etc/letsencrypt/live/simonmccallum.org.nz/fullchain.pem \
  --keyfile=/etc/letsencrypt/live/simonmccallum.org.nz/privkey.pem \
  server:app
```

## Upgrading to Paid em6 Data

With an em6 Basic subscription (~$50/mo NZD):

| Feature | Free | Basic |
|---------|------|-------|
| Generation by fuel type | ✓ | ✓ |
| Carbon intensity | ✓ | ✓ |
| SCADA load by zone (14 zones) | ✗ | ✓ |
| SCADA per-station generation | ✗ | ✓ |
| Price data | ✗ | ✓ |

The update script already handles both tiers — it tries paid endpoints
and silently skips them if not subscribed. When you upgrade, the data
just starts appearing in the cache files automatically.

Per-station generation (`data/station_generation.json`) is the most
impactful upgrade — it lets the map show the actual MW output of each
individual power station in real time, rather than estimating from
the fuel-type totals.

## Data Sources

| Source | Licence | Used For |
|--------|---------|----------|
| em6 (Transpower/EMS) | Subscription | Real-time generation, load, carbon |
| Ember API | CC-BY-4.0 | Monthly generation baselines |
| MBIE | CC-BY-3.0 NZ | Annual baselines, sector breakdown |
| Wikipedia / EA | Various | Power station locations, consumer demand |
| Natural Earth | Public domain | NZ coastline geometry |
| LINZ | CC-BY-4.0 | Lake / geographic reference data |

## Troubleshooting

**Map shows "STATIC DATA" in panel:**
- No proxy running, or proxy has no cached data
- Run `python3 scripts/update_cache.py` manually to populate cache
- Check `logs/cron.log` for errors

**em6 auth fails:**
- Check credentials in `.env`
- Token may have expired — delete `data/.em6_token.json` and retry
- em6 may be down — check https://app.em6.co.nz

**No particles moving:**
- Toggle the "Flow" button in bottom-left controls
- Check browser console for JS errors

**Zoom not working:**
- Scroll wheel, +/- keys, or use the +/⌂/- buttons
- On mobile: pinch to zoom, drag to pan
- Press 0 to reset view
