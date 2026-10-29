"""Domain objects: locations, weather snapshots, attractions and visit assessments."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

import numpy as np

IDEAL_TEMP_C = 21.0

# Column order of the CSV log written by load.py (one row per successful fetch).
LOG_COLUMNS = (
    "timestamp", "city", "country", "lat", "lon", "temp", "feels_like", "humidity",
    "pressure", "wind_speed", "clouds", "visibility", "rain_1h", "snow_1h",
    "condition_id", "condition", "description",
)


def comfort_score(feels_like, humidity, wind_speed, precip_mm):
    """Comfort index from 0 (miserable) to 100 (ideal). Works on scalars and pandas Series.

    Starts at 100 and subtracts penalties: 3 points per degree of "feels like" away from
    21 C, 0.3 per humidity point above 60 %, 1.5 per m/s of wind above 5, and 5 per mm of
    hourly precipitation (capped at 4 mm).
    """
    score = (
        100.0
        - 3.0 * np.abs(feels_like - IDEAL_TEMP_C)
        - 0.3 * np.maximum(0, humidity - 60)
        - 1.5 * np.maximum(0, wind_speed - 5)
        - 5.0 * np.minimum(precip_mm, 4)
    )
    return np.clip(score, 0, 100)


def condition_group(condition_id: int) -> str:
    """Map an OpenWeatherMap condition id to a readable group."""
    if 200 <= condition_id < 300:
        return "Thunderstorm"
    if 300 <= condition_id < 400:
        return "Drizzle"
    if 500 <= condition_id < 600:
        return "Rain"
    if 600 <= condition_id < 700:
        return "Snow"
    if 700 <= condition_id < 800:
        return "Atmosphere"
    if condition_id == 800:
        return "Clear"
    return "Clouds"


@dataclass(frozen=True)
class Location:
    """A named place on the map."""

    city: str
    country: str
    lat: float
    lon: float

    def __str__(self) -> str:
        return f"{self.city}, {self.country}" if self.country else self.city


@dataclass(frozen=True)
class WeatherSnapshot:
    """Weather at one place and one moment (an observation or one forecast slot)."""

    location: Location
    timestamp: datetime  # timezone-aware, UTC
    temp: float
    feels_like: float
    humidity: float
    pressure: float
    wind_speed: float  # m/s
    clouds: float
    visibility: float  # metres
    rain_1h: float  # mm per hour
    snow_1h: float  # mm per hour
    condition_id: int
    condition: str
    description: str
    tz_offset: int = 0  # seconds east of UTC at this location
    pop: float | None = field(default=None, compare=False)  # forecast rain probability, 0-1

    # ---- construction -------------------------------------------------------------------
    @classmethod
    def from_api(cls, payload: dict, location: Location | None = None) -> "WeatherSnapshot":
        """Build a snapshot from a /weather response (or one entry of a /forecast response)."""
        main = payload["main"]
        weather = payload["weather"][0]
        wind = payload.get("wind", {})
        rain = payload.get("rain", {})
        snow = payload.get("snow", {})
        if location is None:
            sys = payload.get("sys", {})
            coord = payload["coord"]
            location = Location(payload["name"], sys.get("country", ""), coord["lat"], coord["lon"])
        return cls(
            location=location,
            timestamp=datetime.fromtimestamp(payload["dt"], tz=timezone.utc),
            temp=float(main["temp"]),
            feels_like=float(main.get("feels_like", main["temp"])),
            humidity=float(main.get("humidity", 0)),
            pressure=float(main.get("pressure", 0)),
            wind_speed=float(wind.get("speed", 0.0)),
            clouds=float(payload.get("clouds", {}).get("all", 0)),
            visibility=float(payload.get("visibility", 10000)),
            # /weather reports "1h"; /forecast reports "3h" -> convert to an hourly rate
            rain_1h=float(rain.get("1h", rain.get("3h", 0.0) / 3)),
            snow_1h=float(snow.get("1h", snow.get("3h", 0.0) / 3)),
            condition_id=int(weather["id"]),
            condition=weather["main"],
            description=weather["description"],
            tz_offset=int(payload.get("timezone", 0)),
            pop=float(payload["pop"]) if "pop" in payload else None,
        )

    # ---- derived values (computed from stored attributes, never stored themselves) ------
    @property
    def city(self) -> str:
        return self.location.city

    @property
    def precip_mm(self) -> float:
        """Rain plus snow (water equivalent), mm per hour."""
        return self.rain_1h + self.snow_1h

    @property
    def feels_like_delta(self) -> float:
        """How much warmer (+) or colder (-) it feels than the thermometer says."""
        return self.feels_like - self.temp

    @property
    def comfort_index(self) -> float:
        return float(comfort_score(self.feels_like, self.humidity, self.wind_speed, self.precip_mm))

    @property
    def group(self) -> str:
        return condition_group(self.condition_id)

    @property
    def local_time(self) -> datetime:
        return self.timestamp + timedelta(seconds=self.tz_offset)

    def to_record(self) -> dict:
        """Flat dict matching LOG_COLUMNS, ready to be appended to the CSV log."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "city": self.location.city,
            "country": self.location.country,
            "lat": self.location.lat,
            "lon": self.location.lon,
            "temp": self.temp,
            "feels_like": self.feels_like,
            "humidity": self.humidity,
            "pressure": self.pressure,
            "wind_speed": self.wind_speed,
            "clouds": self.clouds,
            "visibility": self.visibility,
            "rain_1h": self.rain_1h,
            "snow_1h": self.snow_1h,
            "condition_id": self.condition_id,
            "condition": self.condition,
            "description": self.description,
        }

    # ---- special methods ----------------------------------------------------------------
    def __str__(self) -> str:
        return f"{self.location}: {self.temp:.1f}°C, {self.description}"

    def __repr__(self) -> str:
        return f"WeatherSnapshot({self.location.city!r}, {self.temp:.1f}°C, {self.description!r})"

    def __lt__(self, other: "WeatherSnapshot") -> bool:
        """Snapshots sort by temperature, so ``sorted(snapshots)`` goes coldest to hottest."""
        if not isinstance(other, WeatherSnapshot):
            return NotImplemented
        return self.temp < other.temp


