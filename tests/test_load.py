from pathlib import Path

import pandas as pd

from conftest import owm_payload
from weather_insights import load
from weather_insights.models import LOG_COLUMNS, Attraction, Location, WeatherSnapshot

NO_USER_FILE = Path("this-file-does-not-exist.csv")  # stands in for "no user attractions yet"


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


def shipped_attractions():
    """Only the curated file: attractions a user adds in the app must not affect these checks."""
    return load.load_attractions(user_csv=NO_USER_FILE)


def shipped_cities():
    """Only the curated file: cities a user adds in the app must not affect these checks."""
    return load.load_cities(user_csv=NO_USER_FILE)


def test_shipped_catalogues_are_consistent():
    cities = {c.city: c for c in shipped_cities()}
    attractions = shipped_attractions()
    assert {a.city for a in attractions} <= set(cities)
    assert {a.setting for a in attractions} <= {"outdoor", "indoor", "mixed"}
    assert any(a.wind_sensitive for a in attractions)
    for a in attractions:
        assert -90 <= a.lat <= 90 and -180 <= a.lon <= 180


def test_default_capitals_present_and_every_city_has_up_to_ten_attractions():
    cities = {c.city: c for c in shipped_cities()}
    capitals = {"Beirut": "LB", "Washington D.C.": "US", "Quito": "EC", "Abu Dhabi": "AE",
                "Warsaw": "PL", "Manila": "PH", "Madrid": "ES"}
    for name, country in capitals.items():
        assert cities[name].country == country
    counts = {}
    for a in shipped_attractions():
        counts[a.city] = counts.get(a.city, 0) + 1
    assert set(counts) == set(cities)  # no city without attractions
    assert max(counts.values()) <= 10 and min(counts.values()) >= 8


def test_each_attraction_is_near_its_city():
    from weather_insights.transform import haversine_km
    cities = {c.city: c for c in shipped_cities()}
    far = [(a.name, round(haversine_km(a.lat, a.lon, cities[a.city].lat, cities[a.city].lon)))
           for a in shipped_attractions()
           if haversine_km(a.lat, a.lon, cities[a.city].lat, cities[a.city].lon) > 45]
    assert far == []  # catches typos in coordinates (the close-up covers at most 40 km)


# ---- attractions added by users ----
CURATED_HEADER = "city,name,category,setting,wind_sensitive,lat,lon\n"
USER_HEADER = "city,name,category,setting,wind_sensitive,lat,lon,opening_hours\n"  # what the app writes


def debod(**kw):
    fields = dict(city="Madrid", name="Templo de Debod", category="Temple", setting="outdoor",
                  wind_sensitive=False, lat=40.4241, lon=-3.7176)
    return Attraction(**{**fields, **kw})


def curated_file(tmp_path):
    path = tmp_path / "curated.csv"
    path.write_text(CURATED_HEADER + "Madrid,Retiro Park,Park,outdoor,false,40.4153,-3.6845\n")
    return path


def test_user_attractions_follow_the_curated_ones(tmp_path):
    curated, user = curated_file(tmp_path), tmp_path / "user.csv"
    assert load.save_user_attraction(debod(), user, curated) is True
    assert [a.name for a in load.load_attractions(curated, user)] == ["Retiro Park", "Templo de Debod"]


def test_saved_attraction_round_trips_with_every_field(tmp_path):
    curated, user = curated_file(tmp_path), tmp_path / "data" / "user.csv"  # folder is created
    original = debod(name='Plaza "Mayor", Old Town', wind_sensitive=True)  # comma and quotes
    load.save_user_attraction(original, user, curated)
    assert user.read_text(encoding="utf-8").startswith(USER_HEADER)
    assert load.load_attractions(NO_USER_FILE, user) == [original]


def test_duplicate_attractions_are_refused_ignoring_case(tmp_path):
    curated, user = curated_file(tmp_path), tmp_path / "user.csv"
    assert load.save_user_attraction(debod(), user, curated) is True
    assert load.save_user_attraction(debod(name="TEMPLO DE DEBOD"), user, curated) is False
    assert load.save_user_attraction(debod(name="Retiro Park"), user, curated) is False  # curated one
    assert len(load.load_attractions(curated, user)) == 2


def test_same_name_in_another_city_is_not_a_duplicate(tmp_path):
    curated, user = curated_file(tmp_path), tmp_path / "user.csv"
    assert load.save_user_attraction(debod(city="Lisbon", name="Retiro Park"), user, curated) is True


def test_values_pandas_would_read_as_missing_survive_a_round_trip(tmp_path):
    curated, user = curated_file(tmp_path), tmp_path / "user.csv"
    for name in ["None", "NA", "null", "Nan"]:
        assert load.save_user_attraction(debod(name=name, category=""), user, curated) is True
    loaded = load.load_attractions(NO_USER_FILE, user)
    assert [a.name for a in loaded] == ["None", "NA", "null", "Nan"]
    assert all(a.category == "" for a in loaded)  # not NaN


