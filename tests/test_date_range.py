"""Analytics date-range slider: the pure filter in transform and the widget in ui. No network."""

from datetime import date

from streamlit.testing.v1 import AppTest

from weather_insights import transform as t

DAY = 86_400
BASE = 1_760_000_000  # 2025-10-09 08:53 UTC


def history(days=(0, 0, 1, 3)):
    """One observation per entry in ``days`` (offset in days from BASE), alternating cities A and B."""
    from conftest import owm_payload
    from weather_insights.models import Location, WeatherSnapshot

    snaps = [WeatherSnapshot.from_api(owm_payload(dt=BASE + d * DAY + i * 60), Location("AB"[i % 2], "XX", 40.0, -3.0))
             for i, d in enumerate(days)]
    return t.snapshots_to_frame(snaps)


# ---- transform.filter_history_by_date ----
def test_filter_keeps_both_end_days_inclusive():
    out = t.filter_history_by_date(history(), date(2025, 10, 9), date(2025, 10, 10))
    assert len(out) == 3 and list(out.index) == [0, 1, 2]


def test_filter_single_day_and_empty_gap():
    assert len(t.filter_history_by_date(history(), date(2025, 10, 12), date(2025, 10, 12))) == 1
    assert t.filter_history_by_date(history(), date(2025, 10, 11), date(2025, 10, 11)).empty


def test_filter_full_range_returns_everything_and_empty_log_is_safe():
    h = history()
    assert len(t.filter_history_by_date(h, date(2025, 10, 9), date(2025, 10, 12))) == len(h)
    assert t.filter_history_by_date(h.iloc[0:0], date(2025, 10, 9), date(2025, 10, 12)).empty


# ---- ui._date_range_slider ----
def slider_app():
    """The script AppTest runs: the slider on the log in st.session_state['days'], then the row count."""
    import streamlit as st
    from test_date_range import history
    from weather_insights import ui

    out = ui._date_range_slider(history(st.session_state.get("days", (0, 0, 1, 3))))
    st.text(f"rows={len(out)}")


def run_slider(days=None):
    at = AppTest.from_function(slider_app)
    if days is not None:
        at.session_state["days"] = days
    return at.run()


def test_slider_starts_at_full_range_so_nothing_is_filtered():
    at = run_slider()
    assert not at.exception
    assert at.slider(key="an_dates").value == (date(2025, 10, 9), date(2025, 10, 12))
    assert at.text[0].value == "rows=4" and not at.caption  # no "Showing x of y" note by default


def test_moving_the_slider_filters_and_says_so():
    at = run_slider()
    at.slider(key="an_dates").set_value((date(2025, 10, 10), date(2025, 10, 12))).run()
    assert not at.exception
    assert at.text[0].value == "rows=2"
    assert "Showing 2 of 4 observations" in at.caption[0].value


def test_single_day_log_draws_no_slider():
    at = run_slider(days=(0, 0))
    assert not at.exception
    assert len(at.slider) == 0 and at.text[0].value == "rows=2"


def two_sliders_app():
    """Two sliders on the same log, as in two metric tabs of the Analytics page."""
    from test_date_range import history
    from weather_insights import ui

    ui._date_range_slider(history(), key="an_dates_temp")
    ui._date_range_slider(history(), key="an_dates_humidity")


def test_sliders_in_different_tabs_stay_in_sync():
    at = AppTest.from_function(two_sliders_app).run()
    picked = (date(2025, 10, 10), date(2025, 10, 12))
    at.slider(key="an_dates_temp").set_value(picked).run()
    assert not at.exception
    assert at.slider(key="an_dates_humidity").value == picked


def analytics_app():
    from test_date_range import history
    from weather_insights import ui
    from weather_insights.models import Assumptions

    ui._analytics_tab(history(), Assumptions())


def test_analytics_tab_puts_a_slider_under_each_time_series_chart():
    at = AppTest.from_function(analytics_app).run()
    assert not at.exception
    from weather_insights.ui import ANALYTICS_METRICS
    assert len(at.slider) == len(ANALYTICS_METRICS)  # one per metric tab
    left_column = at.tabs[0].columns[0]
    kinds = [el.type for el in left_column.children.values()]
    assert kinds.index("plotly_chart") < kinds.index("slider")  # chart first, slider underneath
