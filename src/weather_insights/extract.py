"""Extract: talk to web APIs.

``JSONAPIClient`` knows *how* to call any JSON API (request, timeout, status handling,
error translation). ``OpenWeatherClient`` knows *which* API and which endpoints/fields.
Another provider (WeatherAPI, Open-Meteo...) could reuse the base class unchanged.
"""

from __future__ import annotations

import re
import threading
import time
import unicodedata
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Iterable

import requests

from .config import (
    API_BASE_URL, MAX_CALLS_PER_MINUTE, NOMINATIM_BASE_URL, NOMINATIM_PERIOD_S, NOMINATIM_USER_AGENT,
    POI_MAX_KM_FROM_CITY, POI_MAX_RESULTS, POI_SEARCH_BOX_DEG, REQUEST_TIMEOUT_S,
)
from .models import Location, POICandidate, WeatherSnapshot
from .transform import haversine_km


# ---- errors: one class per failure mode so callers (and users) get distinct messages --------
class APIError(Exception):
    """Base class for every error raised by this module."""


class InvalidInputError(APIError, ValueError):
    """The user's input was rejected before any request was made."""


class InvalidAPIKeyError(APIError):
    """The provider rejected the API key (HTTP 401/403)."""


class LocationNotFoundError(APIError):
    """The provider does not know the requested place (HTTP 404 or empty geocoding result)."""


class RateLimitError(APIError):
    """Too many requests (HTTP 429)."""


class NetworkError(APIError):
    """Could not reach the provider (timeout, DNS, connection refused...)."""


class ServerError(APIError):
    """The provider failed (HTTP 5xx) or returned something we cannot understand."""


# ---- input validation ------------------------------------------------------------------------
_ALLOWED_PUNCTUATION = " .,'’-"


def validate_city_name(raw: str) -> str:
    """Return a cleaned city name or raise InvalidInputError. Called before every API request."""
    if raw is None or not str(raw).strip():
        raise InvalidInputError("Please type a city name.")
    name = " ".join(str(raw).split())
    if len(name) > 100:
        raise InvalidInputError("That city name is too long (100 characters max).")
    if not any(ch.isalpha() for ch in name):
        raise InvalidInputError("A city name needs at least one letter.")
    bad = sorted({ch for ch in name if not (ch.isalpha() or ch in _ALLOWED_PUNCTUATION)})
    if bad:
        raise InvalidInputError(f"City names cannot contain: {' '.join(bad)}")
    return name

# Regex for validation
_WHITESPACE = re.compile(r"\s+")  # any run of spaces, tabs or newlines
# \w is any letter (in any alphabet), digit or underscore. A character is NOT allowed in a place
# name if it is not \w, space, or one of  . , ' ’ & ( ) / : -   -- or if it is an underscore.
_POI_DISALLOWED = re.compile(r"[^\w .,'’&()/:-]|_")
_LETTER = re.compile(r"[^\W\d_]")  # a word character that is neither a digit nor "_" is a letter


def validate_poi_name(raw: str) -> str:
    """Return a cleaned place name (park, museum...) or raise InvalidInputError.

    Unlike city names, digits are allowed: "Torre 2", "Pier 39".
    """
    if raw is None or not str(raw).strip():
        raise InvalidInputError("Please type the name of the place.")
    # NFC joins "e" + a separate accent into one "é", so pasted text compares equal to typed text.
    name = unicodedata.normalize("NFC", _WHITESPACE.sub(" ", str(raw)).strip())
    if len(name) > 100:
        raise InvalidInputError("That name is too long (100 characters max).")
    # Accents and vowel signs (Hindi, Thai...) are "marks"; \w does not match them, so check without.
    base = "".join(ch for ch in name if not unicodedata.category(ch).startswith("M"))
    if not _LETTER.search(base):
        raise InvalidInputError("A place name needs at least one letter.")
    bad = sorted(set(_POI_DISALLOWED.findall(base)))
    if bad:
        raise InvalidInputError(f"Place names cannot contain: {' '.join(bad)}")
    return name


def validate_coordinates(lat: float, lon: float) -> tuple[float, float]:
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        raise InvalidInputError("Latitude and longitude must be numbers.") from None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise InvalidInputError("Coordinates are out of range (lat -90..90, lon -180..180).")
    return lat, lon


def validate_poi_coordinates(city: Location, lat: float, lon: float) -> tuple[float, float]:
    """Hand-typed coordinates (e.g. copied from Google Maps) must be real and close to the city."""
    lat, lon = validate_coordinates(lat, lon)
    km = haversine_km(city.lat, city.lon, lat, lon)
    if km > POI_MAX_KM_FROM_CITY:
        raise InvalidInputError(
            f"That point is {km:.0f} km from {city.city}. Places must be within "
            f"{POI_MAX_KM_FROM_CITY} km of the city centre."
        )
    return round(lat, 4), round(lon, 4)


