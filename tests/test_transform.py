from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from conftest import MADRID, owm_payload
from weather_insights import transform as t
from weather_insights.models import Assumptions, Attraction, Location, Verdict, WeatherSnapshot


def snap(city="Madrid", ts=1_760_000_000, **kw):
    loc = Location(city, "XX", 40.0, -3.0)
    return WeatherSnapshot.from_api(owm_payload(dt=ts, **kw), loc)


def outdoor(wind_sensitive=False, setting="outdoor"):
    return Attraction("Madrid", "Spot", "Park", setting, wind_sensitive, 40.0, -3.0)


# ---- frames and derived metrics ----
def test_snapshots_to_frame_has_derived_columns():
    df = t.snapshots_to_frame([snap(temp=10, feels=6, rain=1.0)])
    row = df.iloc[0]
    assert row["feels_like_delta"] == -4 and row["precip_mm"] == 1.0
    assert row["comfort_index"] == pytest.approx(100 - 3 * 15 - 5 * 1)


# ---- comparison ----
def make_frame():
    return t.snapshots_to_frame([
        snap("A", temp=30, feels=32), snap("B", temp=5, feels=2), snap("C", temp=18, feels=18, wind=14),
    ])


def test_rank_and_extremes():
    df = make_frame()
    assert list(t.rank_cities(df, "temp")["city"]) == ["A", "C", "B"]
    assert list(t.rank_cities(df, "temp", n=1, ascending=True)["city"]) == ["B"]
    ex = t.extremes(df)
    assert ex["hottest"]["city"] == "A" and ex["coldest"]["city"] == "B" and ex["windiest"]["city"] == "C"
    assert t.extremes(df.iloc[0:0]) == {}


# ---- trend / daily summary ----
def history():
    day = 86_400
    base = 1_760_000_000
    snaps = [snap("A", ts=base, temp=10), snap("A", ts=base + 3600, temp=16), snap("A", ts=base + day, temp=20),
             snap("B", ts=base, temp=1)]
    return t.snapshots_to_frame(snaps)


def test_metric_timeseries_covers_all_cities_sorted():
    out = t.metric_timeseries(history(), "temp")
    assert list(out.columns) == ["city", "timestamp", "value"]
    assert list(out["city"]) == ["A", "A", "A", "B"]
    assert list(out[out["city"] == "A"]["value"]) == [10, 16, 20]


def test_metric_timeseries_resample_means_per_city():
    out = t.metric_timeseries(history(), "temp", freq="1D")
    a = out[out["city"] == "A"]
    assert list(a["value"]) == [13, 20] and list(out[out["city"] == "B"]["value"]) == [1]


def test_daily_summary_all_cities_and_one_city():
    out = t.daily_summary(history())
    first = out[(out["city"] == "A")].iloc[0]
    assert (first["temp_min"], first["temp_max"], first["temp_range"], first["observations"]) == (10, 16, 6, 2)
    assert len(out) == 3 and set(out["city"]) == {"A", "B"}
    assert len(t.daily_summary(history(), "A")) == 2
    assert t.daily_summary(history(), "Nowhere").empty


def test_latest_per_city_and_coverage():
    h = history()
    latest = t.latest_per_city(h).set_index("city")
    assert latest.loc["A", "temp"] == 20 and len(latest) == 2
    cov = t.history_coverage(h)
    assert cov["rows"] == 4 and cov["cities"] == 2


# ---- geography ----
def test_haversine_madrid_london():
    assert t.haversine_km(40.4168, -3.7038, 51.5072, -0.1276) == pytest.approx(1264, abs=5)


def test_build_grid_shape_and_extent():
    grid = t.build_grid(Location("X", "XX", 0.0, 0.0), 3, 10)
    assert len(grid) == 9
    lats = sorted({g.lat for g in grid})
    assert lats[0] == pytest.approx(-10 / 111.32) and lats[1] == pytest.approx(0) and lats[-1] == pytest.approx(10 / 111.32)
    assert len(t.build_grid(MADRID, 1, 5)) == 1


def test_nearest_snapshot():
    near = WeatherSnapshot.from_api(owm_payload(), Location("n", "XX", 40.01, -3.0))
    far = WeatherSnapshot.from_api(owm_payload(), Location("f", "XX", 41.0, -3.0))
    assert t.nearest_snapshot([far, near], 40.0, -3.0) is near


