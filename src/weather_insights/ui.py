"""Streamlit interface: three tabs on top of the pipeline.

    World overview  -> live weather of every tracked city on a world map (+ ranking)
    City close-up   -> weather sampled on a grid over one city, with attractions and verdicts
    Analytics       -> trends, comparisons and derived metrics from our own logged history
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from . import charts, load, pipeline, transform
from .config import CACHE_TTL_S, LOG_CSV, MissingAPIKeyError
from .extract import (
    APIError, InvalidAPIKeyError, InvalidInputError, LocationNotFoundError, NetworkError, RateLimitError,
)
from .models import Assumptions, Location, Verdict

WORLD_METRICS = ["temp", "feels_like", "humidity", "wind_speed", "clouds", "comfort_index"]
CLOSEUP_LAYERS = ["temp", "feels_like", "humidity", "wind_speed", "clouds", "precip_mm"]
ANALYTICS_METRICS = ["temp", "feels_like", "humidity", "wind_speed", "precip_mm", "clouds",
                     "comfort_index", "feels_like_delta", "temp_range"]
VERDICT_MD_COLOR = {"good": "green", "caution": "orange", "avoid": "red"}
DEFAULTS = Assumptions()


# ---- cached data access ------------------------------------------------------------------------
# Streamlit reruns this script on every click; the cache keeps API calls (and log rows) to one
# per city per CACHE_TTL_S. Arguments are plain tuples so they hash reliably.
@st.cache_resource
def _client():
    return pipeline.build_client()


def _as_key(loc: Location) -> tuple:
    return (loc.city, loc.country, loc.lat, loc.lon)


@st.cache_data(ttl=CACHE_TTL_S, show_spinner="Fetching live weather for tracked cities…")
def _world_data(keys: tuple):
    return pipeline.refresh_cities(_client(), [Location(*k) for k in keys])


@st.cache_data(ttl=CACHE_TTL_S, show_spinner="Sampling weather across the city…")
def _closeup_data(centre: tuple, grid_size: int, radius_km: float):
    return pipeline.fetch_closeup(_client(), Location(*centre), grid_size, radius_km)


@st.cache_data(ttl=CACHE_TTL_S, show_spinner="Loading the 5-day forecast…")
def _forecast_data(centre: tuple):
    try:
        return pipeline.fetch_forecast(_client(), Location(*centre)), None
    except APIError as exc:
        return [], str(exc)


# ---- small helpers -------------------------------------------------------------------------------
def _show_errors(errors: dict[str, str]) -> None:
    if errors:
        with st.expander(f"⚠️ {len(errors)} location(s) could not be loaded"):
            for place, reason in errors.items():
                st.write(f"**{place}**: {reason}")


def _add_city(query: str) -> None:
    """Sidebar action: validate + geocode + remember a city, with a distinct message per failure."""
    try:
        location, is_new = pipeline.add_city(_client(), query)
    except (InvalidInputError, LocationNotFoundError) as exc:
        st.session_state["flash"] = ("warning", str(exc))
    except (InvalidAPIKeyError, NetworkError, RateLimitError, APIError) as exc:
        st.session_state["flash"] = ("error", str(exc))
    else:
        if is_new:
            st.session_state["flash"] = ("success", f"Added {location}. It will appear after the next refresh.")
            st.cache_data.clear()
        else:
            st.session_state["flash"] = ("info", f"{location} is already tracked.")
    st.rerun()


def _reset_assumptions() -> None:
    st.session_state["a_temp"] = (DEFAULTS.temp_min, DEFAULTS.temp_max)
    st.session_state["a_wind"] = DEFAULTS.max_wind
    st.session_state["a_rain"] = DEFAULTS.max_rain


def _assumption_controls() -> Assumptions:
    """Sidebar sliders for what counts as acceptable weather; they drive verdicts and chart bands."""
    st.sidebar.subheader("Acceptable weather")
    st.sidebar.caption("Your assumptions for a good visit. They change the attraction verdicts and "
                       "the green bands in Analytics.")
    temp = st.sidebar.slider("Temperature range (°C, feels-like)", -20.0, 45.0,
                             (DEFAULTS.temp_min, DEFAULTS.temp_max), step=1.0, key="a_temp")
    wind = st.sidebar.slider("Max wind (m/s)", 2.0, 25.0, DEFAULTS.max_wind, step=0.1, key="a_wind",
                             help="About 10.8 m/s (39 km/h) is where umbrellas stop working.")
    rain = st.sidebar.slider("Max rain / snow (mm/h)", 0.0, 10.0, DEFAULTS.max_rain, step=0.1, key="a_rain",
                             help="0 means any precipitation makes a visit 'not ideal'. "
                                  "Heavy rain (7.6 mm/h or twice your limit) is always 'not recommended'.")
    st.sidebar.button("Reset to defaults", on_click=_reset_assumptions)
    return Assumptions(temp_min=temp[0], temp_max=temp[1], max_wind=wind, max_rain=rain)


def _sidebar(history: pd.DataFrame) -> Assumptions:
    st.sidebar.header("Controls")
    if st.sidebar.button("🔄 Refresh live data", help="Bypass the 10-minute cache and fetch again"):
        st.cache_data.clear()
        st.rerun()

    with st.sidebar.form("add_city", clear_on_submit=True):
        query = st.text_input("Track another city", placeholder="e.g. Seville or Osaka")
        if st.form_submit_button("Add city"):
            _add_city(query)

    st.sidebar.divider()
    assumptions = _assumption_controls()

    cov = transform.history_coverage(history)
    st.sidebar.divider()
    st.sidebar.caption(
        f"**Weather log:** {cov['rows']} observations of {cov['cities']} cities."
        if cov["rows"] else "**Weather log:** empty so far. Every fetch adds to it."
    )
    st.sidebar.caption("Data: OpenWeatherMap (free tier). Verdicts are weather-based advice, "
                       "not official opening information: always check the venue.")
    return assumptions


# ---- tab 1: world overview ---------------------------------------------------------------------------
def _world_tab(cities: list[Location]) -> None:
    result = _world_data(tuple(_as_key(c) for c in cities))
    df = transform.snapshots_to_frame(result.batch.snapshots)
    if df.empty:
        st.error("No city could be loaded right now.")
        _show_errors(result.batch.errors)
        return

    ex = transform.extremes(df)
    cols = st.columns(4)
    cols[0].metric("🔥 Hottest", ex["hottest"]["city"], f"{ex['hottest']['temp']:.1f} °C", delta_color="off")
    cols[1].metric("🥶 Coldest", ex["coldest"]["city"], f"{ex['coldest']['temp']:.1f} °C", delta_color="off")
    cols[2].metric("💨 Windiest", ex["windiest"]["city"], f"{ex['windiest']['wind_speed']:.1f} m/s", delta_color="off")
    cols[3].metric("😊 Most comfortable", ex["most_comfortable"]["city"],
                   f"{ex['most_comfortable']['comfort_index']:.0f} / 100", delta_color="off")

    metric = st.selectbox("Colour the map by", WORLD_METRICS, format_func=transform.METRICS.get, key="world_metric")
    field = transform.interpolate_field(df, metric)
    st.plotly_chart(charts.world_map(df, metric, field), width="stretch")
    st.caption("The colour field is estimated between the tracked cities (nearer cities count more) and "
               "fades out far from any of them; each dot is a real reading. Hover a dot for details.")
    if result.logged:
        st.caption(f"Last refresh added {result.logged} new observations to the weather log.")

    st.subheader("Ranking")
    ranked = transform.rank_cities(df, metric)
    st.dataframe(
        ranked[["rank", "city", "country", "temp", "feels_like", "humidity", "wind_speed", "comfort_index", "description"]],
        hide_index=True, width="stretch",
        column_config={
            "rank": "#", "city": "City", "country": "Country",
            "temp": st.column_config.NumberColumn("Temp (°C)", format="%.1f"),
            "feels_like": st.column_config.NumberColumn("Feels like (°C)", format="%.1f"),
            "humidity": st.column_config.NumberColumn("Humidity (%)", format="%.0f"),
            "wind_speed": st.column_config.NumberColumn("Wind (m/s)", format="%.1f"),
            "comfort_index": st.column_config.NumberColumn("Comfort (0-100)", format="%.0f"),
            "description": "Conditions",
        },
    )
    _show_errors(result.batch.errors)


# ---- tab 2: city close-up ------------------------------------------------------------------------------
def _attraction_card(a, container) -> None:
    """One attraction as a bordered card: big verdict, then the reasons."""
    colour = VERDICT_MD_COLOR[a.verdict.value]
    with container.container(border=True):
        st.markdown(f"### {a.verdict.emoji} {a.attraction.name}")
        st.markdown(f"**:{colour}[{a.verdict.plain.upper()}]**  ·  {a.attraction.category} · {a.attraction.setting}")
        for reason in a.reasons:
            st.markdown(f"- {reason}")
        st.caption(f"{a.snapshot.temp:.0f} °C · wind {a.snapshot.wind_speed:.1f} m/s · {a.snapshot.description}")


def _closeup_tab(cities: list[Location], attractions, assumptions: Assumptions) -> None:
    c1, c2, c3, c4 = st.columns([2, 2, 1, 2])
    city = c1.selectbox("City", cities, format_func=str, key="closeup_city")
    layer = c2.selectbox("Overlay", CLOSEUP_LAYERS, format_func=transform.METRICS.get, key="closeup_layer")
    style = c3.radio("Map style", ["Smooth", "Cells"], key="closeup_style",
                     help="Smooth blends the readings into a continuous surface; Cells shows each reading as a square.")
    fit = c4.checkbox("Fit colours to this city", key="closeup_fit",
                      help="Off: fixed scales (e.g. 0-38 °C), so a colour always means the same value. "
                           "On: stretch the colours to this city's own range to see small differences.")

    # Grid and radius decide how many API calls are made, so they sit behind an Apply button: dragging
    # a slider must not trigger a hundred requests.
    with st.form("closeup_sampling"):
        g1, g2, g3 = st.columns([2, 2, 1])
        grid_size = g1.slider("Real readings per side (grid)", 2, 10, 5, key="closeup_grid",
                              help="Each reading is one API call: points² in total (10 points = 100 calls). The free "
                                   "plan allows 60 calls/minute, so more than 7 points takes over a minute the first time.")
        radius = g2.slider("Radius around the centre (km)", 1, 100, 15, key="closeup_radius")
        g3.markdown("&nbsp;")
        g3.form_submit_button("Apply")

    calls = grid_size ** 2
    pace = f" Large grids are paced to stay under the free limit, so the first load takes about {calls / 50 * 60:.0f} s." if calls > 50 else ""
    st.caption(f"{calls} readings = {calls} API calls, cached for {CACHE_TTL_S // 60} minutes.{pace} "
               "The Smooth style interpolates between the readings, so you do not need many calls for a continuous picture.")

    batch = _closeup_data(_as_key(city), grid_size, float(radius))
    if not batch.snapshots:
        st.error("Could not load weather for this city.")
        _show_errors(batch.errors)
        return

    centre = transform.nearest_snapshot(batch.snapshots, city.lat, city.lon)
    cols = st.columns(5)
    cols[0].metric("Temperature", f"{centre.temp:.1f} °C", f"feels {centre.feels_like:.1f} °C", delta_color="off")
    cols[1].metric("Conditions", centre.description.capitalize())
    cols[2].metric("Humidity", f"{centre.humidity:.0f} %")
    cols[3].metric("Wind", f"{centre.wind_speed:.1f} m/s")
    cols[4].metric("Comfort index", f"{centre.comfort_index:.0f} / 100")

    grid_df = transform.snapshots_to_frame(batch.snapshots)
    city_attractions = pipeline.city_attractions(attractions, city.city)
    assessments = pipeline.assess_closeup(city_attractions, batch, assumptions)
    attr_df = transform.assessments_to_frame(assessments)

    field = (transform.smooth_closeup_field(grid_df, city, grid_size, float(radius), layer)
             if style == "Smooth" else None)
    st.plotly_chart(
        charts.closeup_map(grid_df, attr_df, (city.lat, city.lon), layer, float(radius), grid_size, field, fit),
        width="stretch",
    )
    _show_errors(batch.errors)

    st.subheader(f"What can you visit in {city.city} right now?")
    if not assessments:
        st.info(f"No attractions are curated for {city.city} yet. Add rows to data/attractions.csv.")
        return

    counts = attr_df["verdict_key"].value_counts()
    k1, k2, k3 = st.columns(3)
    k1.metric("✅ Good to go", int(counts.get("good", 0)))
    k2.metric("⚠️ Not ideal", int(counts.get("caution", 0)))
    k3.metric("⛔ Not recommended", int(counts.get("avoid", 0)))
    st.caption(f"Judged against your assumptions: {assumptions.temp_min:.0f}–{assumptions.temp_max:.0f} °C, "
               f"wind up to {assumptions.max_wind:.1f} m/s, rain up to {assumptions.max_rain:.1f} mm/h "
               "(change them in the sidebar).")

    shown = st.multiselect("Show", ["good", "caution", "avoid"], default=["good", "caution", "avoid"],
                           format_func=lambda k: Verdict(k).label, key="closeup_filter")
    visible = sorted((a for a in assessments if a.verdict.value in shown),
                     key=lambda a: (a.verdict.severity, a.attraction.name))
    card_cols = st.columns(2)
    for i, a in enumerate(visible):
        _attraction_card(a, card_cols[i % 2])

    st.subheader("When is the best day to go?")
    chosen = st.selectbox("Attraction", city_attractions, format_func=lambda a: a.name, key="outlook_attraction")
    forecast, error = _forecast_data(_as_key(city))
    if error:
        st.warning(f"Forecast unavailable: {error}")
        return
    outlook = transform.visit_outlook(chosen, forecast, assumptions)
    if outlook.empty:
        st.info("The forecast has no daytime slots to evaluate.")
        return
    best = transform.best_day(outlook)
    st.success(f"Best day for {chosen.name}: **{best:%A %d %B}**")
    st.plotly_chart(charts.outlook_chart(outlook, chosen.name), width="stretch")


# ---- tab 3: analytics ---------------------------------------------------------------------------------------
def _band_caption(metric: str, band, assumptions: Assumptions) -> str:
    if band is None:
        return "No acceptable range is defined for this metric."
    text = {
        "temp": f"{assumptions.temp_min:.0f} to {assumptions.temp_max:.0f} °C",
        "feels_like": f"{assumptions.temp_min:.0f} to {assumptions.temp_max:.0f} °C",
        "wind_speed": f"up to {assumptions.max_wind:.1f} m/s",
        "precip_mm": f"up to {assumptions.max_rain:.1f} mm/h",
    }[metric]
    return f"Green band = your acceptable range ({text}). Change it in the sidebar."


def _analytics_tab(history: pd.DataFrame, assumptions: Assumptions) -> None:
    cov = transform.history_coverage(history)
    if not cov["rows"]:
        st.info("The weather log is empty. Open the World overview tab (or run "
                "`uv run python main.py --refresh`) to record the first observations.")
        return
    c1, c2, c3 = st.columns(3)
    c1.metric("Observations logged", cov["rows"])
    c2.metric("Cities", cov["cities"])
    c3.metric("History span", f"{(cov['last'] - cov['first']).total_seconds() / 3600:.1f} h")
    st.caption(f"From {cov['first']:%Y-%m-%d %H:%M} to {cov['last']:%Y-%m-%d %H:%M} UTC. "
               "Run `main.py --refresh` on a schedule to keep growing it.")

    resolution = st.radio("Time resolution", ["Every observation", "Hourly average", "Daily average"],
                          horizontal=True, key="an_res")
    freq = {"Every observation": None, "Hourly average": "1h", "Daily average": "1D"}[resolution]

    with st.expander("How are the derived metrics computed?"):
        st.markdown(
            "- **Comfort index (0-100):** starts at 100, minus 3 points per °C that *feels-like* is away from 21 °C, "
            "0.3 per humidity point above 60 %, 1.5 per m/s of wind above 5, and 5 per mm/h of precipitation (max 4).\n"
            "- **Feels-like minus actual:** negative = it feels colder than the thermometer says.\n"
            "- **Daily temperature range:** highest minus lowest logged temperature of each day."
        )

    tabs = st.tabs([transform.METRICS[m].split(" (")[0] for m in ANALYTICS_METRICS])
    for tab, metric in zip(tabs, ANALYTICS_METRICS):
        with tab:
            label = transform.METRICS[metric]
            band = assumptions.acceptable_range(metric)
            if metric == "temp_range":
                daily = transform.daily_summary(history)
                series = daily.rename(columns={"date": "timestamp", "temp_range": "value"})[["city", "timestamp", "value"]]
                series["timestamp"] = pd.to_datetime(series["timestamp"])
                bars = daily.groupby("city", as_index=False)["temp_range"].mean().rename(columns={"temp_range": "value"})
                bar_title = "Average daily range"
            else:
                series = transform.metric_timeseries(history, metric, freq)
                bars = transform.latest_per_city(history)[["city", metric]].rename(columns={metric: "value"})
                bar_title = "Latest observation"
            st.caption(_band_caption(metric, band, assumptions))
            left, right = st.columns([3, 2])
            left.markdown(f"**{label} over time, all cities**")
            left.plotly_chart(charts.metric_timeseries_chart(series, label, band), width="stretch")
            right.markdown(f"**{bar_title}, all cities**")
            right.plotly_chart(charts.metric_bar_chart(bars, label, band), width="stretch")
            if series.groupby("city").size().max() < 2:
                st.caption("Each city has a single observation so far, so the lines show dots only. "
                           "They will connect as the log grows.")


# ---- entry point ---------------------------------------------------------------------------------------------------
def render() -> None:
    st.set_page_config(page_title="Weather Insights", page_icon="🌤️", layout="wide")
    st.title("🌤️ Weather Insights")
    st.caption("See the world's weather at a glance, zoom into a city, and find out which attractions "
               "the weather lets you visit.")

    try:
        _client()
    except MissingAPIKeyError as exc:
        st.error(str(exc))
        st.stop()

    flash = st.session_state.pop("flash", None)
    if flash:
        getattr(st, flash[0])(flash[1])

    cities = load.load_cities()
    attractions = load.load_attractions()
    history = load.load_history(LOG_CSV)
    assumptions = _sidebar(history)

    world_tab, closeup_tab, analytics_tab = st.tabs(["🌍 World overview", "🔍 City close-up", "📈 Analytics"])
    with world_tab:
        _world_tab(cities)
    # Re-read the log after the world tab may have appended to it, so Analytics is always current.
    history = load.load_history(LOG_CSV)
    with closeup_tab:
        _closeup_tab(cities, attractions, assumptions)
    with analytics_tab:
        _analytics_tab(history, assumptions)