class Verdict(str, Enum):
    """Can you go there today?"""

    GOOD = "good"
    CAUTION = "caution"
    AVOID = "avoid"

    @property
    def emoji(self) -> str:
        return {"good": "✅", "caution": "⚠️", "avoid": "⛔"}[self.value]

    @property
    def plain(self) -> str:
        return {"good": "Good to go", "caution": "Not ideal", "avoid": "Not recommended"}[self.value]

    @property
    def label(self) -> str:
        return f"{self.emoji} {self.plain}"

    @property
    def severity(self) -> int:
        return {"good": 0, "caution": 1, "avoid": 2}[self.value]


@dataclass(frozen=True)
class Assumptions:
    """What the group considers acceptable weather for a visit (editable from the sidebar).

    Temperature applies to the *feels-like* temperature. Wind and rain are the highest values
    still considered pleasant; beyond them a visit is "not ideal", and well beyond them it is
    "not recommended" (see the derived limits below).
    """

    temp_min: float = 5.0  # deg C
    temp_max: float = 32.0  # deg C
    max_wind: float = 10.8  # m/s (Beaufort 6)
    max_rain: float = 0.0  # mm/h of rain or snow water equivalent

    # Reference points the other limits scale from (the original fixed rules).
    _REF_WIND = 10.8
    HEAT_MARGIN = 7.0  # degrees above temp_max where heat becomes dangerous
    COLD_MARGIN = 15.0  # degrees below temp_min where cold becomes dangerous
    HEAVY_RAIN_MM_H = 7.6  # WMO "heavy rain"

    def __post_init__(self):
        if self.temp_min > self.temp_max:
            raise ValueError("Minimum temperature cannot be above the maximum.")
        if self.max_wind <= 0:
            raise ValueError("Maximum wind must be positive.")
        if self.max_rain < 0:
            raise ValueError("Maximum rain cannot be negative.")

    @property
    def heat_limit(self) -> float:
        return self.temp_max + self.HEAT_MARGIN

    @property
    def cold_limit(self) -> float:
        return self.temp_min - self.COLD_MARGIN

    @property
    def gale_wind(self) -> float:
        """Wind at which any outdoor visit is not recommended (Beaufort 8 at the default)."""
        return 17.2 * self.max_wind / self._REF_WIND

    @property
    def sensitive_close_wind(self) -> float:
        """Wind at which cable cars, boats and towers usually suspend service."""
        return 13.9 * self.max_wind / self._REF_WIND

    @property
    def sensitive_warn_wind(self) -> float:
        return 8.0 * self.max_wind / self._REF_WIND

    @property
    def heavy_rain(self) -> float:
        return max(self.HEAVY_RAIN_MM_H, 2 * self.max_rain)

    def acceptable_range(self, metric: str) -> tuple[float, float] | None:
        """(low, high) band to draw on charts for a metric, or None if no assumption covers it."""
        return {
            "temp": (self.temp_min, self.temp_max),
            "feels_like": (self.temp_min, self.temp_max),
            "wind_speed": (0.0, self.max_wind),
            "precip_mm": (0.0, self.max_rain),
        }.get(metric)


@dataclass(frozen=True)
class Attraction:
    """A visitable place whose enjoyment (or opening) depends on the weather."""

    city: str
    name: str
    category: str
    setting: str  # "outdoor", "indoor" or "mixed"
    wind_sensitive: bool  # cable cars, boats, towers... usually suspend operation in high wind
    lat: float
    lon: float

    def __str__(self) -> str:
        return f"{self.name} ({self.category}, {self.setting})"


@dataclass(frozen=True)
class Assessment:
    """The weather verdict for one attraction."""

    attraction: Attraction
    verdict: Verdict
    reasons: tuple[str, ...]
    snapshot: WeatherSnapshot

    def __str__(self) -> str:
        return f"{self.attraction.name}: {self.verdict.label} ({'; '.join(self.reasons)})"
