"""Paths and environment configuration.

The API key is read from the environment (``variables.env`` via python-dotenv) and is
never hardcoded, logged, or shown in the UI.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(os.getenv("WEATHER_INSIGHTS_HOME", Path(__file__).resolve().parents[2]))
DATA_DIR = PROJECT_ROOT / "data"

CITIES_CSV = DATA_DIR / "cities.csv"  # curated world cities (shipped with the repo)
USER_CITIES_CSV = DATA_DIR / "user_cities.csv"  # cities added by users from the UI
ATTRACTIONS_CSV = DATA_DIR / "attractions.csv"  # curated attractions with weather sensitivity
LOG_CSV = DATA_DIR / "weather_log.csv"  # our own time series, appended on every fetch

API_BASE_URL = "https://api.openweathermap.org"
REQUEST_TIMEOUT_S = 10
CACHE_TTL_S = 600  # OpenWeatherMap refreshes current data roughly every 10 minutes
MAX_CALLS_PER_MINUTE = 50  # OpenWeatherMap's free plan allows 60; keep some headroom
MIN_LOG_INTERVAL_MIN = 10  # do not log the same city twice inside this window

load_dotenv(PROJECT_ROOT / "variables.env")


class MissingAPIKeyError(RuntimeError):
    """Raised when OPENWEATHER_API_KEY is not configured."""


def get_api_key() -> str:
    """Return the API key from the environment or raise a clear error."""
    key = (os.getenv("OPENWEATHER_API_KEY") or "").strip()
    if not key or key == "your_key_here":
        raise MissingAPIKeyError(
            "OPENWEATHER_API_KEY is not set. Copy variables.env.example to variables.env "
            "and paste your own OpenWeatherMap key into it."
        )
    return key
