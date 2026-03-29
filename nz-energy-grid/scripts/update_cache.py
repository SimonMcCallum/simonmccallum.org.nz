#!/usr/bin/env python3
"""
NZ Energy Grid — Cache Update Script
=====================================
Run via cron every 5 minutes:
  */5 * * * * cd /home/simon/nz-energy-grid && python3 scripts/update_cache.py >> logs/cron.log 2>&1

This script:
  1. Authenticates to em6 via AWS Cognito
  2. Fetches generation by fuel type (free tier)
  3. Fetches carbon intensity (free tier)
  4. Optionally fetches SCADA load by zone (paid Basic tier)
  5. Fetches Ember monthly data (open, CC-BY) — daily
  6. Writes results to data/*.json for the proxy server to read

If em6 auth fails, falls back to Ember API.
If everything fails, does nothing — the proxy serves MBIE estimates.
"""

import os
import sys
import json
import time
import logging
from pathlib import Path
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
log = logging.getLogger("update_cache")

try:
    import requests
except ImportError:
    log.error("requests not installed: pip install requests")
    sys.exit(1)

# ── Config ──
BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

EM6_AUTH_URL = "https://api.em6.co.nz/auth"
EM6_API_URL = "https://api.em6.co.nz/ords/em6/data_api"
EMBER_API_URL = "https://api.ember-energy.org/v1"

# Load .env
def load_env():
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())

load_env()

EM6_USERNAME = os.environ.get("EM6_USERNAME", "")
EM6_PASSWORD = os.environ.get("EM6_PASSWORD", "")
EM6_CLIENT_ID = os.environ.get("EM6_CLIENT_ID", "")

# ── Token cache (file-based so cron doesn't re-auth every run) ──
TOKEN_FILE = DATA_DIR / ".em6_token.json"


def write_json(name, data):
    """Write data to data/{name}.json atomically."""
    fp = DATA_DIR / f"{name}.json"
    tmp = DATA_DIR / f"{name}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.rename(fp)
    log.info(f"Wrote {fp} ({fp.stat().st_size} bytes)")


# ═══════════════════════════════════════════════════════
# EM6 Authentication
# ═══════════════════════════════════════════════════════

def em6_get_token():
    """Get a valid em6 token, using file cache if not expired."""
    # Check cached token
    if TOKEN_FILE.exists():
        try:
            cached = json.loads(TOKEN_FILE.read_text())
            if cached.get("expires_at", 0) > time.time():
                log.info("Using cached em6 token")
                return cached["id_token"]
        except Exception:
            pass

    if not all([EM6_USERNAME, EM6_PASSWORD, EM6_CLIENT_ID]):
        log.warning("em6 credentials not configured — skipping em6 fetch")
        return None

    log.info(f"Authenticating to em6 as {EM6_USERNAME}...")
    try:
        resp = requests.post(EM6_AUTH_URL, json={
            "AuthParameters": {
                "USERNAME": EM6_USERNAME,
                "PASSWORD": EM6_PASSWORD,
            },
            "AuthFlow": "USER_PASSWORD_AUTH",
            "ClientId": EM6_CLIENT_ID,
        }, timeout=15)

        if resp.status_code != 200:
            log.error(f"em6 auth failed: {resp.status_code} {resp.text[:200]}")
            return None

        result = resp.json()
        auth = result["AuthenticationResult"]
        token_data = {
            "id_token": auth["IdToken"],
            "expires_at": time.time() + auth["ExpiresIn"] - 120,
        }
        TOKEN_FILE.write_text(json.dumps(token_data))
        TOKEN_FILE.chmod(0o600)
        log.info("em6 auth successful, token cached")
        return auth["IdToken"]

    except Exception as e:
        log.error(f"em6 auth error: {e}")
        return None


def em6_fetch(endpoint, token, params=None):
    """Fetch from em6 API with auth token."""
    url = f"{EM6_API_URL}/{endpoint}"
    try:
        resp = requests.get(url, params=params,
                          headers={"Authorization": token},
                          timeout=15)
        if resp.status_code == 200:
            return resp.json()
        log.error(f"em6 {endpoint}: HTTP {resp.status_code}")
    except Exception as e:
        log.error(f"em6 {endpoint} error: {e}")
    return None


# ═══════════════════════════════════════════════════════
# Data Fetchers
# ═══════════════════════════════════════════════════════

def fetch_generation(token):
    """Fetch generation by fuel type from em6 Generation Type API (free)."""
    data = em6_fetch("generation_type", token)
    if not data or "items" not in data:
        return None

    by_type = {}
    total_mw = 0
    renewable_mw = 0
    renewable_ids = {"HYD", "GEO", "WIN", "SOL"}
    type_map = {
        "HYD": "hydro", "GEO": "geothermal", "WIN": "wind",
        "GAS": "gas", "CG": "coal_gas", "COG": "cogen",
        "SOL": "solar", "BAT": "battery", "LIQ": "diesel",
    }

    for item in data["items"]:
        tid = item.get("generation_type_id", "")
        mw = item.get("mw", 0) or 0
        name = type_map.get(tid, tid.lower())
        by_type[name] = mw
        total_mw += mw
        if tid in renewable_ids:
            renewable_mw += mw

    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "em6_live",
        "by_type": by_type,
        "total_mw": total_mw,
        "renewable_mw": renewable_mw,
        "renewable_pct": round(renewable_mw / max(total_mw, 1) * 100, 1),
    }
    return result


