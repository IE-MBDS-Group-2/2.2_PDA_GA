import pytest

from conftest import FakeResponse, FakeSession, MADRID
from weather_insights import load, pipeline
from weather_insights.extract import InvalidInputError, NominatimClient

HEADER = "city,name,category,setting,wind_sensitive,lat,lon\n"


@pytest.fixture
def files(tmp_path):
    """(curated, user) CSV paths in a temp folder, so tests never touch the real data/ folder."""
    curated = tmp_path / "curated.csv"
    curated.write_text(HEADER + "Madrid,Retiro Park,Park,outdoor,false,40.4153,-3.6845\n")
    return curated, tmp_path / "user.csv"


def add(files, **overrides):
    curated, user = files
    args = dict(city=MADRID, name="Templo de Debod", category="Temple", setting="outdoor",
                wind_sensitive=False, lat=40.4241, lon=-3.7176)
    return pipeline.add_attraction(**{**args, **overrides}, user_csv=user, curated_csv=curated)


def test_find_poi_returns_what_the_client_found():
    answer = [{"display_name": "Templo de Debod, Madrid", "lat": "40.4241", "lon": "-3.7176",
               "category": "tourism", "type": "museum"}]
    client = NominatimClient(session=FakeSession(FakeResponse(200, answer)))
    (found,) = pipeline.find_poi(client, "Templo de Debod", MADRID)
    assert found.label == "Templo de Debod, Madrid" and found.suggested_category == "Museum"


def test_add_attraction_saves_it_and_it_shows_up_in_the_catalogue(files):
    attraction, is_new = add(files)
    assert is_new is True
    assert (attraction.city, attraction.name, attraction.setting) == ("Madrid", "Templo de Debod", "outdoor")
    assert [a.name for a in load.load_attractions(*files)] == ["Retiro Park", "Templo de Debod"]


def test_adding_the_same_attraction_twice_reports_not_new(files):
    add(files)
    _, is_new = add(files, name="templo DE debod")
    assert is_new is False
    assert len(load.load_attractions(*files)) == 2


def test_input_is_cleaned_before_saving(files):
    attraction, _ = add(files, name="  Templo   de Debod ", category="  ", wind_sensitive=1,
                        lat="40.42411111", lon="-3.71761111")  # forms can hand over text and truthy values
    assert attraction.name == "Templo de Debod"
    assert attraction.category == "Other"  # blank category gets a default
    assert attraction.wind_sensitive is True
    assert (attraction.lat, attraction.lon) == (40.4241, -3.7176)


@pytest.mark.parametrize("overrides, message", [
    ({"setting": "underwater"}, "outdoor, indoor, mixed"),
    ({"name": "   "}, "name of the place"),
    ({"name": "Museo <script>"}, "cannot contain"),
    ({"lat": 95, "lon": 0}, "out of range"),
    ({"lat": 48.8566, "lon": 2.3522}, "from Madrid"),  # Paris: far from the city centre
    ({"lat": "abc"}, "must be numbers"),
    ({"category": "x" * 41}, "too long"),
    ({"category": "=SUM(A1)"}, "cannot start with"),
])
def test_bad_input_is_rejected_and_nothing_is_written(files, overrides, message):
    with pytest.raises(InvalidInputError, match=message):
        add(files, **overrides)
    assert not files[1].exists()  # the user file was never created


def test_search_then_save_end_to_end(files):
    answer = [{"display_name": "Templo de Debod, Madrid", "lat": "40.4241", "lon": "-3.7176",
               "category": "tourism", "type": "museum"}]
    client = NominatimClient(session=FakeSession(FakeResponse(200, answer)))
    pick = pipeline.find_poi(client, "Templo de Debod", MADRID)[0]
    attraction, is_new = add(files, category=pick.suggested_category, lat=pick.lat, lon=pick.lon)
    assert is_new and attraction.category == "Museum"
    assert pipeline.city_attractions(load.load_attractions(*files), "Madrid")[-1].name == "Templo de Debod"
