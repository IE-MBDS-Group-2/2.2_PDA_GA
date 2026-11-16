"""Domain objects: locations, weather snapshots, attractions and visit assessments."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from urllib.parse import quote_plus

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


# ---- opening hours (OpenStreetMap "opening_hours" syntax, simple patterns only) ----------------------
# Regular expressions as in the s07 datetimes/regex session; re.fullmatch must match a whole rule.
_DAY_CODES = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
_DAY = r"(?:Mo|Tu|We|Th|Fr|Sa|Su)"
_DAY_LIST = rf"{_DAY}(?:-{_DAY})?(?:\s*,\s*{_DAY}(?:-{_DAY})?)*"  # "Mo-Fr", "Mo,We", "Fr-Mo"
_CLOCK = r"(?:[01]?\d|2[0-4]):[0-5]\d"  # 0:00 or 00:00 to 24:59; 24:xx beyond 24:00 is refused below
_TIME_LIST = rf"{_CLOCK}\s*-\s*{_CLOCK}(?:\s*,\s*{_CLOCK}\s*-\s*{_CLOCK})*"  # "09:00-13:00,15:00-19:00", "9:00 - 20:15"
_RULE = re.compile(rf"(?:(?P<days>{_DAY_LIST})\s+)?(?:(?P<times>{_TIME_LIST})|(?P<off>off|closed))")
_TIME_RANGE = re.compile(r"(\d{1,2}):(\d\d)\s*-\s*(\d{1,2}):(\d\d)")
MINUTES_PER_DAY = 24 * 60


def _expand_days(days: str) -> list[int]:
    """Weekday numbers for a day list: "Mo-We,Fr" -> [0, 1, 2, 4]. Ranges may wrap: "Fr-Mo" -> [4, 5, 6, 0]."""
    result = []
    for part in days.split(","):
        first, _, last = part.strip().partition("-")
        start = _DAY_CODES.index(first)
        end = _DAY_CODES.index(last) if last else start
        result.extend((start + k) % 7 for k in range((end - start) % 7 + 1))
    return result


def _parse_week(raw: str) -> dict[int, list[tuple[int, int]]] | None:
    """Each weekday's ranges as written (an overnight range keeps end <= start), or None if not understood.

    Understands "24/7" and rules such as "Mo-Fr 09:00-17:00; Sa 10:00-14:00; Su off", several ranges per
    day and overnight ranges ("22:00-03:00"). A later rule replaces earlier ones for the days it names,
    as in OpenStreetMap. Anything else (public holidays, months, sunrise...) gives None. Never raises.
    """
    text = str(raw or "").strip()
    if text == "24/7":
        return {day: [(0, MINUTES_PER_DAY)] for day in range(7)}
    week: dict[int, list[tuple[int, int]]] = {day: [] for day in range(7)}
    found_a_rule = False
    for piece in text.split(";"):
        piece = piece.strip()
        if not piece:
            continue
        match = _RULE.fullmatch(piece)
        if match is None:
            return None
        if match["off"] and not match["days"]:
            return None  # a bare "off" would close every day: too unusual to trust
        ranges = []
        for start_h, start_m, end_h, end_m in _TIME_RANGE.findall(match["times"] or ""):
            if (start_h == "24" and start_m != "00") or (end_h == "24" and end_m != "00"):
                return None
            ranges.append((int(start_h) * 60 + int(start_m), int(end_h) * 60 + int(end_m)))
        for day in (_expand_days(match["days"]) if match["days"] else range(7)):
            week[day] = list(ranges)  # replaces what an earlier rule said about this day
        found_a_rule = True
    return week if found_a_rule else None


def _parse_opening_hours(raw: str) -> dict[int, list[tuple[int, int]]] | None:
    """Weekday (Monday = 0) -> list of (start, end) in minutes since midnight, or None if not understood.

    Like _parse_week, but an overnight range is split at midnight: start -> midnight on its own day, then
    00:00 -> end on the next day, so "is it open at minute m of day d?" is a simple lookup.
    """
    week = _parse_week(raw)
    if week is None:
        return None
    schedule: dict[int, list[tuple[int, int]]] = {day: [] for day in range(7)}
    for day, ranges in week.items():
        for start, end in ranges:
            if end > start:
                schedule[day].append((start, end))
            else:
                schedule[day].append((start, MINUTES_PER_DAY))
                if end > 0:
                    schedule[(day + 1) % 7].append((0, end))
    return schedule


# ---- opening hours as table rows (for people, not for the open-now check) ----
WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_HOLIDAY_NAMES = {"PH": "Public holidays", "SH": "School holidays"}
_LOOSE_TIMES = r"\d{1,2}:\d\d-\d{1,2}:\d\d(?:\s*,\s*\d{1,2}:\d\d-\d{1,2}:\d\d)*"
# One rule split into "when" and "hours": "Nov Th[4] off", "Oct-Mar: 10:00-18:00", "09:00-12:00".
_RULE_PARTS = re.compile(rf"(?:(?P<when>.*?)[\s:]+)?(?P<hours>{_LOOSE_TIMES}|off|closed)")


def _clock(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _tidy_when(when: str) -> str:
    """Readable "when" part: "Mo-Su" -> "Mon–Sun", "Jul 25,Oct 12" -> "Jul 25, Oct 12", "PH" -> "Public holidays"."""
    when = re.sub(r"\b(Mo|Tu|We|Th|Fr|Sa|Su)\b", lambda m: WEEKDAY_NAMES[_DAY_CODES.index(m[1])], when)
    when = re.sub(r"\b(PH|SH)\b", lambda m: _HOLIDAY_NAMES[m[1]], when)
    return re.sub(r"\s*,\s*", ", ", when.replace("-", "–")).strip()


def _tidy_hours(hours: str) -> str:
    if hours in ("off", "closed"):
        return "Closed"
    return re.sub(r"\s*,\s*", ", ", hours.replace("-", "–"))


@dataclass(frozen=True)
class OpeningHours:
    """Opening hours as written in OpenStreetMap, with an "open now?" check for simple patterns."""

    raw: str = ""

    @property
    def _text(self) -> str:
        return str(self.raw or "").strip()

    @property
    def _schedule(self) -> dict[int, list[tuple[int, int]]] | None:
        return _parse_opening_hours(self._text)

    @property
    def is_understood(self) -> bool:
        """True when the text is a pattern we can evaluate (otherwise it is only shown)."""
        return self._schedule is not None

    def is_open(self, at: datetime) -> bool | None:
        """True / False for simple patterns; None when there are no hours or we cannot tell.

        Only the weekday and clock time of ``at`` are used and its tzinfo is ignored on purpose:
        WeatherSnapshot.local_time is tagged UTC but already shifted to the city's own clock.
        The start of a range is inclusive and the end exclusive ("09:00-17:00" is closed at 17:00).
        """
        schedule = self._schedule if self else None
        if schedule is None:
            return None
        minute = at.hour * 60 + at.minute
        return any(start <= minute < end for start, end in schedule[at.weekday()])

    def heading(self, at: datetime) -> str | None:
        """Short title for the hours section of a card ("🔒 Closed now · Opening hours"), or None if blank."""
        if not self:
            return None
        is_open = self.is_open(at)
        if is_open is None:
            return "🕘 Opening hours (check before going)"
        return f"{'🕘 Open now' if is_open else '🔒 Closed now'} · Opening hours"

    def table(self) -> list[tuple[str, str]]:
        """Rows of (when, hours) that are easy to read; [] when blank.

        Simple patterns give one row per weekday, Monday first, with later rules already applied.
        Anything else gives one row per rule, as written but tidied ("Nov Th[4] off" -> "Nov Thu[4]",
        "Closed"); a piece we cannot split is shown whole in the hours column.
        """
        week = _parse_week(self._text) if self else None
        if week is not None:
            return [(WEEKDAY_NAMES[day], ", ".join(
                        "Open 24 hours" if (start, end) == (0, MINUTES_PER_DAY) else f"{_clock(start)}–{_clock(end)}"
                        for start, end in week[day]) or "Closed")
                    for day in range(7)]
        rows = []
        for piece in self._text.split(";"):
            piece = piece.strip()
            if not piece:
                continue
            parts = _RULE_PARTS.fullmatch(piece)
            if parts is None:
                rows.append(("", piece))
            else:
                rows.append((_tidy_when(parts["when"]) if parts["when"] else "Every day", _tidy_hours(parts["hours"])))
        return rows

    def __bool__(self) -> bool:
        return bool(self._text)  # False means "no hours known"

    def __str__(self) -> str:
        return self._text or "Hours not listed"


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
    opening_hours: str = ""  # OpenStreetMap syntax; blank = unknown. Last, so 7-argument calls still work.

    @property
    def hours(self) -> OpeningHours:
        return OpeningHours(self.opening_hours)

    @property
    def maps_url(self) -> str:
        """Google Maps search for "<name>, <city>" centred on this attraction's coordinates.

        The name opens the place's own page (reviews, hours, photos); the coordinates make Google
        prefer the match at this spot, and if the name is vague it lists results around the pin.
        """
        query = quote_plus(f"{self.name}, {self.city}")
        return f"https://www.google.com/maps/search/{query}/@{self.lat:.6f},{self.lon:.6f},17z"

    def __str__(self) -> str:
        return f"{self.name} ({self.category}, {self.setting})"

# a clone of the Attraction class, but with a few extra fields for the user-added attractions
@dataclass(frozen=True)
class POICandidate:
    """One possible match for a place a user typed in, before they confirm it."""

    label: str  # full address-style name from the geocoder
    lat: float
    lon: float
    osm_class: str  # OpenStreetMap feature class, e.g. "tourism"
    osm_type: str  # OpenStreetMap feature type, e.g. "museum"
    km_from_city: float
    opening_hours: str = ""  # OpenStreetMap "opening_hours" tag when the place has one

    @property
    def suggested_category(self) -> str:
        """A starting point for the Attraction category; the user can change it."""
        return self.osm_type.replace("_", " ").title() if self.osm_type else "Other"

    def __str__(self) -> str:
        return f"{self.label} ({self.km_from_city:g} km from the city centre)"


@dataclass(frozen=True)
class Assessment:
    """The weather verdict for one attraction."""

    attraction: Attraction
    verdict: Verdict
    reasons: tuple[str, ...]
    snapshot: WeatherSnapshot

    def __str__(self) -> str:
        return f"{self.attraction.name}: {self.verdict.label} ({'; '.join(self.reasons)})"