def fetch_carbon(token):
    """Fetch carbon intensity from em6 (free tier)."""
    data = em6_fetch("current_carbon", token)
    if not data or "items" not in data or not data["items"]:
        return None

    item = data["items"][0]
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "em6_live",
        "intensity_gco2_per_kwh": item.get("carbon_intensity"),
        "renewable_pct": item.get("renewable_percentage"),
    }


def fetch_scada_load(token):
    """Fetch SCADA load by zone from em6 (paid Basic tier).
    Returns None if not subscribed."""
    data = em6_fetch("scada_load", token)
    if not data or "items" not in data:
        return None

    zones = {}
    total = 0
    # Map em6 grid zone IDs to readable names
    zone_names = {
        1: "upper_north", 2: "auckland", 3: "waikato",
        4: "central_plateau", 5: "eastern_ni", 6: "taranaki",
        7: "bop", 8: "wellington", 9: "wairarapa",
        10: "nelson_marlborough", 12: "canterbury",
        13: "otago", 14: "southland", 15: "southland_tiwai",
    }
    for item in data["items"]:
        zid = item.get("grid_zone_id")
        mw = item.get("mw", 0) or 0
        name = zone_names.get(zid, f"zone_{zid}")
        zones[name] = mw
        total += mw

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "em6_live",
        "zones": zones,
        "total_mw": total,
    }


def fetch_scada_generation(token):
    """Fetch per-station generation from em6 SCADA (paid).
    This gives individual station output — the gold standard."""
    data = em6_fetch("scada_generation", token)
    if not data or "items" not in data:
        return None

    stations = []
    for item in data["items"]:
        stations.append({
            "node_id": item.get("node_id"),
            "site_name": item.get("site_name"),
            "mw": item.get("mw", 0) or 0,
            "type": item.get("generation_type_id"),
        })

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "em6_live",
        "stations": stations,
        "total_mw": sum(s["mw"] for s in stations),
    }


def fetch_ember():
    """Fetch latest monthly generation data from Ember (open, CC-BY).
    Only run once per day — check file age."""
    cache = DATA_DIR / "ember.json"
    if cache.exists():
        age_hours = (time.time() - cache.stat().st_mtime) / 3600
        if age_hours < 20:
            log.info(f"Ember cache is {age_hours:.1f}h old — skipping")
            return

    log.info("Fetching Ember monthly data...")
    try:
        resp = requests.get(f"{EMBER_API_URL}/monthly-data", params={
            "entity": "New Zealand",
            "is_aggregate_series": "false",
        }, timeout=20)
        if resp.status_code == 200:
            data = resp.json()
            write_json("ember", {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": "ember_api",
                "data": data,
            })
        else:
            log.warning(f"Ember API: HTTP {resp.status_code}")
    except Exception as e:
        log.error(f"Ember fetch error: {e}")


# ═══════════════════════════════════════════════════════
# Main Update Flow
# ═══════════════════════════════════════════════════════

def main():
    log.info("=" * 50)
    log.info("NZ Energy Grid — Cache Update")
    log.info("=" * 50)

    # 1. Try em6
    token = em6_get_token()

    if token:
        # Generation (free tier)
        gen = fetch_generation(token)
        if gen:
            write_json("generation", gen)
            log.info(f"  Generation: {gen['total_mw']} MW total, {gen['renewable_pct']}% renewable")
        else:
            log.warning("  Generation fetch failed")

        # Carbon (free tier)
        carbon = fetch_carbon(token)
        if carbon:
            write_json("carbon", carbon)
            log.info(f"  Carbon: {carbon.get('intensity_gco2_per_kwh')} gCO2/kWh")
        else:
            log.warning("  Carbon fetch failed")

        # SCADA Load (paid — will return None if not subscribed)
        load = fetch_scada_load(token)
        if load:
            write_json("load", load)
            log.info(f"  Load: {load['total_mw']} MW across {len(load['zones'])} zones")
        else:
            log.info("  SCADA Load: not available (requires Basic subscription)")

        # SCADA Generation (paid — per-station output)
        station_gen = fetch_scada_generation(token)
        if station_gen:
            write_json("station_generation", station_gen)
            log.info(f"  Station gen: {len(station_gen['stations'])} stations, {station_gen['total_mw']} MW")
        else:
            log.info("  Station generation: not available (requires subscription)")

    else:
        log.info("em6 not available — skipping live data")

    # 2. Ember (open, daily)
    fetch_ember()

    log.info("Update complete")


if __name__ == "__main__":
    main()
