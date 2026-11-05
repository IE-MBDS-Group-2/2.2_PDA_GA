"""Load: read and write everything that lives on disk (catalogues and the weather log)."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable

import pandas as pd

from .config import ATTRACTIONS_CSV, CITIES_CSV, LOG_CSV, MIN_LOG_INTERVAL_MIN, USER_CITIES_CSV
from .models import LOG_COLUMNS, Attraction, Location, WeatherSnapshot
from .transform import add_derived_metrics


# ---- catalogues ----------------------------------------------------------------------------------
def load_cities(cities_csv: Path = CITIES_CSV, user_csv: Path = USER_CITIES_CSV) -> list[Location]:
    """Curated world cities followed by the cities users added (duplicates removed)."""
    frames = [pd.read_csv(p) for p in (cities_csv, user_csv) if Path(p).exists()]
    if not frames:
        return []
    df = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["city", "country"])
    df["country"] = df["country"].fillna("")
    return [Location(r.city, r.country, float(r.lat), float(r.lon)) for r in df.itertuples()]


def save_user_city(location: Location, user_csv: Path = USER_CITIES_CSV) -> bool:
    """Remember a user-added city. Returns False if it was already tracked."""
    if any(c.city == location.city and c.country == location.country for c in load_cities(user_csv=user_csv)):
        return False
    user_csv = Path(user_csv)
    user_csv.parent.mkdir(parents=True, exist_ok=True)
    is_new = not user_csv.exists()
    with open(user_csv, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["city", "country", "lat", "lon"])
        writer.writerow([location.city, location.country, location.lat, location.lon])
    return True


def load_attractions(path: Path = ATTRACTIONS_CSV) -> list[Attraction]:
    if not Path(path).exists():
        return []
    df = pd.read_csv(path)
    return [
        Attraction(r.city, r.name, r.category, r.setting, str(r.wind_sensitive).lower() == "true", float(r.lat), float(r.lon))
        for r in df.itertuples()
    ]


# ---- the weather log (our own time series) --------------------------------------------------------
def _empty_history() -> pd.DataFrame:
    df = pd.DataFrame(columns=list(LOG_COLUMNS))
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    numeric = [c for c in LOG_COLUMNS if c not in ("timestamp", "city", "country", "condition", "description")]
    df[numeric] = df[numeric].astype("float64")
    return add_derived_metrics(df)


def load_history(path: Path = LOG_CSV) -> pd.DataFrame:
    """Read the whole log with parsed timestamps and derived metrics. Empty frame if no log yet."""
    if not Path(path).exists() or Path(path).stat().st_size == 0:
        return _empty_history()
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return add_derived_metrics(df.sort_values("timestamp").reset_index(drop=True))


def append_snapshots(snapshots: Iterable[WeatherSnapshot], path: Path = LOG_CSV,
                     min_interval_min: float = MIN_LOG_INTERVAL_MIN) -> int:
    """Append snapshots to the CSV log; return how many rows were actually written.

    A city already logged less than ``min_interval_min`` minutes before the new snapshot is
    skipped, so repeated clicks on "Refresh" do not flood the series with duplicates.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    last_seen: dict[str, pd.Timestamp] = {}
    if path.exists() and path.stat().st_size > 0:
        existing = pd.read_csv(path, usecols=["timestamp", "city", "country"])
        existing["timestamp"] = pd.to_datetime(existing["timestamp"], utc=True)
        existing["key"] = existing["city"] + "|" + existing["country"].fillna("")
        last_seen = existing.groupby("key")["timestamp"].max().to_dict()

    to_write = []
    for snap in snapshots:
        key = f"{snap.location.city}|{snap.location.country}"
        previous = last_seen.get(key)
        if previous is not None and (pd.Timestamp(snap.timestamp) - previous).total_seconds() < min_interval_min * 60:
            continue
        to_write.append(snap.to_record())
        last_seen[key] = pd.Timestamp(snap.timestamp)

    if not to_write:
        return 0
    is_new = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(LOG_COLUMNS))
        if is_new:
            writer.writeheader()
        writer.writerows(to_write)
    return len(to_write)
