"""Pipeline: wires extract -> transform -> load into the few jobs the app and CLI need."""

from __future__ import annotations

from dataclasses import dataclass

from .config import LOG_CSV, get_api_key
from .extract import BatchResult, OpenWeatherClient, fetch_many
from .load import append_snapshots, save_user_city
from .models import Assumptions, Attraction, Location, WeatherSnapshot
from .transform import assess_city, build_grid


def build_client() -> OpenWeatherClient:
    """Create a client from the key in the environment (raises MissingAPIKeyError if absent)."""
    return OpenWeatherClient(get_api_key())


@dataclass
class RefreshResult:
    batch: BatchResult
    logged: int  # rows appended to the CSV log


def refresh_cities(client: OpenWeatherClient, locations: list[Location], log_path=LOG_CSV) -> RefreshResult:
    """Fetch current weather for tracked cities and append every success to the log."""
    batch = fetch_many(client, locations)
    logged = append_snapshots(batch.snapshots, log_path) if batch.snapshots else 0
    return RefreshResult(batch, logged)


def fetch_closeup(client: OpenWeatherClient, center: Location, grid_size: int, radius_km: float) -> BatchResult:
    """Sample current weather on a grid around a city centre (not logged: it is not a city series)."""
    return fetch_many(client, build_grid(center, grid_size, radius_km))


def fetch_forecast(client: OpenWeatherClient, location: Location) -> list[WeatherSnapshot]:
    return client.forecast(location)


def add_city(client: OpenWeatherClient, query: str) -> tuple[Location, bool]:
    """Validate and geocode a city typed by the user, then remember it. Returns (location, is_new)."""
    location = client.geocode(query)
    return location, save_user_city(location)


def city_attractions(attractions: list[Attraction], city: str) -> list[Attraction]:
    return [a for a in attractions if a.city == city]


def assess_closeup(attractions: list[Attraction], batch: BatchResult, assumptions: Assumptions | None = None):
    """Verdict for each attraction from the grid weather nearest to it."""
    return assess_city(attractions, batch.snapshots, assumptions) if batch.snapshots else []
