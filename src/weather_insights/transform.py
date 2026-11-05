"""Transform: turn snapshots into analytics and attraction advice.

Everything here is a pure function of its inputs (no I/O, no Streamlit), so each piece can
be checked against a small hand-made case with a known answer (see tests/).
"""

from __future__ import annotations

import math
from typing import Iterable

import numpy as np
import pandas as pd

from .models import (
    LOG_COLUMNS, Assessment, Assumptions, Attraction, Location, Verdict, WeatherSnapshot, comfort_score,
)

LOW_VISIBILITY_M = 1000.0  # below this, sightseeing is poor whatever the other settings say

METRICS = {  # column -> label with unit
    "temp": "Temperature (°C)",
    "feels_like": "Feels like (°C)",
    "humidity": "Humidity (%)",
    "wind_speed": "Wind speed (m/s)",
    "precip_mm": "Rain / snow (mm/h)",
    "clouds": "Cloud cover (%)",
    "comfort_index": "Comfort index (0-100)",
    "feels_like_delta": "Feels-like minus actual (°C)",
    "temp_range": "Daily temperature range (°C)",
}


# ---- snapshots <-> DataFrames --------------------------------------------------------------------
def add_derived_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """Add precip_mm, feels_like_delta and comfort_index (same formulas as WeatherSnapshot)."""
    out = df.copy()
    out["precip_mm"] = out["rain_1h"] + out["snow_1h"]
    out["feels_like_delta"] = out["feels_like"] - out["temp"]
    out["comfort_index"] = comfort_score(out["feels_like"], out["humidity"], out["wind_speed"], out["precip_mm"])
    return out


def snapshots_to_frame(snapshots: Iterable[WeatherSnapshot]) -> pd.DataFrame:
    rows = [s.to_record() for s in snapshots]
    df = pd.DataFrame(rows, columns=list(LOG_COLUMNS))
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return add_derived_metrics(df)


# ---- analytical view 1: trend over time, across all cities (uses our own logged history) ------
def metric_timeseries(history: pd.DataFrame, metric: str, freq: str | None = None) -> pd.DataFrame:
    """One metric over time for every city: columns city, timestamp, value.

    With ``freq`` (e.g. '1h', '1D') observations are averaged per city and period.
    """
    cols = history[["city", "timestamp", metric]].dropna()
    if freq:
        out = (cols.set_index("timestamp").groupby("city")[metric].resample(freq).mean().dropna().reset_index())
    else:
        out = cols.sort_values(["city", "timestamp"])
    return out.rename(columns={metric: "value"}).reset_index(drop=True)


def daily_summary(history: pd.DataFrame, city: str | None = None) -> pd.DataFrame:
    """Per city and day: min / max / mean temperature and the daily range (a derived metric)."""
    sub = history if city is None else history[history["city"] == city]
    columns = ["city", "date", "temp_min", "temp_max", "temp_mean", "temp_range", "observations"]
    if sub.empty:
        return pd.DataFrame(columns=columns)
    sub = sub.assign(date=sub["timestamp"].dt.date)
    grouped = sub.groupby(["city", "date"])["temp"].agg(
        temp_min="min", temp_max="max", temp_mean="mean", observations="count")
    grouped["temp_range"] = grouped["temp_max"] - grouped["temp_min"]
    return grouped.reset_index()[columns]


# ---- analytical view 2: cross-location comparison --------------------------------------------
def latest_per_city(history: pd.DataFrame) -> pd.DataFrame:
    """Most recent logged observation of every city."""
    if history.empty:
        return history.copy()
    return history.sort_values("timestamp").groupby("city", as_index=False).tail(1).reset_index(drop=True)


def rank_cities(df: pd.DataFrame, metric: str, n: int | None = None, ascending: bool = False) -> pd.DataFrame:
    """Sort cities by a metric (default: highest first) and keep the top n."""
    ranked = df.sort_values(metric, ascending=ascending).reset_index(drop=True)
    ranked.insert(0, "rank", range(1, len(ranked) + 1))
    return ranked.head(n) if n else ranked