# ---- rate limiting -----------------------------------------------------------------------------------
class RateLimiter:
    """Sliding-window limiter shared by every thread: at most ``max_calls`` per ``period`` seconds.

    ``acquire()`` blocks until a call is allowed, so a big batch is slowed down instead of
    being rejected by the provider with HTTP 429.
    """

    def __init__(self, max_calls: int, period: float = 60.0,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        if max_calls < 1:
            raise ValueError("max_calls must be at least 1")
        self.max_calls, self.period = max_calls, period
        self._clock, self._sleep = clock, sleep
        self._starts: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = self._clock()
                while self._starts and now - self._starts[0] >= self.period:
                    self._starts.popleft()
                if len(self._starts) < self.max_calls:
                    self._starts.append(now)
                    return
                wait = self.period - (now - self._starts[0])
            self._sleep(max(wait, 0.01))  # sleep outside the lock so other threads are not blocked


# ---- generic client --------------------------------------------------------------------------
class JSONAPIClient:
    """Reusable GET-JSON client with provider-independent error handling."""

    service_name = "service"  # how error messages refer to the provider; subclasses override

    def __init__(self, base_url: str, auth_params: dict | None = None,
                 timeout: float = REQUEST_TIMEOUT_S, session: requests.Session | None = None,
                 limiter: RateLimiter | None = None, headers: dict | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._headers = dict(headers or {})  # sent with every request (e.g. a User-Agent)
        self._limiter = limiter  # optional: provider-specific request budget, shared by all calls
        # Name-mangled on purpose: credentials must not be reachable by accident (vars(), repr,
        # subclass code, debugging dumps). Only _get() below adds them to outgoing requests.
        self.__auth_params = dict(auth_params or {})
        self._session = session or requests.Session()  # protected: subclasses/tests may swap it

    def __repr__(self) -> str:
        return f"{type(self).__name__}(base_url={self.base_url!r})"  # never includes credentials

    def _get(self, endpoint: str, params: dict | None = None, what: str = "resource"):
        """GET ``endpoint`` and return parsed JSON, translating every failure into an APIError."""
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        query = {**(params or {}), **self.__auth_params}
        if self._limiter is not None:
            self._limiter.acquire()
        extra = {"headers": self._headers} if self._headers else {}  # only when configured
        try:
            response = self._session.get(url, params=query, timeout=self.timeout, **extra)
        except requests.Timeout:
            raise NetworkError(f"The {self.service_name} took too long to answer. Try again.") from None
        except requests.ConnectionError:
            raise NetworkError(f"Could not reach the {self.service_name}. Check your internet connection.") from None
        except requests.RequestException as exc:
            # `from None` + no str(exc): the exception text contains the full URL (with the key).
            raise NetworkError(
                f"Network problem while contacting the {self.service_name} ({type(exc).__name__})."
            ) from None
        self._check_status(response, what)
        try:
            return response.json()
        except ValueError:
            raise ServerError(f"The {self.service_name} sent a response we could not read.") from None

    def _rejected_message(self, status: int) -> str:
        """Text for HTTP 401/403. Subclasses with an API key give a more helpful hint."""
        return f"The {self.service_name} refused the request (HTTP {status})."

    def _check_status(self, response, what: str) -> None:
        status = response.status_code
        if status == 200:
            return
        if status in (401, 403):
            # hook for 401/403 text: subclasses with an API key give a better hint
            raise InvalidAPIKeyError(self._rejected_message(status))
        if status == 404:
            raise LocationNotFoundError(f"The {self.service_name} could not find that {what}.")
        if status == 429:
            raise RateLimitError(f"Too many requests to the {self.service_name}. Wait a minute and retry.")
        if status >= 500:
            raise ServerError(f"The {self.service_name} is having problems (HTTP {status}). Try again later.")
        raise APIError(f"Unexpected answer from the {self.service_name} (HTTP {status}).")


# ---- OpenWeatherMap --------------------------------------------------------------------------
class OpenWeatherClient(JSONAPIClient):
    """Knows the OpenWeatherMap endpoints and how to turn them into domain objects."""

    service_name = "weather service"

    def _rejected_message(self, status: int) -> str:
        return ("The weather service rejected the API key. Check OPENWEATHER_API_KEY in "
                ".env (new keys can take up to two hours to activate).")

    def __init__(self, api_key: str, **kwargs):
        if not api_key or not str(api_key).strip():
            raise InvalidAPIKeyError("No API key provided.")
        # The free plan allows 60 calls/minute; stay a little under it by default.
        kwargs.setdefault("limiter", RateLimiter(MAX_CALLS_PER_MINUTE))
        super().__init__(API_BASE_URL, {"appid": api_key.strip()}, **kwargs)

    def geocode(self, query: str) -> Location:
        """Resolve free text such as 'Seville' or 'Paris, FR' to a Location."""
        name = validate_city_name(query)
        results = self._get("geo/1.0/direct", {"q": name, "limit": 1}, what="city")
        if not results:
            raise LocationNotFoundError(f"No city called '{name}' was found. Check the spelling.")
        top = results[0]
        try:
            return Location(top["name"], top.get("country", ""), float(top["lat"]), float(top["lon"]))
        except (KeyError, TypeError, ValueError):
            raise ServerError("The weather service sent an unexpected geocoding answer.") from None

    def current(self, location: Location) -> WeatherSnapshot:
        """Current conditions at a location (queried by coordinates, so names are unambiguous)."""
        lat, lon = validate_coordinates(location.lat, location.lon)
        payload = self._get("data/2.5/weather", {"lat": lat, "lon": lon, "units": "metric"}, what="location")
        try:
            return WeatherSnapshot.from_api(payload, location)
        except (KeyError, IndexError, TypeError, ValueError):
            raise ServerError("The weather service sent an unexpected weather answer.") from None

    def forecast(self, location: Location) -> list[WeatherSnapshot]:
        """5-day / 3-hour forecast as a list of snapshots (free tier)."""
        lat, lon = validate_coordinates(location.lat, location.lon)
        payload = self._get("data/2.5/forecast", {"lat": lat, "lon": lon, "units": "metric"}, what="location")
        try:
            tz = int(payload.get("city", {}).get("timezone", 0))
            slots = []
            for item in payload["list"]:
                item = {**item, "timezone": tz}
                slots.append(WeatherSnapshot.from_api(item, location))
            return slots
        except (KeyError, IndexError, TypeError, ValueError):
            raise ServerError("The weather service sent an unexpected forecast answer.") from None


# ---- OpenStreetMap Nominatim (finding a named place) ------------------------------------------
# Matches that are not visitable places. Whole classes: city/district outlines (what Nominatim falls back
# to when the name is unknown) and train/transit stops. Not all of "highway": it also holds pedestrian
# squares and footpaths such as Puerta del Sol, so only its bus stops are skipped.
_SKIP_OSM_CLASSES = frozenset({"boundary", "railway", "public_transport"})
_SKIP_OSM_TYPES = frozenset({"bus_stop"})


def _to_candidates(hits: list, city: Location) -> list[POICandidate]:
    """Turn raw Nominatim hits into candidates, dropping pub transpo stops and matches far from the city.
    Kept Nominatim's passed orders hits by relevance
    """
    candidates = []
    for hit in hits:
        if hit.get("category") in _SKIP_OSM_CLASSES or hit.get("type") in _SKIP_OSM_TYPES:
            continue
        lat, lon = float(hit["lat"]), float(hit["lon"])
        km = haversine_km(city.lat, city.lon, lat, lon)
        if km > POI_MAX_KM_FROM_CITY:
            continue
        candidates.append(POICandidate(
            hit["display_name"], round(lat, 4), round(lon, 4),
            hit.get("category", ""), hit.get("type", ""), round(km, 1),
        ))
    return candidates


class NominatimClient(JSONAPIClient):
    """Finds a named place near a city with OpenStreetMap's free search (no API key needed)."""

    service_name = "place search service"

    def __init__(self, **kwargs):
        # Nominatim's usage policy: at most 1 request per second and an identifying User-Agent.
        kwargs.setdefault("limiter", RateLimiter(1, NOMINATIM_PERIOD_S))
        kwargs.setdefault("headers", {"User-Agent": NOMINATIM_USER_AGENT})
        super().__init__(NOMINATIM_BASE_URL, **kwargs)

    def search_poi(self, name: str, city: Location) -> list[POICandidate]:
        """Candidate matches for ``name`` within reach of ``city``; an empty list if none fit."""
        name = validate_poi_name(name)
        lat, lon = validate_coordinates(city.lat, city.lon)
        box = POI_SEARCH_BOX_DEG
        params = {
            "q": f"{name}, {city.city}",
            "format": "jsonv2",
            "limit": POI_MAX_RESULTS,
            "accept-language": "en",  # English labels instead of the local language
            "viewbox": f"{lon - box},{lat + box},{lon + box},{lat - box}",  # left, top, right, bottom
            "bounded": 1,  # 1 = only return results inside the box
        }
        hits = self._get("search", params, what="place")
        unexpected = ServerError("The place search service sent an unexpected answer.")
        if not isinstance(hits, list):
            raise unexpected
        try:
            return _to_candidates(hits, city)
        except (AttributeError, KeyError, TypeError, ValueError):
            raise unexpected from None


# ---- batch fetching --------------------------------------------------------------------------
@dataclass
class BatchResult:
    """Successful snapshots plus the reasons why some locations failed."""

    snapshots: list[WeatherSnapshot] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def ok_count(self) -> int:
        return len(self.snapshots)

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)


def fetch_many(client: OpenWeatherClient, locations: Iterable[Location], max_workers: int = 5) -> BatchResult:
    """Fetch current weather for many locations; one failure never stops the others."""
    locations = list(locations)
    result = BatchResult()

    def _one(loc: Location):
        try:
            return loc, client.current(loc), None
        except APIError as exc:
            return loc, None, str(exc)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for loc, snap, err in pool.map(_one, locations):  # pool.map keeps the input order
            if snap is not None:
                result.snapshots.append(snap)
            else:
                result.errors[str(loc)] = err
    return result
