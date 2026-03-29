#!/usr/bin/env python3
"""
NZ Energy Grid — Proxy Server
==============================
Serves cached energy data to the map frontend.

Two data modes:
  1. LIVE (em6 API) — requires credentials in .env
  2. CACHED (cron job) — scripts/update_cache.py writes JSON to data/
     The proxy reads from these files. No em6 credentials needed at
     serve time — the cron job handles auth separately.

Cron mode lets you run updates on a schedule (e.g. every 5 min) and
serve the cached results instantly. This is more robust than having
the web server do the fetching inline.

Endpoints:
  GET /api/generation   — current generation by fuel type
  GET /api/load         — load by grid zone / consumer
  GET /api/carbon       — carbon intensity + renewable %
  GET /api/summary      — all above combined
  GET /api/health       — status check
"""

import os
import sys
import json
import time
import logging
from pathlib import Path
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

# ── Paths ──
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

try:
    from flask import Flask, jsonify, send_from_directory
    FLASK_OK = True
except ImportError:
    FLASK_OK = False
    log.error("Flask not installed: pip install flask")

try:
    import requests as http_req
except ImportError:
    http_req = None

# ── MBIE 2024 Annual Baselines (fallback) ──
BASELINES = {
    "generation_gwh": {
        "hydro": 24066, "geothermal": 8741, "wind": 3919,
        "gas": 3850, "coal": 900, "solar": 200,
        "cogen": 800, "other": 400,
    },
    "total_gwh": 43879,
    "consumption_gwh": 40002,
    "zone_peak_mw": {
        "auckland": 1722, "hamilton": 290, "tauranga": 200,
        "wellington": 420, "christchurch": 380, "dunedin": 150,
        "tiwai_point": 572, "nz_steel": 116, "tasman_pulp": 175,
        "kiwirail": 40,
    },
    # Typical NZ hourly demand shape (fraction of peak)
    "hourly_curve": [
        0.55,0.52,0.50,0.49,0.50,0.55,  # 00-05
        0.65,0.78,0.88,0.92,0.93,0.90,  # 06-11
        0.88,0.87,0.86,0.87,0.90,0.95,  # 12-17
        1.00,0.97,0.90,0.82,0.72,0.62,  # 18-23
    ],
}


def read_cache(name):
    """Read a cached JSON file from data/ directory."""
    fp = DATA_DIR / f"{name}.json"
    if fp.exists():
        try:
            with open(fp) as f:
                data = json.load(f)
            age = time.time() - fp.stat().st_mtime
            data["_cache_age_seconds"] = round(age)
            data["_source"] = "cached"
            return data
        except Exception as e:
            log.warning(f"Cache read error for {name}: {e}")
    return None


def estimate_generation():
    """Generate estimated data from MBIE baselines + time of day."""
    import pytz
    try:
        nz_hour = datetime.now(pytz.timezone("Pacific/Auckland")).hour
    except Exception:
        nz_hour = (datetime.utcnow().hour + 12) % 24  # rough NZST

    fraction = BASELINES["hourly_curve"][nz_hour]
    avg_mw = BASELINES["total_gwh"] * 1000 / 8760
    total_gwh = BASELINES["total_gwh"]

    by_type = {}
    total_mw = 0
    renewable_mw = 0
    renewable_fuels = {"hydro", "geothermal", "wind", "solar"}

    for fuel, gwh in BASELINES["generation_gwh"].items():
        mw = round(avg_mw * fraction * (gwh / total_gwh))
        by_type[fuel] = mw
        total_mw += mw
        if fuel in renewable_fuels:
            renewable_mw += mw

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "mbie_estimated",
        "by_type": by_type,
        "total_mw": total_mw,
        "renewable_mw": renewable_mw,
        "renewable_pct": round(renewable_mw / max(total_mw, 1) * 100, 1),
        "nz_hour": nz_hour,
        "demand_fraction": fraction,
    }


def estimate_load():
    """Estimate zone loads from baselines."""
    import pytz
    try:
        nz_hour = datetime.now(pytz.timezone("Pacific/Auckland")).hour
    except Exception:
        nz_hour = (datetime.utcnow().hour + 12) % 24

    fraction = BASELINES["hourly_curve"][nz_hour]
    zones = {}
    total = 0
    for zone, peak in BASELINES["zone_peak_mw"].items():
        mw = round(peak * fraction)
        zones[zone] = mw
        total += mw

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "mbie_estimated",
        "zones": zones,
        "total_mw": total,
        "demand_fraction": fraction,
    }


def estimate_carbon():
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "estimated",
        "intensity_gco2_per_kwh": 80,
        "renewable_pct": 82,
    }


def get_generation():
    """Try cache first, then estimate."""
    cached = read_cache("generation")
    if cached and cached.get("_cache_age_seconds", 9999) < 600:
        return cached
    return estimate_generation()


def get_load():
    cached = read_cache("load")
    if cached and cached.get("_cache_age_seconds", 9999) < 600:
        return cached
    return estimate_load()


def get_carbon():
    cached = read_cache("carbon")
    if cached and cached.get("_cache_age_seconds", 9999) < 600:
        return cached
    return estimate_carbon()


def get_summary():
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "generation": get_generation(),
        "load": get_load(),
        "carbon": get_carbon(),
    }


# ── Flask App ──
if FLASK_OK:
    app = Flask(__name__, static_folder="static")

    @app.route("/")
    def index():
        return send_from_directory("static", "index.html")

    @app.route("/<path:path>")
    def static_files(path):
        return send_from_directory("static", path)

    @app.route("/api/generation")
    def api_gen():
        return jsonify(get_generation())

    @app.route("/api/load")
    def api_load():
        return jsonify(get_load())

    @app.route("/api/carbon")
    def api_carbon():
        return jsonify(get_carbon())

    @app.route("/api/summary")
    def api_summary():
        return jsonify(get_summary())

    @app.route("/api/health")
    def api_health():
        gen_cache = DATA_DIR / "generation.json"
        load_cache = DATA_DIR / "load.json"
        return jsonify({
            "status": "ok",
            "generation_cache": {
                "exists": gen_cache.exists(),
                "age_seconds": round(time.time() - gen_cache.stat().st_mtime) if gen_cache.exists() else None,
            },
            "load_cache": {
                "exists": load_cache.exists(),
                "age_seconds": round(time.time() - load_cache.stat().st_mtime) if load_cache.exists() else None,
            },
            "em6_configured": bool(os.environ.get("EM6_CLIENT_ID")),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    # Add CORS headers
    @app.after_request
    def add_cors(resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Cache-Control"] = "public, max-age=120"
        return resp

    if __name__ == "__main__":
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass
        app.run(host="127.0.0.1", port=5050, debug=True)
