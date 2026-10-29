import pytest
import requests

from conftest import FakeResponse, FakeSession, owm_payload, MADRID
from weather_insights.extract import (
    APIError, InvalidAPIKeyError, InvalidInputError, JSONAPIClient, LocationNotFoundError,
    NetworkError, OpenWeatherClient, RateLimiter, RateLimitError, ServerError, fetch_many,
    validate_city_name, validate_coordinates,
)
from weather_insights.models import Location

KEY = "SECRET123KEY"


def client(*responses):
    return OpenWeatherClient(KEY, session=FakeSession(*responses))


# ---- input validation ----
@pytest.mark.parametrize("bad", ["", "   ", None, "12345", "Mad<rid>", "x" * 101, "!!!"])
def test_invalid_city_names_rejected(bad):
    with pytest.raises(InvalidInputError):
        validate_city_name(bad)


def test_valid_city_names_cleaned():
    assert validate_city_name("  San   José ") == "San José"
    assert validate_city_name("Stratford-upon-Avon") == "Stratford-upon-Avon"
    assert validate_city_name("St. John's") == "St. John's"


def test_coordinates_validated():
    assert validate_coordinates("40.4", "-3.7") == (40.4, -3.7)
    with pytest.raises(InvalidInputError):
        validate_coordinates(95, 0)
    with pytest.raises(InvalidInputError):
        validate_coordinates("abc", 0)


def test_bad_input_never_reaches_the_network():
    c = client(FakeResponse(200, []))
    with pytest.raises(InvalidInputError):
        c.geocode("   ")
    assert c._session.calls == []


# ---- one distinct error per failure mode ----
@pytest.mark.parametrize("status, exc", [
    (401, InvalidAPIKeyError), (403, InvalidAPIKeyError), (404, LocationNotFoundError),
    (429, RateLimitError), (500, ServerError), (503, ServerError), (418, APIError),
])
def test_status_codes_map_to_specific_errors(status, exc):
    with pytest.raises(exc) as info:
        client(FakeResponse(status, {})).current(MADRID)
    assert type(info.value) is exc


def test_network_failures_are_wrapped():
    for raised in (requests.Timeout("t"), requests.ConnectionError("c"), requests.RequestException("r")):
        with pytest.raises(NetworkError):
            client(raised).current(MADRID)


def test_unreadable_json_is_a_server_error():
    with pytest.raises(ServerError):
        client(FakeResponse(200, bad_json=True)).current(MADRID)


def test_unexpected_payload_is_a_server_error():
    with pytest.raises(ServerError):
        client(FakeResponse(200, {"weather": []})).current(MADRID)


def test_geocode_empty_result_is_not_found():
    with pytest.raises(LocationNotFoundError):
        client(FakeResponse(200, [])).geocode("Atlantis")


def test_geocode_success():
    loc = client(FakeResponse(200, [{"name": "Seville", "country": "ES", "lat": 37.38, "lon": -5.98}])).geocode("seville")
    assert loc == Location("Seville", "ES", 37.38, -5.98)


# ---- the key must never leak ----
def test_key_is_sent_but_never_exposed():
    c = client(FakeResponse(200, owm_payload()))
    c.current(MADRID)
    assert c._session.calls[0][1]["appid"] == KEY  # sent to the API...
    public = {k: v for k, v in vars(c).items() if not k.startswith("_")}
    assert KEY not in repr(c) and KEY not in str(public)  # ...but not in repr or public attributes


def test_errors_do_not_contain_the_key():
    secret_url_error = requests.ConnectionError(f"https://api.openweathermap.org/x?appid={KEY}")
    for resp in (secret_url_error, FakeResponse(401, {}), FakeResponse(500, {})):
        with pytest.raises(APIError) as info:
            client(resp).current(MADRID)
        assert KEY not in str(info.value)
        assert info.value.__cause__ is None
        assert info.value.__context__ is None or info.value.__suppress_context__  # no chained URL


def test_empty_key_rejected():
    with pytest.raises(InvalidAPIKeyError):
        OpenWeatherClient("  ")


# ---- inheritance: base class is provider-independent ----
def test_base_client_is_reusable_for_another_provider():
    class OtherProvider(JSONAPIClient):
        def ping(self):
            return self._get("ping")

    other = OtherProvider("https://example.com/api/", {"token": "t"}, session=FakeSession(FakeResponse(200, {"ok": True})))
    assert other.ping() == {"ok": True}
    assert other._session.calls[0][0] == "https://example.com/api/ping"
    assert issubclass(OpenWeatherClient, JSONAPIClient)


# ---- batch: one bad location does not stop the rest ----
def test_fetch_many_skips_failures_and_keeps_order():
    locs = [Location(n, "XX", 10.0, 10.0 + i) for i, n in enumerate(["A", "B", "C"])]

    class PerLocationSession:
        def get(self, url, params=None, timeout=None):
            if params["lon"] == 11.0:
                return FakeResponse(404, {})
            return FakeResponse(200, owm_payload())

    result = fetch_many(OpenWeatherClient(KEY, session=PerLocationSession()), locs)
    assert [s.location.city for s in result.snapshots] == ["A", "C"]
    assert list(result.errors) == ["B, XX"] and result.has_errors and result.ok_count == 2


# ---- rate limiting: big batches slow down instead of hitting HTTP 429 ----
class FakeClock:
    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def test_limiter_lets_a_burst_through_then_waits_for_the_window():
    clock = FakeClock()
    limiter = RateLimiter(2, period=10, clock=clock, sleep=clock.sleep)
    limiter.acquire(); limiter.acquire()
    assert clock.sleeps == []  # two calls fit in the window
    limiter.acquire()
    assert clock.sleeps == [10]  # the third waits until the first one leaves the window
    assert clock.now == 10


def test_limiter_paces_a_long_batch():
    clock = FakeClock()
    limiter = RateLimiter(50, period=60, clock=clock, sleep=clock.sleep)
    for _ in range(100):
        limiter.acquire()
    assert 60 <= clock.now < 61  # 100 calls at 50/minute: the second fifty start one minute later


def test_limiter_rejects_nonsense():
    with pytest.raises(ValueError):
        RateLimiter(0)


def test_client_uses_the_limiter_and_it_is_shared_by_every_call():
    clock = FakeClock()
    limiter = RateLimiter(1, period=5, clock=clock, sleep=clock.sleep)
    c = OpenWeatherClient(KEY, session=FakeSession(FakeResponse(200, owm_payload())), limiter=limiter)
    c.current(MADRID); c.current(MADRID); c.current(MADRID)
    assert clock.now == 10  # calls 2 and 3 each waited for the 5 s window
    assert isinstance(OpenWeatherClient(KEY)._limiter, RateLimiter)  # on by default
