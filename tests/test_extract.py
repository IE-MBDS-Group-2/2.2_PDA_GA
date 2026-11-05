import pytest
import requests

from conftest import FakeResponse, FakeSession, owm_payload, MADRID
from weather_insights.extract import (
    APIError, InvalidAPIKeyError, InvalidInputError, JSONAPIClient, LocationNotFoundError,
    NetworkError, NominatimClient, OpenWeatherClient, RateLimiter, RateLimitError, ServerError,
    fetch_many, validate_city_name, validate_coordinates, validate_poi_name,
)
from weather_insights.config import NOMINATIM_PERIOD_S, NOMINATIM_USER_AGENT, POI_MAX_RESULTS
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


# ---- base client: custom headers and provider-specific error wording ----
def test_configured_headers_are_sent_with_every_request():
    """Tests that the base client passes headers to the session.get() call"""
    session = FakeSession(FakeResponse(200, {}))
    JSONAPIClient("https://example.com/api", headers={"User-Agent": "demo/1.0"}, session=session)._get("ping")
    assert session.last_headers == {"User-Agent": "demo/1.0"}


def test_no_headers_means_no_headers_argument():
    """No headers passsed when none configured"""
    session = FakeSession(FakeResponse(200, {}))
    JSONAPIClient("https://example.com/api", session=session)._get("ping")
    assert session.last_headers is None  # nothing extra passed to session.get


def test_error_messages_name_the_service_that_failed():
    """Confirms custom service_name is used in the error message, not a generic "API" or "weather"."""
    class PlaceClient(JSONAPIClient):
        service_name = "place search service"

    for status, fragment in [(403, "refused the request (HTTP 403)"), (429, "Too many requests"),
                             (500, "having problems"), (418, "Unexpected answer")]:
        with pytest.raises(APIError) as info:
            PlaceClient("https://example.com", session=FakeSession(FakeResponse(status, {})))._get("x")
        assert "place search service" in str(info.value) and fragment in str(info.value)
        assert "weather" not in str(info.value)


def test_openweather_keeps_its_own_wording():
    """Confirms that OpenWeatherClient subclasses JSONAPIClient and overrides the 401/403 message."""
    with pytest.raises(InvalidAPIKeyError, match="OPENWEATHER_API_KEY"):
        client(FakeResponse(401, {})).current(MADRID)
    with pytest.raises(NetworkError, match="weather service"):
        client(requests.Timeout("t")).current(MADRID)


# ---- Tests for finding a named place (Nominatim) ----
def hit(name="Templo de Debod, Madrid", lat="40.4241", lon="-3.7176", category="tourism", type_="museum"):
    """One entry of a Nominatim search answer (it sends coordinates as strings)."""
    return {"display_name": name, "lat": lat, "lon": lon, "category": category, "type": type_}


def place_client(*responses):
    return NominatimClient(session=FakeSession(*responses))


@pytest.mark.parametrize("bad", ["", "   ", None, "1234", "Museo <del> Prado", "x" * 101, "!!!"])
def test_invalid_place_names_rejected(bad):
    with pytest.raises(InvalidInputError):
        validate_poi_name(bad)


def test_valid_place_names_cleaned():
    assert validate_poi_name("  Museo   del Prado ") == "Museo del Prado"
    assert validate_poi_name("Torre 2") == "Torre 2"  # digits are fine for places, unlike cities
    assert validate_poi_name("St. Paul's Cathedral") == "St. Paul's Cathedral"
    assert validate_poi_name("Arc de Triomphe (Paris)") == "Arc de Triomphe (Paris)"
    assert validate_poi_name("浅草寺") == "浅草寺"  # letters from any alphabet
    assert validate_poi_name("Museo\t\tdel\nPrado") == "Museo del Prado"  # tabs/newlines collapse


def test_invalid_characters_are_listed_in_the_error():
    with pytest.raises(InvalidInputError, match="cannot contain: < > _"):
        validate_poi_name("Museo_<del>")


def test_search_returns_candidates_with_distance_from_the_city():
    (found,) = place_client(FakeResponse(200, [hit()])).search_poi("Templo de Debod", MADRID)
    assert found.label == "Templo de Debod, Madrid"
    assert (found.lat, found.lon) == (40.4241, -3.7176)
    assert found.km_from_city == pytest.approx(1.4, abs=0.1)
    assert found.suggested_category == "Museum"


def test_search_drops_bus_stops_and_matches_far_from_the_city():
    paris = hit("Templo de Debod, Paris", lat="48.8566", lon="2.3522")
    stop = hit("Templo de Debod, bus stop", category="highway", type_="bus_stop")
    station = hit("Templo de Debod, station", category="railway", type_="station")
    answer = [stop, paris, station, hit()]
    found = place_client(FakeResponse(200, answer)).search_poi("Templo de Debod", MADRID)
    assert [c.label for c in found] == ["Templo de Debod, Madrid"]


def test_search_keeps_the_providers_order():
    answer = [hit("First"), hit("Second", lat="40.5"), hit("Third", lat="40.3")]
    found = place_client(FakeResponse(200, answer)).search_poi("Museo", MADRID)
    assert [c.label for c in found] == ["First", "Second", "Third"]


def test_search_with_no_results_is_an_empty_list_not_an_error():
    assert place_client(FakeResponse(200, [])).search_poi("Zzqxv Plorfington Hall", MADRID) == []


def test_search_asks_nominatim_for_english_results_inside_the_city_box():
    c = place_client(FakeResponse(200, []))
    c.search_poi("  Retiro   Park ", MADRID)
    url, params = c._session.calls[0]
    assert url == "https://nominatim.openstreetmap.org/search"
    assert params["q"] == "Retiro Park, Madrid"  # cleaned name + city
    assert params["accept-language"] == "en" and params["bounded"] == 1
    assert params["limit"] == POI_MAX_RESULTS
    left, top, right, bottom = (float(v) for v in params["viewbox"].split(","))
    assert left < MADRID.lon < right and bottom < MADRID.lat < top


def test_search_identifies_the_app_with_a_user_agent():
    c = place_client(FakeResponse(200, []))
    c.search_poi("Retiro Park", MADRID)
    assert c._session.last_headers == {"User-Agent": NOMINATIM_USER_AGENT}


def test_bad_place_name_never_reaches_the_network():
    c = place_client(FakeResponse(200, []))
    with pytest.raises(InvalidInputError):
        c.search_poi("   ", MADRID)
    assert c._session.calls == []


@pytest.mark.parametrize("status, exc", [(403, InvalidAPIKeyError), (429, RateLimitError), (503, ServerError)])
def test_search_errors_name_the_place_search_service(status, exc):
    with pytest.raises(exc, match="place search service") as info:
        place_client(FakeResponse(status, {})).search_poi("Retiro Park", MADRID)
    assert "weather" not in str(info.value)


def test_search_network_failure_is_wrapped():
    with pytest.raises(NetworkError, match="place search service"):
        place_client(requests.Timeout("t")).search_poi("Retiro Park", MADRID)


@pytest.mark.parametrize("answer", [{}, {"error": "oops"}, [{"display_name": "No coordinates"}], ["not a dict"]])
def test_search_unexpected_answer_is_a_server_error(answer):
    with pytest.raises(ServerError, match="unexpected answer"):
        place_client(FakeResponse(200, answer)).search_poi("Retiro Park", MADRID)


def test_search_is_limited_to_one_request_per_period():
    limiter = NominatimClient()._limiter
    assert (limiter.max_calls, limiter.period) == (1, NOMINATIM_PERIOD_S)