def extremes(df: pd.DataFrame) -> dict[str, pd.Series]:
    """Hottest, coldest, windiest, most/least comfortable city of a one-row-per-city frame."""
    if df.empty:
        return {}
    pick = lambda col, fn: df.loc[getattr(df[col], fn)()]  # noqa: E731
    return {
        "hottest": pick("temp", "idxmax"),
        "coldest": pick("temp", "idxmin"),
        "windiest": pick("wind_speed", "idxmax"),
        "most_comfortable": pick("comfort_index", "idxmax"),
        "least_comfortable": pick("comfort_index", "idxmin"),
    }


def interpolate_field(df: pd.DataFrame, metric: str, lon_step: float = 0.5, lat_limit: float = 75.0,
                      full_km: float = 1800.0, fade_km: float = 4500.0) -> dict:
    """Smooth map of `metric` between the sampled cities (inverse-distance weighting).

    Returns a dict with ``values`` and ``alpha`` (2-D arrays, row 0 = northernmost), the cell
    centres ``lats``/``lons`` and the bounding box ``north/south/west/east``. Rows are evenly
    spaced in Web-Mercator y, so the raster can be stretched over a web map without distortion.
    ``alpha`` is 1 within ``full_km`` of the nearest city and fades to 0 at ``fade_km``: the map
    does not pretend to know the weather far away from any measurement.
    """
    lats_c = np.radians(df["lat"].to_numpy(float))
    lons_c = np.radians(df["lon"].to_numpy(float))
    vals_c = df[metric].to_numpy(float)

    n_cols = int(round(360 / lon_step))
    y_max = math.log(math.tan(math.pi / 4 + math.radians(lat_limit) / 2))
    n_rows = int(round(2 * y_max / (2 * math.pi) * n_cols))
    lons = -180 + (np.arange(n_cols) + 0.5) * lon_step
    ys = y_max - (np.arange(n_rows) + 0.5) * (2 * y_max / n_rows)
    lats = np.degrees(2 * np.arctan(np.exp(ys)) - math.pi / 2)

    lat_g = np.radians(lats)[:, None, None]
    lon_g = np.radians(lons)[None, :, None]
    h = np.sin((lats_c - lat_g) / 2) ** 2 + np.cos(lat_g) * np.cos(lats_c) * np.sin((lons_c - lon_g) / 2) ** 2
    dist_km = 2 * 6371.0088 * np.arcsin(np.sqrt(np.clip(h, 0, 1)))  # rows x cols x cities

    # Softening of 300 km keeps the field smooth (no 'bullseye' around each city) while a city's own
    # value still dominates its surroundings.
    weights = 1.0 / (dist_km + 300.0) ** 2
    values = (weights * vals_c).sum(axis=2) / weights.sum(axis=2)
    alpha = np.clip((fade_km - dist_km.min(axis=2)) / (fade_km - full_km), 0.0, 1.0)
    return {"values": values, "alpha": alpha, "lats": lats, "lons": lons,
            "north": lat_limit, "south": -lat_limit, "west": -180.0, "east": 180.0}


def history_coverage(history: pd.DataFrame) -> dict:
    if history.empty:
        return {"rows": 0, "cities": 0, "first": None, "last": None}
    return {
        "rows": len(history),
        "cities": history["city"].nunique(),
        "first": history["timestamp"].min(),
        "last": history["timestamp"].max(),
    }


# ---- geography helpers --------------------------------------------------------------------------
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dlmb = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def build_grid(center: Location, n: int, radius_km: float) -> list[Location]:
    """n x n sample points covering a square of +/- radius_km around a city centre."""
    if n < 1:
        raise ValueError("Grid size must be at least 1.")
    km_per_deg_lat = 111.32
    km_per_deg_lon = km_per_deg_lat * max(math.cos(math.radians(center.lat)), 0.01)
    if n == 1:
        steps = [0.0]
    else:
        steps = [-radius_km + 2 * radius_km * i / (n - 1) for i in range(n)]
    points = []
    for i, dy in enumerate(steps):
        for j, dx in enumerate(steps):
            points.append(Location(f"{center.city} grid {i},{j}", center.country,
                                   center.lat + dy / km_per_deg_lat, center.lon + dx / km_per_deg_lon))
    return points


def nearest_snapshot(snapshots: list[WeatherSnapshot], lat: float, lon: float) -> WeatherSnapshot:
    """The sampled point closest to (lat, lon): the weather an attraction actually experiences."""
    return min(snapshots, key=lambda s: haversine_km(lat, lon, s.location.lat, s.location.lon))