# ---- attraction rules ----
@pytest.mark.parametrize("kwargs, setting, wind_sens, expected", [
    ({}, "outdoor", False, Verdict.GOOD),
    ({"cond_id": 501, "main": "Rain", "rain": 1.2}, "outdoor", False, Verdict.CAUTION),
    ({"cond_id": 502, "main": "Rain", "rain": 9.0}, "outdoor", False, Verdict.AVOID),
    ({"cond_id": 211, "main": "Thunderstorm"}, "outdoor", False, Verdict.AVOID),
    ({"wind": 12}, "outdoor", False, Verdict.CAUTION),
    ({"wind": 18}, "outdoor", False, Verdict.AVOID),
    ({"wind": 14}, "outdoor", True, Verdict.AVOID),     # cable car / boat suspended
    ({"wind": 14}, "outdoor", False, Verdict.CAUTION),  # same wind is only unpleasant elsewhere
    ({"wind": 9}, "outdoor", True, Verdict.CAUTION),
    ({"temp": 41, "feels": 43}, "outdoor", False, Verdict.AVOID),
    ({"temp": 34, "feels": 35}, "outdoor", False, Verdict.CAUTION),
    ({"temp": -3, "feels": -6}, "outdoor", False, Verdict.CAUTION),
    ({"temp": -18, "feels": -22}, "outdoor", False, Verdict.AVOID),
    ({"visibility": 400}, "outdoor", False, Verdict.CAUTION),
    ({"cond_id": 211, "main": "Thunderstorm"}, "indoor", False, Verdict.GOOD),
    ({"cond_id": 502, "main": "Rain", "rain": 9.0}, "indoor", False, Verdict.GOOD),
    ({"wind": 15}, "indoor", True, Verdict.CAUTION),   # observation deck may close
    ({"cond_id": 501, "main": "Rain", "rain": 1.2}, "mixed", False, Verdict.GOOD),
    ({"cond_id": 211, "main": "Thunderstorm"}, "mixed", False, Verdict.CAUTION),
])
def test_attraction_verdicts(kwargs, setting, wind_sens, expected):
    a = outdoor(wind_sens, setting)
    result = t.assess_attraction(a, snap(**kwargs))
    assert result.verdict is expected
    assert result.reasons  # always explains itself


def test_assess_city_uses_nearest_point_weather():
    calm = WeatherSnapshot.from_api(owm_payload(wind=1), Location("calm", "XX", 40.0, -3.0))
    gale = WeatherSnapshot.from_api(owm_payload(wind=20), Location("gale", "XX", 41.0, -3.0))
    at_calm = Attraction("Madrid", "A", "Park", "outdoor", False, 40.0, -3.0)
    at_gale = Attraction("Madrid", "B", "Park", "outdoor", False, 41.0, -3.0)
    verdicts = {a.attraction.name: a.verdict for a in t.assess_city([at_calm, at_gale], [calm, gale])}
    assert verdicts == {"A": Verdict.GOOD, "B": Verdict.AVOID}


def test_assessments_frame_sorted_best_first():
    snaps = [snap(wind=20)]
    a_bad = outdoor(); a_ok = Attraction("Madrid", "Museum", "Museum", "indoor", False, 40.0, -3.0)
    df = t.assessments_to_frame([t.assess_attraction(a_bad, snaps[0]), t.assess_attraction(a_ok, snaps[0])])
    assert list(df["Attraction"]) == ["Museum", "Spot"]


# ---- forecast outlook ----
def forecast_slots():
    # tz offset 0; day 1 = clear all day, day 2 = thunderstorms all day
    base = int(datetime(2026, 11, 2, 0, 0, tzinfo=timezone.utc).timestamp())
    slots = []
    for day, kw in enumerate([{}, {"cond_id": 211, "main": "Thunderstorm"}]):
        for hour in range(0, 24, 3):
            slots.append(snap(ts=base + day * 86400 + hour * 3600, tz=0, **kw))
    return slots


def test_visit_outlook_and_best_day():
    out = t.visit_outlook(outdoor(), forecast_slots())
    assert list(out["date"]) == [date(2026, 11, 2), date(2026, 11, 3)]
    assert list(out["verdict"]) == ["good", "avoid"]
    assert t.best_day(out) == date(2026, 11, 2)
    assert t.best_day(pd.DataFrame()) is None
    assert t.visit_outlook(outdoor(), []).empty


# ---- user-editable assumptions ----
def verdict(setting="outdoor", wind_sens=False, assumptions=None, **kw):
    return t.assess_attraction(outdoor(wind_sens, setting), snap(**kw), assumptions).verdict


def test_wider_temperature_range_makes_heat_acceptable():
    hot = {"temp": 34, "feels": 35}
    assert verdict(**hot) is Verdict.CAUTION
    assert verdict(assumptions=Assumptions(temp_max=36), **hot) is Verdict.GOOD
    assert verdict(assumptions=Assumptions(temp_max=20), temp=25, feels=25) is Verdict.CAUTION