def test_an_empty_user_file_is_treated_as_no_attractions_and_gets_a_header(tmp_path):
    curated, user = curated_file(tmp_path), tmp_path / "user.csv"
    user.write_text("")  # e.g. left behind by a crash or a manual edit
    assert [a.name for a in load.load_attractions(curated, user)] == ["Retiro Park"]
    assert load.save_user_attraction(debod(), user, curated) is True
    assert user.read_text(encoding="utf-8").startswith(USER_HEADER)
    assert [a.name for a in load.load_attractions(curated, user)] == ["Retiro Park", "Templo de Debod"]


def test_simultaneous_saves_of_the_same_attraction_add_it_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    curated, user = curated_file(tmp_path), tmp_path / "user.csv"
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: load.save_user_attraction(debod(), user, curated), range(8)))
    assert results.count(True) == 1
    assert len(load.load_attractions(NO_USER_FILE, user)) == 1


def test_missing_files_mean_no_attractions():
    assert load.load_attractions(NO_USER_FILE, NO_USER_FILE) == []



def test_load_user_attractions_returns_only_the_user_added_ones(tmp_path):
    curated, user = curated_file(tmp_path), tmp_path / "user.csv"
    load.save_user_attraction(debod(), user, curated)
    assert load.load_user_attractions(user) == [debod()]  # Retiro Park (curated) is not in it


def test_load_user_attractions_is_empty_without_a_user_file(tmp_path):
    assert load.load_user_attractions(tmp_path / "missing.csv") == []


# ---- deleting user-added attractions ----
def test_delete_removes_only_that_attraction_and_keeps_the_others_intact(tmp_path):
    curated, user = curated_file(tmp_path), tmp_path / "user.csv"
    tricky = debod(name='Plaza "Mayor", Old Town', wind_sensitive=True)  # comma and quotes
    others = [debod(), tricky, debod(city="Lisbon", name="Templo de Debod")]
    for a in others:
        load.save_user_attraction(a, user, curated)
    assert load.delete_user_attraction("Madrid", "Templo de Debod", user) is True
    assert load.load_user_attractions(user) == [tricky, debod(city="Lisbon", name="Templo de Debod")]
    assert user.read_text(encoding="utf-8").startswith(USER_HEADER)


def test_delete_matches_city_and_name_ignoring_case(tmp_path):
    curated, user = curated_file(tmp_path), tmp_path / "user.csv"
    load.save_user_attraction(debod(), user, curated)
    assert load.delete_user_attraction("Madrid", "TEMPLO de debod", user) is True
    assert load.load_user_attractions(user) == []


def test_delete_needs_the_right_city(tmp_path):
    curated, user = curated_file(tmp_path), tmp_path / "user.csv"
    load.save_user_attraction(debod(), user, curated)
    assert load.delete_user_attraction("Lisbon", "Templo de Debod", user) is False
    assert load.load_user_attractions(user) == [debod()]


def test_delete_of_an_absent_attraction_returns_false_and_changes_nothing(tmp_path):
    curated, user = curated_file(tmp_path), tmp_path / "user.csv"
    load.save_user_attraction(debod(), user, curated)
    before = user.read_bytes()
    assert load.delete_user_attraction("Madrid", "Nowhere", user) is False
    assert user.read_bytes() == before


def test_delete_does_not_create_a_missing_file(tmp_path):
    user = tmp_path / "data" / "user.csv"
    assert load.delete_user_attraction("Madrid", "Templo de Debod", user) is False
    assert not user.exists()
    assert not user.parent.exists()


def test_delete_never_touches_curated_attractions(tmp_path):
    curated, user = curated_file(tmp_path), tmp_path / "user.csv"
    load.save_user_attraction(debod(), user, curated)
    curated_before = curated.read_bytes()
    assert load.delete_user_attraction("Madrid", "Retiro Park", user) is False  # curated, not user-added
    assert curated.read_bytes() == curated_before
    assert [a.name for a in load.load_attractions(curated, user)] == ["Retiro Park", "Templo de Debod"]


def test_after_deleting_the_last_attraction_the_file_is_reusable_without_a_second_header(tmp_path):
    curated, user = curated_file(tmp_path), tmp_path / "user.csv"
    load.save_user_attraction(debod(), user, curated)
    assert load.delete_user_attraction("Madrid", "Templo de Debod", user) is True
    assert load.load_user_attractions(user) == []
    assert load.save_user_attraction(debod(name="Palacio Real"), user, curated) is True
    assert user.read_text(encoding="utf-8").count(USER_HEADER) == 1
    assert load.load_user_attractions(user) == [debod(name="Palacio Real")]