# Fixed colour scales: the same colour always means the same value, on every map and for every city.
COLOUR_RANGES = {
    "temp": (0.0, 38.0),  # deg C
    "feels_like": (0.0, 38.0),  # deg C
    "humidity": (0.0, 100.0),  # %
    "clouds": (0.0, 100.0),  # %
    "wind_speed": (0.0, 20.0),  # m/s (a gale is about 17)
    "precip_mm": (0.0, 5.0),  # mm/h (7.6 and above is heavy rain)
    "comfort_index": (0.0, 100.0),
}


def value_range(metric: str, values, fit: bool = False) -> tuple[float, float]:
    """Colour-scale limits for a metric.

    By default these are the fixed ``COLOUR_RANGES`` (values outside are shown in the end colour).
    With ``fit=True`` the scale is stretched to the data instead, which shows small local
    differences but makes colours mean different things on different maps. Rain and wind cannot
    be negative, so even a fitted scale for them starts at 0.
    """
    if not fit and metric in COLOUR_RANGES:
        return COLOUR_RANGES[metric]
    arr = np.asarray(values, dtype=float)
    lo, hi = float(np.nanmin(arr)), float(np.nanmax(arr))
    if metric == "precip_mm":
        return 0.0, max(hi, 1.0)
    if metric == "wind_speed":
        return 0.0, max(hi, 5.0)
    return lo, hi


def _mercator_y(lat_deg):
    return np.log(np.tan(np.pi / 4 + np.radians(lat_deg) / 2))


def _cubic_axis(values: np.ndarray, u: np.ndarray, axis: int) -> np.ndarray:
    """Catmull-Rom (smooth cubic) interpolation along one axis at fractional indices ``u``."""
    n = values.shape[axis]
    i = np.clip(np.floor(u).astype(int), 0, n - 2)
    t = u - i
    weights = (0.5 * (-t**3 + 2 * t**2 - t), 0.5 * (3 * t**3 - 5 * t**2 + 2),
               0.5 * (-3 * t**3 + 4 * t**2 + t), 0.5 * (t**3 - t**2))
    shape = [1] * values.ndim
    shape[axis] = -1
    out = 0
    for offset, w in zip((-1, 0, 1, 2), weights):
        out = out + w.reshape(shape) * np.take(values, np.clip(i + offset, 0, n - 1), axis=axis)
    return out


def smooth_closeup_field(df: pd.DataFrame, center: Location, grid_size: int, radius_km: float,
                         metric: str, out_size: int = 320) -> dict | None:
    """A continuous picture of ``metric`` built from the n x n real readings (no extra API calls).

    The readings sit on a regular lattice (see ``build_grid``); a smooth cubic surface is passed
    through them and sampled on a fine ``out_size`` x ``out_size`` raster, row 0 = north, with rows
    evenly spaced in Web-Mercator y so the picture can be stretched over a web map. Lattice points
    whose request failed are filled in from their neighbours. Returns None if nothing is known.
    """
    nodes = build_grid(center, grid_size, radius_km)
    lookup = {(round(lat, 6), round(lon, 6)): v for lat, lon, v in zip(df["lat"], df["lon"], df[metric])}
    z = np.array([lookup.get((round(nd.lat, 6), round(nd.lon, 6)), np.nan) for nd in nodes], dtype=float)
    if np.isnan(z).all():
        return None
    node_lat = np.array([nd.lat for nd in nodes])
    node_lon = np.array([nd.lon for nd in nodes])
    lat_s, lat_n = node_lat.min(), node_lat.max()
    lon_w, lon_e = node_lon.min(), node_lon.max()

    missing = np.isnan(z)
    if missing.any():  # inverse-distance fill from the readings we do have
        x = (node_lon - center.lon) * math.cos(math.radians(center.lat))
        y = node_lat - center.lat
        for k in np.flatnonzero(missing):
            d2 = (x[~missing] - x[k]) ** 2 + (y[~missing] - y[k]) ** 2
            w = 1.0 / (d2 + 1e-12)
            z[k] = (w * z[~missing]).sum() / w.sum()

    if grid_size < 2:
        values = np.full((out_size, out_size), z[0])
        return {"values": values, "north": center.lat + radius_km / 111.32, "south": center.lat - radius_km / 111.32,
                "west": center.lon - radius_km / (111.32 * math.cos(math.radians(center.lat))),
                "east": center.lon + radius_km / (111.32 * math.cos(math.radians(center.lat)))}

    lattice = z.reshape(grid_size, grid_size)  # [lat index (south->north), lon index (west->east)]
    y_rows = np.linspace(_mercator_y(lat_n), _mercator_y(lat_s), out_size)  # north first, even in Mercator y
    lat_rows = np.degrees(2 * np.arctan(np.exp(y_rows)) - np.pi / 2)
    u_lat = (lat_rows - lat_s) / (lat_n - lat_s) * (grid_size - 1)
    u_lon = np.linspace(0, grid_size - 1, out_size)
    values = _cubic_axis(_cubic_axis(lattice, u_lon, axis=1), u_lat, axis=0)
    values = np.clip(values, lattice.min(), lattice.max())  # a cubic can overshoot slightly
    return {"values": values, "north": float(lat_n), "south": float(lat_s), "west": float(lon_w), "east": float(lon_e)}