def test_narrower_range_flags_cold_and_dangerous_limits_move_with_it():
    assert verdict(temp=8, feels=7) is Verdict.GOOD
    assert verdict(assumptions=Assumptions(temp_min=10), temp=8, feels=7) is Verdict.CAUTION
    a = Assumptions(temp_min=10, temp_max=25)
    assert (a.cold_limit, a.heat_limit) == (-5, 32)
    assert verdict(assumptions=a, temp=33, feels=33) is Verdict.AVOID


def test_wind_limit_scales_all_wind_thresholds():
    assert verdict(wind=12) is Verdict.CAUTION
    assert verdict(assumptions=Assumptions(max_wind=14), wind=12) is Verdict.GOOD
    strict = Assumptions(max_wind=5.4)  # half the default: every wind threshold halves
    assert strict.gale_wind == pytest.approx(8.6) and strict.sensitive_close_wind == pytest.approx(6.95)
    assert verdict(assumptions=strict, wind=9) is Verdict.AVOID


def test_rain_tolerance():
    light = {"cond_id": 500, "main": "Rain", "rain": 0.8}
    assert verdict(**light) is Verdict.CAUTION
    assert verdict(assumptions=Assumptions(max_rain=1.0), **light) is Verdict.GOOD
    # heavy rain is never acceptable: 7.6 mm/h, or twice the user's limit if that is higher
    assert verdict(assumptions=Assumptions(max_rain=1.0), cond_id=502, main="Rain", rain=8.0) is Verdict.AVOID
    assert Assumptions(max_rain=6.0).heavy_rain == 12.0
    # a "rain" condition with no reported amount still counts as light rain
    assert verdict(cond_id=500, main="Rain") is Verdict.CAUTION


def test_assumptions_validation_and_ranges():
    with pytest.raises(ValueError):
        Assumptions(temp_min=30, temp_max=10)
    with pytest.raises(ValueError):
        Assumptions(max_wind=0)
    with pytest.raises(ValueError):
        Assumptions(max_rain=-1)
    a = Assumptions(temp_min=8, temp_max=28, max_wind=9, max_rain=0.5)
    assert a.acceptable_range("temp") == a.acceptable_range("feels_like") == (8, 28)
    assert a.acceptable_range("wind_speed") == (0, 9) and a.acceptable_range("precip_mm") == (0, 0.5)
    assert a.acceptable_range("humidity") is None


def test_assumptions_flow_through_city_and_outlook():
    calm_hot = WeatherSnapshot.from_api(owm_payload(temp=34, feels=35), Location("c", "XX", 40.0, -3.0))
    att = outdoor()
    assert t.assess_city([att], [calm_hot])[0].verdict is Verdict.CAUTION
    assert t.assess_city([att], [calm_hot], Assumptions(temp_max=40))[0].verdict is Verdict.GOOD
    base = int(datetime(2026, 11, 2, 0, 0, tzinfo=timezone.utc).timestamp())
    slots = [snap(ts=base + h * 3600, tz=0, temp=34, feels=35) for h in range(0, 24, 3)]
    assert list(t.visit_outlook(att, slots)["verdict"]) == ["caution"]
    assert list(t.visit_outlook(att, slots, Assumptions(temp_max=40))["verdict"]) == ["good"]



# ---- smooth world field ----
def field_frame():
    return pd.DataFrame({"city": ["Madrid", "Sydney"], "lat": [40.4, -33.9], "lon": [-3.7, 151.2], "temp": [10.0, 30.0]})


def cell_at(field, lat, lon):
    row = int(abs(field["lats"] - lat).argmin())
    col = int(abs(field["lons"] - lon).argmin())
    return row, col


def test_interpolate_field_shape_bounds_and_geometry():
    f = t.interpolate_field(field_frame(), "temp", lon_step=2.0)
    assert f["values"].shape == f["alpha"].shape == (len(f["lats"]), len(f["lons"])) == (116, 180)
    assert f["lats"][0] > 70 and f["lats"][-1] < -70 and (f["lats"][:-1] > f["lats"][1:]).all()  # north -> south
    assert f["values"].min() >= 10 - 1e-9 and f["values"].max() <= 30 + 1e-9  # never invents extremes
    # rows are evenly spaced in Mercator y, which is what a web map expects of an image layer
    y = np.log(np.tan(np.pi / 4 + np.radians(f["lats"]) / 2))
    assert np.allclose(np.diff(y), np.diff(y)[0])


def test_interpolate_field_follows_the_nearest_city():
    f = t.interpolate_field(field_frame(), "temp", lon_step=2.0)
    r, c = cell_at(f, 40.4, -3.7)
    assert f["values"][r, c] == pytest.approx(10, abs=0.1)
    r, c = cell_at(f, -33.9, 151.2)
    assert f["values"][r, c] == pytest.approx(30, abs=0.1)
    r, c = cell_at(f, 0.0, 70.0)  # between the two: somewhere in the middle
    assert 10 < f["values"][r, c] < 30


