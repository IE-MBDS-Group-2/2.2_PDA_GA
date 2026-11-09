import pytest

from conftest import MADRID, owm_payload
from weather_insights.models import Attraction, Verdict, WeatherSnapshot, comfort_score, condition_group


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
