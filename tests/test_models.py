from datetime import datetime, timezone

import pytest

from conftest import MADRID, owm_payload
from weather_insights.models import (
    Attraction, POICandidate, Verdict, WeatherSnapshot, comfort_score, condition_group,
)


def snap(**kw):
    return WeatherSnapshot.from_api(owm_payload(**kw), MADRID)


def test_from_api_parses_fields():
    s = snap(temp=22.5, feels=23.0, humidity=40, wind=3.5, rain=0.4)
    assert (s.temp, s.feels_like, s.humidity, s.wind_speed, s.rain_1h) == (22.5, 23.0, 40, 3.5, 0.4)
    assert s.timestamp.tzinfo is not None and s.city == "Madrid"


def test_forecast_3h_rain_converted_to_hourly():
    payload = owm_payload()
    payload["rain"] = {"3h": 6.0}
    payload["pop"] = 0.8
    s = WeatherSnapshot.from_api(payload, MADRID)
    assert s.rain_1h == pytest.approx(2.0) and s.pop == 0.8


def test_ideal_conditions_score_100():
    assert comfort_score(21, 50, 2, 0) == 100


def test_comfort_penalties_known_values():
    # feels 31 -> -30 ; humidity 80 -> -6 ; wind 9 -> -6 ; rain 1 mm -> -5  => 100-47 = 53
    assert comfort_score(31, 80, 9, 1) == pytest.approx(53)
    assert comfort_score(-40, 100, 30, 10) == 0  # clipped at 0


def test_properties_are_derived():
    s = snap(temp=10, feels=6, rain=1.0, snow=0.5)
    assert s.feels_like_delta == -4
    assert s.precip_mm == 1.5
    assert s.comfort_index == pytest.approx(float(comfort_score(6, 50, 2.0, 1.5)))


def test_sorted_by_temperature_and_str_repr():
    a, b, c = snap(temp=30), snap(temp=-2, feels=-4), snap(temp=12)
    assert [s.temp for s in sorted([a, b, c])] == [-2, 12, 30]
    assert max([a, b, c]).temp == 30
    assert "Madrid" in str(a) and repr(a).startswith("WeatherSnapshot('Madrid'")


def test_local_time_applies_offset():
    s = snap(tz=7200)
    assert (s.local_time - s.timestamp).total_seconds() == 7200


@pytest.mark.parametrize("cid, group", [(211, "Thunderstorm"), (301, "Drizzle"), (501, "Rain"),
                                         (601, "Snow"), (741, "Atmosphere"), (800, "Clear"), (803, "Clouds")])
def test_condition_groups(cid, group):
    assert condition_group(cid) == group


def test_verdict_labels_and_attraction_str():
    assert Verdict.AVOID.severity > Verdict.CAUTION.severity > Verdict.GOOD.severity
    assert "outdoor" in str(Attraction("Madrid", "Retiro", "Park", "outdoor", False, 0, 0))


def test_attraction_maps_url_searches_for_its_name_and_city_at_its_coordinates():
    retiro = Attraction("Madrid", "Retiro Park", "Park", "outdoor", False, 40.4153, -3.6844)
    assert retiro.maps_url == ("https://www.google.com/maps/search/Retiro+Park%2C+Madrid"
                               "/@40.415300,-3.684400,17z")
    # names with special characters (including "/") are encoded, so they cannot break the link
    odd = Attraction("New York", "9/11 Musée d'Orsay & Café", "Museum", "indoor", False, 40.71, -74.01)
    assert "/search/9%2F11+Mus%C3%A9e+d%27Orsay+%26+Caf%C3%A9%2C+New+York/@" in odd.maps_url


# tests for the new POI candidate class and its methods
def candidate(osm_type="museum", km=1.4):
    return POICandidate("Templo de Debod, Madrid", 40.4241, -3.7176, "tourism", osm_type, km)


def test_poi_candidate_suggests_a_readable_category():
    assert candidate("museum").suggested_category == "Museum"
    assert candidate("theme_park").suggested_category == "Theme Park"
    assert candidate("").suggested_category == "Other"


def test_poi_candidate_str_shows_distance_from_centre():
    assert str(candidate(km=1.4)) == "Templo de Debod, Madrid (1.4 km from the city centre)"
    assert "2 km" in str(candidate(km=2.0))


# ---- opening hours (issue #4) ----
# The week of 16-22 Nov 2026, Monday to Sunday. UTC-tagged like WeatherSnapshot.local_time.
DAY = {"Mon": 16, "Tue": 17, "Wed": 18, "Thu": 19, "Fri": 20, "Sat": 21, "Sun": 22}


def at(day, hh, mm=0):
    return datetime(2026, 11, DAY[day], hh, mm, tzinfo=timezone.utc)


def hours(raw):
    from weather_insights.models import OpeningHours
    return OpeningHours(raw)


def test_the_test_week_starts_on_a_monday():
    assert at("Wed", 12).weekday() == 2  # 18 Nov 2026 is a Wednesday
    assert [at(d, 12).weekday() for d in DAY] == list(range(7))


@pytest.mark.parametrize("when, expected", [
    (at("Wed", 10), True),
    (at("Wed", 18), False),
    (at("Sat", 10), False),
    (at("Fri", 17), False),  # the end is exclusive: closed at 17:00
    (at("Mon", 9), True),  # the start is inclusive
])
def test_weekday_office_hours(when, expected):
    assert hours("Mo-Fr 09:00-17:00").is_open(when) is expected


