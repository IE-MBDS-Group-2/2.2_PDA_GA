"""Combined attraction filters and their close-up UI wiring, without network access."""
import pytest
from streamlit.testing.v1 import AppTest

from conftest import MADRID, owm_payload
from weather_insights import charts, pipeline, transform, ui
from weather_insights.extract import BatchResult
from weather_insights.models import Assessment, Attraction, Verdict, WeatherSnapshot


def examples():
    snap = WeatherSnapshot.from_api(owm_payload(), MADRID)
    return [Assessment(Attraction("Madrid", name, "Other", setting, False, MADRID.lat, MADRID.lon),
                       verdict, (), snap)
            for name, setting, verdict in [("Park", "outdoor", Verdict.GOOD),
                                           ("Museum", "indoor", Verdict.GOOD),
                                           ("Palace", "mixed", Verdict.CAUTION)]]


@pytest.mark.parametrize("verdicts,settings,names", [
    (["good", "caution", "avoid"], ["indoor", "outdoor", "mixed"], ["Museum", "Park", "Palace"]),
    (["good"], ["indoor", "mixed"], ["Museum"]),
    (["caution"], ["mixed"], ["Palace"]),
    (["good"], ["mixed"], []),
    ([], ["indoor", "outdoor", "mixed"], []),
    (["good", "caution", "avoid"], [], []),
])
def test_combined_filters(verdicts, settings, names):
    original = examples()
    before = list(original)
    assert [a.attraction.name for a in transform.filter_assessments(original, verdicts, settings)] == names
    assert original == before
    assert transform.filter_assessments([], verdicts, settings) == []


def closeup_app():
    from conftest import MADRID
    from weather_insights import ui
    from weather_insights.models import Assumptions
    ui._closeup_tab([MADRID], [], Assumptions())


def test_filters_update_cards_and_map_without_fetching_again(monkeypatch):
    assessments = examples()
    fetches, map_names, card_names = [], [], []
    ui._closeup_data.clear()
    monkeypatch.setattr(ui, "_client", lambda: None)
    def fetch(*args):
        fetches.append(args)
        return BatchResult(snapshots=[assessments[0].snapshot])
    monkeypatch.setattr(pipeline, "fetch_closeup", fetch)
    monkeypatch.setattr(pipeline, "city_attractions", lambda *args: [a.attraction for a in assessments])
    monkeypatch.setattr(pipeline, "assess_closeup", lambda *args: assessments)
    monkeypatch.setattr(ui, "_add_attraction_panel", lambda *args: None)
    monkeypatch.setattr(ui, "_forecast_data", lambda *args: ([], None))
    monkeypatch.setattr(transform, "smooth_closeup_field", lambda *args: None)
    original_map = charts.closeup_map
    def record_map(grid, attractions, *args):
        map_names.append([] if attractions.empty else list(attractions["Attraction"]))
        return original_map(grid, attractions, *args)
    monkeypatch.setattr(charts, "closeup_map", record_map)
    def record_card(a, container):
        card_names.append(a.attraction.name)
    monkeypatch.setattr(ui, "_attraction_card", record_card)
    try:
        at = AppTest.from_function(closeup_app).run()
        assert not at.exception
        assert map_names[-1] == card_names == ["Museum", "Park", "Palace"]
        card_names.clear()
        at.multiselect(key="closeup_setting_filter").set_value(["indoor", "mixed"])
        at.multiselect(key="closeup_filter").set_value(["good"]).run()
        assert not at.exception
        assert map_names[-1] == card_names == ["Museum"]
        card_names.clear()
        at.multiselect(key="closeup_setting_filter").set_value([]).run()
        assert not at.exception
        assert map_names[-1] == card_names == []
        assert any("No attractions match" in message.value for message in at.info)
        at.multiselect(key="closeup_setting_filter").set_value(["indoor", "outdoor", "mixed"])
        at.multiselect(key="closeup_filter").set_value(["good", "caution", "avoid"]).run()
        assert not at.exception
        assert map_names[-1] == card_names == ["Museum", "Park", "Palace"]
        assert len(fetches) == 1
    finally:
        ui._closeup_data.clear()
