"""Pipeline: wires extract -> transform -> load into the few jobs the app and CLI need."""

from __future__ import annotations

from dataclasses import dataclass

from .config import ATTRACTIONS_CSV, LOG_CSV, USER_ATTRACTIONS_CSV, get_api_key
from .extract import (
    BatchResult, InvalidInputError, NominatimClient, OpenWeatherClient, fetch_many,
    validate_poi_coordinates, validate_poi_name,
)
from .load import append_snapshots, save_user_attraction, save_user_city
from .models import Assumptions, Attraction, Location, POICandidate, WeatherSnapshot
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


SETTINGS = ("outdoor", "indoor", "mixed")  # where a visit takes place; drives the weather verdict
MAX_CATEGORY_LEN = 40


def build_place_client() -> NominatimClient:
    """Create the place-search client (OpenStreetMap needs no API key)."""
    return NominatimClient()


def find_poi(client: NominatimClient, name: str, city: Location) -> list[POICandidate]:
    """Possible matches for a place the user typed in, near ``city``. Empty if nothing fits."""
    return client.search_poi(name, city)


def _clean_category(raw: str) -> str:
    """Tidy a category label ("Other" if blank) or raise InvalidInputError.

    The label is shown with st.markdown and stored in a CSV, so it gets the same character rules as a
    place name (no brackets or backticks, which would make links or formatting), plus two more checks.
    """
    category = " ".join(str(raw or "").split()) or "Other"
    if len(category) > MAX_CATEGORY_LEN:
        raise InvalidInputError(f"That category is too long ({MAX_CATEGORY_LEN} characters max).")
    if category[0] in "=+-@":  # spreadsheet programs treat these as the start of a formula
        raise InvalidInputError("A category cannot start with = + - or @.")
    try:
        return validate_poi_name(category)
    except InvalidInputError as exc:
        raise InvalidInputError(f"Category: {exc}") from None


def add_attraction(city: Location, name: str, category: str, setting: str, wind_sensitive: bool,
                   lat: float, lon: float, user_csv=USER_ATTRACTIONS_CSV,
                   curated_csv=ATTRACTIONS_CSV) -> tuple[Attraction, bool]:
    """Validate and remember a user-added attraction. Returns (attraction, is_new).

    Raises InvalidInputError for bad input; is_new is False if the city already has that name.
    """
    if setting not in SETTINGS:
        raise InvalidInputError(f"Choose where it takes place: {', '.join(SETTINGS)}.")
    lat, lon = validate_poi_coordinates(city, lat, lon)
    attraction = Attraction(city.city, validate_poi_name(name), _clean_category(category),
                            setting, bool(wind_sensitive), lat, lon)
    return attraction, save_user_attraction(attraction, user_csv, curated_csv)


def city_attractions(attractions: list[Attraction], city: str) -> list[Attraction]:
    return [a for a in attractions if a.city == city]


def assess_closeup(attractions: list[Attraction], batch: BatchResult, assumptions: Assumptions | None = None):
    """Verdict for each attraction from the grid weather nearest to it."""
    return assess_city(attractions, batch.snapshots, assumptions) if batch.snapshots else []