def test_24_7_is_always_open():
    h = hours("24/7")
    assert all(h.is_open(at(d, hh)) is True for d in DAY for hh in (0, 12, 23))


def test_a_closed_day():
    h = hours("Tu-Su 10:00-20:00; Mo off")
    assert h.is_open(at("Mon", 12)) is False
    assert h.is_open(at("Tue", 12)) is True


def test_a_lunch_break():
    h = hours("Mo-Sa 10:00-14:00,16:00-20:00")
    assert h.is_open(at("Mon", 15)) is False
    assert h.is_open(at("Mon", 16, 30)) is True


def test_a_later_rule_replaces_an_earlier_one_for_its_days():
    h = hours("Mo-Su 09:00-18:00; Su 10:00-14:00")
    assert h.is_open(at("Sun", 16)) is False
    assert h.is_open(at("Sat", 16)) is True


def test_overnight_hours_run_into_the_next_day():
    h = hours("Fr-Sa 22:00-03:00")
    assert h.is_open(at("Fri", 23)) is True
    assert h.is_open(at("Sat", 1)) is True  # Friday night's range
    assert h.is_open(at("Sun", 2)) is True  # Saturday night's range
    assert h.is_open(at("Fri", 4)) is False  # Thursday has no hours


def test_day_ranges_can_wrap_around_the_week():
    h = hours("Fr-Mo 10:00-12:00")  # Fr, Sa, Su, Mo
    assert h.is_open(at("Sun", 11)) is True
    assert h.is_open(at("Wed", 11)) is False


@pytest.mark.parametrize("raw", [
    "Mo-Fr 09:00-17:00; PH off",
    "sunrise-sunset",
    "Apr-Oct: Mo-Su 10:00-18:00",
    '"by appointment"',
    "Mo-Fr 25:00-26:00",
    "Mo-Fr 09:60-10:00",
    "Xx 10:00-12:00",
])
def test_hours_we_cannot_evaluate_show_the_text_and_never_raise(raw):
    h = hours(raw)
    assert h.is_understood is False
    assert h.is_open(at("Wed", 12)) is None
    assert h.heading(at("Wed", 12)) == "🕘 Opening hours (check before going)"


def test_blank_hours_mean_nothing_to_show():
    h = hours("")
    assert bool(h) is False and bool(hours("24/7")) is True
    assert h.heading(at("Wed", 12)) is None and h.is_open(at("Wed", 12)) is None
    assert str(h) == "Hours not listed" and str(hours("24/7")) == "24/7"


def test_attraction_hours_are_optional():
    old_style = Attraction("Madrid", "Retiro", "Park", "outdoor", False, 40.4, -3.7)  # 7 positional args
    assert old_style.opening_hours == "" and not old_style.hours
    always = Attraction("Madrid", "Prado", "Museum", "indoor", False, 40.4, -3.7, opening_hours="24/7")
    assert always.hours.is_open(at("Sun", 3)) is True


# ---- opening hours as a table on the card ----
WEEK = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def test_simple_hours_become_one_row_per_weekday():
    rows = hours("Mo-Sa 10:00-20:00; Su 10:00-19:00").table()
    assert rows == [(day, "10:00–20:00") for day in WEEK[:6]] + [("Sun", "10:00–19:00")]


def test_the_weekday_table_applies_overrides_breaks_overnight_and_closed_days():
    rows = dict(hours("Mo-Fr 09:00-13:00,15:00-19:00; Fr-Sa 22:00-03:00; Su off").table())
    assert list(rows) == list(WEEK)
    assert rows["Mon"] == "09:00–13:00, 15:00–19:00"  # lunch break
    assert rows["Fri"] == "22:00–03:00"  # the later rule replaces Friday's daytime hours
    assert rows["Sat"] == "22:00–03:00"  # shown as written, not split at midnight
    assert rows["Sun"] == "Closed"


def test_24_7_reads_as_open_all_day():
    assert hours("24/7").table() == [(day, "Open 24 hours") for day in WEEK]


def test_complex_hours_become_one_row_per_rule():
    rows = hours("Mo-Su 10:30-17:30; Sa 10:30-19:00; Dec 25 off; Nov Th[4] off").table()  # MoMA, from OSM
    assert rows == [("Mon–Sun", "10:30–17:30"), ("Sat", "10:30–19:00"), ("Dec 25", "Closed"),
                    ("Nov Thu[4]", "Closed")]


def test_rule_rows_tidy_months_dates_and_holidays():
    rows = hours("Oct-Mar: 10:00-18:00; Jul 25,Oct 12: 10:00-15:00; 09:00-12:00; PH off; sunrise-sunset").table()
    assert rows == [("Oct–Mar", "10:00–18:00"), ("Jul 25, Oct 12", "10:00–15:00"), ("Every day", "09:00–12:00"),
                    ("Public holidays", "Closed"), ("", "sunrise-sunset")]  # unknown pieces shown as written


def test_no_hours_no_table():
    assert hours("").table() == []
    assert hours("").heading(at("Wed", 12)) is None


@pytest.mark.parametrize("raw, when, expected", [
    ("Mo-Fr 09:00-17:00", at("Wed", 10), "🕘 Open now · Opening hours"),
    ("Mo-Fr 09:00-17:00", at("Sat", 10), "🔒 Closed now · Opening hours"),
    ("Mo-Su 10:30-17:30; Dec 25 off", at("Wed", 12), "🕘 Opening hours (check before going)"),
])
def test_the_heading_says_open_closed_or_check(raw, when, expected):
    assert hours(raw).heading(when) == expected