# ---- attraction rules ------------------------------------------------------------------------------
def _outdoor_exposure(snap: WeatherSnapshot, wind_sensitive: bool, a: Assumptions) -> tuple[Verdict, list[str]]:
    """How bad is the weather for someone standing outside here? Returns verdict + reasons."""
    avoid: list[str] = []
    caution: list[str] = []

    if snap.group == "Thunderstorm":
        avoid.append("Thunderstorm")

    precip = snap.precip_mm
    if precip == 0 and snap.group in ("Rain", "Drizzle", "Snow"):
        precip = 0.1  # the condition says it is raining/snowing but no amount was reported
    kind = "snow" if snap.snow_1h > snap.rain_1h else "rain"
    amount = f" ({snap.precip_mm:.1f} mm/h)" if snap.precip_mm > 0 else ""
    if precip >= a.heavy_rain:
        avoid.append(f"Heavy {kind}{amount}")
    elif precip > a.max_rain:
        caution.append(f"{'Drizzle' if snap.group == 'Drizzle' else kind.capitalize()}{amount}")
    elif snap.pop is not None and snap.pop >= 0.6 and a.max_rain < 1.0:
        caution.append(f"{snap.pop:.0%} chance of precipitation")

    wind = snap.wind_speed
    if wind >= a.gale_wind:
        avoid.append(f"Gale-force wind ({wind:.1f} m/s)")
    elif wind_sensitive and wind >= a.sensitive_close_wind:
        avoid.append(f"Wind {wind:.1f} m/s: cable cars, boats and towers usually suspend service")
    elif wind >= a.max_wind:
        caution.append(f"Strong wind ({wind:.1f} m/s, your limit is {a.max_wind:.1f})")
    elif wind_sensitive and wind >= a.sensitive_warn_wind:
        caution.append(f"Wind {wind:.1f} m/s: possible delays or closures")

    feels = snap.feels_like
    if feels >= a.heat_limit:
        avoid.append(f"Dangerous heat (feels like {feels:.0f}°C)")
    elif feels <= a.cold_limit:
        avoid.append(f"Dangerous cold (feels like {feels:.0f}°C)")
    elif feels > a.temp_max:
        caution.append(f"Too hot (feels like {feels:.0f}°C, your limit is {a.temp_max:.0f}°C)")
    elif feels < a.temp_min:
        caution.append(f"Too cold (feels like {feels:.0f}°C, your limit is {a.temp_min:.0f}°C)")

    if snap.visibility < LOW_VISIBILITY_M:
        caution.append(f"Low visibility ({snap.visibility:.0f} m)")

    if avoid:
        return Verdict.AVOID, avoid + caution
    if caution:
        return Verdict.CAUTION, caution
    return Verdict.GOOD, []