def test_values_pandas_would_read_as_missing_survive_a_delete(tmp_path):
    curated, user = curated_file(tmp_path), tmp_path / "user.csv"
    for name in ["None", "NA", "Templo de Debod"]:
        load.save_user_attraction(debod(name=name, category=""), user, curated)
    assert load.delete_user_attraction("Madrid", "Templo de Debod", user) is True
    loaded = load.load_user_attractions(user)
    assert [a.name for a in loaded] == ["None", "NA"]
    assert all(a.category == "" for a in loaded)


def test_simultaneous_deletes_of_the_same_attraction_remove_it_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    curated, user = curated_file(tmp_path), tmp_path / "user.csv"
    load.save_user_attraction(debod(), user, curated)
    load.save_user_attraction(debod(name="Palacio Real"), user, curated)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: load.delete_user_attraction("Madrid", "Templo de Debod", user), range(8)))
    assert results.count(True) == 1
    assert load.load_user_attractions(user) == [debod(name="Palacio Real")]


# ---- opening hours (issue #4) ----
def test_a_file_without_the_hours_column_still_loads(tmp_path):
    (retiro,) = load.load_attractions(curated_file(tmp_path), NO_USER_FILE)  # the old 7-column format
    assert retiro.opening_hours == ""


def test_a_file_with_the_hours_column_loads_them(tmp_path):
    path = tmp_path / "curated.csv"
    path.write_text("city,name,category,setting,wind_sensitive,lat,lon,opening_hours\n"
                    'Madrid,Museo del Prado,Museum,indoor,false,40.4138,-3.6921,"Mo-Sa 10:00-20:00; Su 10:00-19:00"\n'
                    "Madrid,Retiro Park,Park,outdoor,false,40.4153,-3.6845,\n")
    prado, retiro = load.load_attractions(path, NO_USER_FILE)
    assert prado.opening_hours == "Mo-Sa 10:00-20:00; Su 10:00-19:00"
    assert retiro.opening_hours == ""  # blank, not NaN


def test_hours_with_a_comma_and_a_semicolon_round_trip(tmp_path):
    curated, user = curated_file(tmp_path), tmp_path / "user.csv"
    original = debod(opening_hours="Mo-Fr 09:00-13:00,15:00-19:00; Sa 10:00-14:00")
    assert load.save_user_attraction(original, user, curated) is True
    assert load.load_attractions(NO_USER_FILE, user) == [original]


def test_an_old_user_file_is_migrated_when_a_new_attraction_is_saved(tmp_path):
    curated, user = curated_file(tmp_path), tmp_path / "user.csv"
    user.write_text(CURATED_HEADER
                    + "Madrid,Templo de Debod,Temple,outdoor,false,40.4241,-3.7176\n"
                    + "Madrid,Casa de Campo,Park,outdoor,false,40.4242,-3.7559\n")
    sorolla = debod(name="Museo Sorolla", category="Museum", setting="indoor", lat=40.4356, lon=-3.6925,
                    opening_hours="Tu-Sa 09:30-20:00; Su 10:00-15:00")
    assert load.save_user_attraction(sorolla, user, curated) is True
    loaded = load.load_attractions(NO_USER_FILE, user)
    assert [(a.name, a.opening_hours) for a in loaded] == [
        ("Templo de Debod", ""), ("Casa de Campo", ""), ("Museo Sorolla", "Tu-Sa 09:30-20:00; Su 10:00-15:00"),
    ]
    header = user.read_text(encoding="utf-8").splitlines()[0]
    assert header.endswith(",opening_hours")


def test_deleting_one_attraction_keeps_the_hours_of_the_others(tmp_path):
    curated, user = curated_file(tmp_path), tmp_path / "user.csv"
    sorolla = debod(name="Museo Sorolla", opening_hours="Tu-Sa 09:30-20:00; Su 10:00-15:00")
    load.save_user_attraction(debod(), user, curated)
    load.save_user_attraction(sorolla, user, curated)
    assert load.delete_user_attraction("Madrid", "Templo de Debod", user) is True
    assert load.load_user_attractions(user) == [sorolla]
    assert user.read_text(encoding="utf-8").startswith(USER_HEADER)


def test_deleting_from_an_old_user_file_upgrades_its_header(tmp_path):
    user = tmp_path / "user.csv"
    user.write_text(CURATED_HEADER
                    + "Madrid,Templo de Debod,Temple,outdoor,false,40.4241,-3.7176\n"
                    + "Madrid,Casa de Campo,Park,outdoor,false,40.4242,-3.7559\n")
    assert load.delete_user_attraction("Madrid", "Templo de Debod", user) is True
    assert [(a.name, a.opening_hours) for a in load.load_user_attractions(user)] == [("Casa de Campo", "")]
    assert user.read_text(encoding="utf-8").startswith(USER_HEADER)


def test_the_shipped_catalogue_has_text_hours_on_every_row():
    attractions = shipped_attractions()
    assert attractions and all(isinstance(a.opening_hours, str) for a in attractions)
