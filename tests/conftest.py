"""Shared fakes: no test ever touches the network."""

import pytest
import requests

from weather_insights.models import Location

MADRID = Location("Madrid", "ES", 40.4168, -3.7038)


def owm_payload(temp=20.0, feels=19.0, humidity=50, wind=2.0, cond_id=800, main="Clear",
                desc="clear sky", rain=None, snow=None, dt=1_760_000_000, visibility=10000, tz=3600):
    p = {
        "coord": {"lat": 40.4168, "lon": -3.7038},
        "weather": [{"id": cond_id, "main": main, "description": desc}],
        "main": {"temp": temp, "feels_like": feels, "humidity": humidity, "pressure": 1015},
        "wind": {"speed": wind}, "clouds": {"all": 10}, "visibility": visibility,
        "dt": dt, "timezone": tz, "sys": {"country": "ES"}, "name": "Madrid",
    }
    if rain is not None:
        p["rain"] = {"1h": rain}
    if snow is not None:
        p["snow"] = {"1h": snow}
    return p


class FakeResponse:
    def __init__(self, status=200, payload=None, bad_json=False):
        self.status_code = status
        self._payload = payload
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Returns queued responses (or raises queued exceptions) and records requests."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def madrid():
    return MADRID