def assess_attraction(attraction: Attraction, snap: WeatherSnapshot,
                      assumptions: Assumptions | None = None) -> Assessment:
    """Combine the attraction's type with the weather at its location into a verdict."""
    a = assumptions or Assumptions()
    exposure, reasons = _outdoor_exposure(snap, attraction.wind_sensitive, a)

    if attraction.setting == "outdoor":
        verdict = exposure
        if verdict is Verdict.GOOD:
            reasons = ["Weather suits an outdoor visit"]
    elif attraction.setting == "mixed":
        # Indoor parts remain available, so only severe weather downgrades the visit.
        if exposure is Verdict.AVOID:
            verdict, reasons = Verdict.CAUTION, [f"Outdoor parts affected: {r}" for r in reasons]
        else:
            verdict = Verdict.GOOD
            reasons = [f"Bring protection: {r}" for r in reasons] or ["Weather is fine, indoors and out"]
    else:  # indoor
        verdict = Verdict.GOOD
        reasons = ["Weather-proof"]
        if attraction.wind_sensitive and snap.wind_speed >= a.sensitive_close_wind:
            verdict = Verdict.CAUTION
            reasons = [f"Wind {snap.wind_speed:.1f} m/s: observation decks may close"]
        elif exposure is not Verdict.GOOD:
            reasons = ["Good shelter from today's weather"]
    return Assessment(attraction, verdict, tuple(reasons), snap)


def assess_city(attractions: Iterable[Attraction], snapshots: list[WeatherSnapshot],
                assumptions: Assumptions | None = None) -> list[Assessment]:
    """Assess each attraction with the sampled weather point nearest to it."""
    return [assess_attraction(a, nearest_snapshot(snapshots, a.lat, a.lon), assumptions) for a in attractions]


def assessments_to_frame(assessments: Iterable[Assessment]) -> pd.DataFrame:
    rows = [{
        "Attraction": a.attraction.name,
        "Type": a.attraction.category,
        "Setting": a.attraction.setting,
        "Verdict": a.verdict.label,
        "Label": f"{a.verdict.emoji} {a.attraction.name}",
        "verdict_key": a.verdict.value,
        "Why": "; ".join(a.reasons),
        "Temp (°C)": round(a.snapshot.temp, 1),
        "Wind (m/s)": round(a.snapshot.wind_speed, 1),
        "lat": a.attraction.lat,
        "lon": a.attraction.lon,
    } for a in assessments]
    df = pd.DataFrame(rows)
    if not df.empty:
        order = {"good": 0, "caution": 1, "avoid": 2}
        df = df.sort_values("verdict_key", key=lambda s: s.map(order), kind="stable").reset_index(drop=True)
    return df


# ---- forecast: "which day should I go?" ---------------------------------------------------------
def visit_outlook(attraction: Attraction, forecast: list[WeatherSnapshot],
                  assumptions: Assumptions | None = None) -> pd.DataFrame:
    """Day-by-day verdict for one attraction from the 3-hourly forecast (daytime slots only).

    A day is GOOD if at least 70 % of its daytime slots are GOOD, AVOID if at least half are
    AVOID, otherwise CAUTION.
    """
    rows = []
    for snap in forecast:
        hour = snap.local_time.hour
        if 8 <= hour <= 20:
            a = assess_attraction(attraction, snap, assumptions)
            rows.append({
                "date": snap.local_time.date(), "verdict": a.verdict.value, "reasons": a.reasons,
                "temp": snap.temp, "pop": snap.pop or 0.0, "precip_mm": snap.precip_mm,
            })
    if not rows:
        return pd.DataFrame(columns=["date", "verdict", "good_share", "temp_min", "temp_max", "max_pop", "why"])
    df = pd.DataFrame(rows)
    out = []
    for date, g in df.groupby("date"):
        good_share = (g["verdict"] == "good").mean()
        avoid_share = (g["verdict"] == "avoid").mean()
        verdict = "good" if good_share >= 0.7 else "avoid" if avoid_share >= 0.5 else "caution"
        reasons = sorted({r for rs in g["reasons"] for r in rs})
        out.append({
            "date": date, "verdict": verdict, "good_share": good_share,
            "temp_min": g["temp"].min(), "temp_max": g["temp"].max(),
            "max_pop": g["pop"].max(), "why": "; ".join(reasons[:3]),
        })
    return pd.DataFrame(out)


def best_day(outlook: pd.DataFrame):
    """Date with the highest share of good daytime slots (earliest wins ties), or None."""
    if outlook.empty:
        return None
    return outlook.sort_values(["good_share", "date"], ascending=[False, True]).iloc[0]["date"]