def test_interpolate_field_fades_away_from_cities():
    f = t.interpolate_field(field_frame(), "temp", lon_step=2.0)
    near = f["alpha"][cell_at(f, 40.4, -3.7)]
    far = f["alpha"][cell_at(f, 0.0, -120.0)]  # mid Pacific, thousands of km from both
    assert near == 1.0 and far == 0.0


# ---- smooth close-up field (built from the real readings, no extra API calls) ----
def lattice_frame(centre, n, radius, fn, drop=()):
    nodes = t.build_grid(centre, n, radius)
    rows = [{"lat": nd.lat, "lon": nd.lon, "v": fn(nd)} for k, nd in enumerate(nodes) if k not in drop]
    return pd.DataFrame(rows)


def test_smooth_field_reproduces_a_planar_surface_exactly():
    c = Location("X", "XX", 40.0, -3.0)
    df = lattice_frame(c, 4, 20, lambda nd: nd.lat)  # value = latitude: must come back as latitude
    f = t.smooth_closeup_field(df, c, 4, 20, "v", out_size=50)
    v = f["values"]
    assert v.shape == (50, 50)
    assert v[0, 0] == pytest.approx(f["north"]) and v[-1, 0] == pytest.approx(f["south"])
    assert (np.diff(v[:, 0]) < 0).all()  # row 0 is north, values fall towards the south
    assert np.allclose(v[:, 0], v[:, -1])  # no variation along longitude
    assert f["north"] > c.lat > f["south"] and f["east"] > c.lon > f["west"]


def test_smooth_field_is_smoother_than_the_readings_and_never_overshoots():
    c = Location("X", "XX", 40.0, -3.0)
    df = lattice_frame(c, 3, 10, lambda nd: 30.0 if (round((nd.lat - 40) * 100), round((nd.lon + 3) * 100)) == (0, 0) else 10.0)
    f = t.smooth_closeup_field(df, c, 3, 10, "v", out_size=60)
    v = f["values"]
    assert v.min() >= 10 - 1e-9 and v.max() <= 30 + 1e-9
    assert v[30, 30] == pytest.approx(30, abs=0.8) and v[0, 0] == pytest.approx(10)
    assert 0 < np.abs(np.diff(v, axis=1)).max() < 3  # a gentle gradient, not a step from 10 to 30


def test_smooth_field_fills_failed_readings_from_neighbours():
    c = Location("X", "XX", 40.0, -3.0)
    df = lattice_frame(c, 3, 10, lambda nd: 20.0, drop=(4,))  # centre request failed
    f = t.smooth_closeup_field(df, c, 3, 10, "v", out_size=20)
    assert np.isfinite(f["values"]).all() and np.allclose(f["values"], 20)
    assert t.smooth_closeup_field(df.iloc[0:0], c, 3, 10, "v") is None


def test_smooth_field_single_point_grid():
    c = Location("X", "XX", 40.0, -3.0)
    df = lattice_frame(c, 1, 5, lambda nd: 7.0)
    f = t.smooth_closeup_field(df, c, 1, 5, "v", out_size=8)
    assert (f["values"] == 7).all() and f["north"] > f["south"]


def test_colour_scales_are_fixed_by_default():
    assert t.value_range("temp", [-5.0, 12.5]) == (0.0, 38.0)  # same scale whatever the data
    assert t.value_range("temp", [30.0, 40.0]) == t.value_range("feels_like", [1.0]) == (0.0, 38.0)
    assert t.value_range("humidity", [50.0]) == t.value_range("clouds", [1.0]) == (0.0, 100.0)
    assert t.value_range("wind_speed", [1.0, 2.0]) == (0.0, 20.0)
    assert t.value_range("precip_mm", [0, 0, 0]) == (0.0, 5.0)
    assert t.value_range("comfort_index", [40.0]) == (0.0, 100.0)
    assert all(lo == 0.0 for lo, _ in t.COLOUR_RANGES.values())  # nothing negative: no negative rain


def test_fitted_colour_scales_follow_the_data_but_rain_and_wind_start_at_zero():
    assert t.value_range("temp", [-5.0, 12.5], fit=True) == (-5.0, 12.5)
    assert t.value_range("precip_mm", [0, 0, 0], fit=True) == (0.0, 1.0)
    assert t.value_range("precip_mm", [0.2, 3.4], fit=True) == (0.0, 3.4)
    assert t.value_range("wind_speed", [1.0, 2.0], fit=True) == (0.0, 5.0)
