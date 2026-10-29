import pandas as pd

from conftest import owm_payload
from weather_insights import load
from weather_insights.models import LOG_COLUMNS, Location, WeatherSnapshot


def snap(city="Madrid", ts=1_760_000_000, **kw):
    return WeatherSnapshot.from_api(owm_payload(dt=ts, **kw), Location(city, "ES", 40.0, -3.0))


def test_append_creates_file_with_header_then_accumulates(tmp_path):
    log = tmp_path / "data" / "log.csv"
    assert load.append_snapshots([snap("A"), snap("B")], log) == 2
    assert load.append_snapshots([snap("A", ts=1_760_000_000 + 3600)], log) == 1
    df = pd.read_csv(log)
    assert list(df.columns) == list(LOG_COLUMNS) and len(df) == 3


def test_recent_duplicates_are_skipped(tmp_path):
    log = tmp_path / "log.csv"
    load.append_snapshots([snap("A")], log)
    assert load.append_snapshots([snap("A", ts=1_760_000_000 + 60)], log) == 0  # 1 min later
    assert load.append_snapshots([snap("A", ts=1_760_000_000 + 11 * 60)], log) == 1  # 11 min later
    assert len(pd.read_csv(log)) == 2


def test_load_history_adds_derived_and_handles_missing_file(tmp_path):
    log = tmp_path / "log.csv"
    empty = load.load_history(log)
    assert empty.empty and "comfort_index" in empty.columns
    load.append_snapshots([snap(temp=10, feels=6)], log)
    df = load.load_history(log)
    assert df.iloc[0]["feels_like_delta"] == -4 and str(df["timestamp"].dt.tz) == "UTC"


def test_cities_and_user_cities(tmp_path):
    base, user = tmp_path / "c.csv", tmp_path / "u.csv"
    base.write_text("city,country,lat,lon\nMadrid,ES,40.4,-3.7\n")
    assert load.save_user_city(Location("Seville", "ES", 37.4, -6.0), user) is True
    assert load.save_user_city(Location("Seville", "ES", 37.4, -6.0), user) is False
    assert [str(c) for c in load.load_cities(base, user)] == ["Madrid, ES", "Seville, ES"]


def test_shipped_catalogues_are_consistent():
    cities = {c.city: c for c in load.load_cities()}
    attractions = load.load_attractions()
    assert {a.city for a in attractions} <= set(cities)
    assert {a.setting for a in attractions} <= {"outdoor", "indoor", "mixed"}
    assert any(a.wind_sensitive for a in attractions)
    for a in attractions:
        assert -90 <= a.lat <= 90 and -180 <= a.lon <= 180


def test_default_capitals_present_and_every_city_has_up_to_ten_attractions():
    cities = {c.city: c for c in load.load_cities()}
    capitals = {"Beirut": "LB", "Washington D.C.": "US", "Quito": "EC", "Abu Dhabi": "AE",
                "Warsaw": "PL", "Manila": "PH", "Madrid": "ES"}
    for name, country in capitals.items():
        assert cities[name].country == country
    counts = {}
    for a in load.load_attractions():
        counts[a.city] = counts.get(a.city, 0) + 1
    assert set(counts) == set(cities)  # no city without attractions
    assert max(counts.values()) <= 10 and min(counts.values()) >= 8


def test_each_attraction_is_near_its_city():
    from weather_insights.transform import haversine_km
    cities = {c.city: c for c in load.load_cities()}
    far = [(a.name, round(haversine_km(a.lat, a.lon, cities[a.city].lat, cities[a.city].lon)))
           for a in load.load_attractions()
           if haversine_km(a.lat, a.lon, cities[a.city].lat, cities[a.city].lon) > 45]
    assert far == []  # catches typos in coordinates (the close-up covers at most 40 km)

