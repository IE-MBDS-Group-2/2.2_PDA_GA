"""Drives the "add an attraction" panel headlessly with Streamlit's AppTest. No browser, no network."""

import pytest
from streamlit.testing.v1 import AppTest

from weather_insights import pipeline
from weather_insights.extract import NetworkError
from weather_insights.models import Attraction, POICandidate

MATCH = POICandidate("Templo de Debod, Madrid", 40.4241, -3.7176, "tourism", "museum", 1.4)


def panel_app():
    """The script AppTest runs: just the panel for Madrid."""
    from conftest import MADRID
    from weather_insights import ui

    ui._add_attraction_panel(MADRID)


@pytest.fixture
def saved(monkeypatch):
    """Replaces pipeline.add_attraction with a recorder, so no file is written."""
    calls = []

    def fake_add(city, name, category, setting, wind_sensitive, lat, lon, **_):
        calls.append((name, category, setting, wind_sensitive, lat, lon))
        return Attraction(city.city, name, category, setting, wind_sensitive, lat, lon), True

    monkeypatch.setattr(pipeline, "add_attraction", fake_add)
    return calls


def search(monkeypatch, matches, name="Templo de Debod"):
    """Runs the panel and submits the search form; find_poi returns ``matches``."""
    monkeypatch.setattr(pipeline, "find_poi", lambda client, n, city: matches)
    at = AppTest.from_function(panel_app).run()
    at.text_input[0].input(name)
    at.button[0].click().run()
    return at


def page_app():
    """The whole page with every tab body stubbed out: we only care about where the tab bar sits."""
    import pandas as pd
    from weather_insights import load, ui
    from weather_insights.models import Assumptions

    ui._client = lambda: None
    load.load_cities = load.load_attractions = lambda: []
    load.load_history = lambda *a, **k: pd.DataFrame()
    ui._sidebar = lambda history: Assumptions()
    ui._world_tab = ui._closeup_tab = ui._analytics_tab = lambda *a, **k: None
    ui.render()


def tab_bar_position(flash):
    at = AppTest.from_function(page_app)
    if flash:
        at.session_state["flash"] = ("success", "Added Seville.")
    at.run()
    assert not at.exception
    return [el.type for el in at.main.children.values()].index("tab_container")


def test_a_page_message_never_moves_the_tab_bar():
    # Streamlit resets the selected tab to the first one when the tab bar changes position, which
    # sent users back to "World overview" after every message.
    assert tab_bar_position(flash=True) == tab_bar_position(flash=False)


def test_the_page_message_is_still_shown():
    at = AppTest.from_function(page_app)
    at.session_state["flash"] = ("success", "Added Seville.")
    at.run()
    assert at.success[0].value == "Added Seville."


def test_panel_starts_collapsed_with_only_the_search_form():
    at = AppTest.from_function(panel_app).run()
    assert not at.exception
    assert len(at.text_input) == 1 and at.text_input[0].label == "Name of the place"
    assert len(at.radio) == 0  # nothing to confirm yet


def test_search_offers_the_matches_and_a_manual_option(monkeypatch):
    at = search(monkeypatch, [MATCH])
    assert not at.exception
    options = at.radio[0].options
    assert str(MATCH) in options and any("enter coordinates myself" in o for o in options)
    assert at.text_input(key="poi_name_1").value == "Templo de Debod"
    assert at.text_input(key="poi_category_1_0").value == "Museum"  # suggested from OpenStreetMap


def test_confirming_a_match_saves_it_with_the_matched_coordinates(monkeypatch, saved):
    at = search(monkeypatch, [MATCH])
    at.text_input(key="poi_category_1_0").input("Temple")
    at.selectbox(key="poi_setting_1").select("outdoor")
    at.checkbox(key="poi_wind_1").check()
    at.button[1].click().run()
    assert saved == [("Templo de Debod", "Temple", "outdoor", True, 40.4241, -3.7176)]
    assert "Added Templo de Debod to Madrid" in at.success[0].value  # shown in the panel itself
    assert "poi_search" not in at.session_state  # the form closes after a successful save
    assert len(at.radio) == 0


def test_no_matches_means_typing_coordinates(monkeypatch, saved):
    at = search(monkeypatch, [], name="Hidden Garden")
    assert "No match for 'Hidden Garden' near Madrid" in at.info[0].value
    assert len(at.radio) == 0  # nothing to pick from
    at.text_input(key="poi_coords_1").input("40.4153, -3.6845")  # pasted from Google Maps
    at.button[1].click().run()
    assert saved == [("Hidden Garden", "Other", "outdoor", False, 40.4153, -3.6845)]


def test_unreadable_coordinates_are_reported_and_nothing_is_saved(monkeypatch, saved):
    at = search(monkeypatch, [], name="Hidden Garden")
    at.text_input(key="poi_coords_1").input("somewhere near the park")
    at.button[1].click().run()
    assert saved == []
    assert "latitude, longitude" in at.warning[0].value


def test_choosing_none_of_these_asks_for_coordinates(monkeypatch):
    def coordinate_fields(at):
        return [t for t in at.text_input if t.key == "poi_coords_1"]

    at = search(monkeypatch, [MATCH])
    assert coordinate_fields(at) == []  # not needed while a match is chosen
    at.radio[0].set_value(at.radio[0].options[-1]).run()  # "None of these: I will enter coordinates myself"
    assert len(coordinate_fields(at)) == 1


def test_a_failed_search_shows_an_error_and_keeps_the_form_closed(monkeypatch):
    def broken(client, name, city):
        raise NetworkError("Could not reach the place search service.")

    monkeypatch.setattr(pipeline, "find_poi", broken)
    at = AppTest.from_function(panel_app).run()
    at.text_input[0].input("Templo de Debod")
    at.button[0].click().run()
    assert at.error[0].value == "Could not reach the place search service."
    assert len(at.radio) == 0
