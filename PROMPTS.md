# AI collaboration log

One entry per feature, in the order the work was done. Agent: Claude (Claude Code session, model `claude-sonnet-5-5`).
Entries marked **Group review** need a group member to confirm or change the "Kept / changed" line after reading the code.

## Feature: project scope and structure

**Asked:** Build the Weather Insights capstone in the testing-ground folder following the provided template, using uv, pandas, the OpenWeather API and Streamlit, with a clean `data/` folder, a `src/` package with `__init__.py`, separate modules for extract / transform / load / interface, and one orchestrator file. App idea chosen by the group: Spain heatmap idea from our notebook, extended to a worldwide view of major cities plus a city close-up with weather overlaid and attractions we can (or can't) visit depending on the weather.

**Got:** Claude read the brief, the template, `summary.md`, our group notebook and the Session 13/14/16 material, noticed that the project description (extract/transform/load/ui + orchestrator) and the template (client/models/storage/analytics + `app.py`) differ, and asked us which to follow. We had no preference, so it merged them: `extract.py` (= client), `transform.py` (= analytics), `load.py` (= storage), `models.py`, `ui.py`, plus `config.py`, `pipeline.py`, `charts.py` and `main.py` as the orchestrator.

**Checked:** Compared the layout against the rubric rows (OOP, robustness, analytics, Streamlit, repo) in the course README.

**Kept as-is / changed / rejected:** Kept. **Group review:** confirm we are happy with this module naming.

---

## Feature: API client with inheritance and robust errors (`extract.py`)

**Asked:** A generic JSON API base class and an OpenWeatherMap subclass; distinct errors for bad input, bad key, unknown city, rate limit, network and server failures; input validation before any request; batch fetching where one bad location is skipped; the API key must never leak.

**Got:** `JSONAPIClient` (generic `_get`, `_check_status`), `OpenWeatherClient` (`geocode`, `current`, `forecast`), an `APIError` hierarchy, `validate_city_name`, `validate_coordinates`, and `fetch_many` (thread pool, returns successes + a dict of errors).

**Checked:** 27 pytest cases with a fake HTTP session: every status code maps to its own exception, network exceptions are wrapped, bad input never reaches the (fake) network, the key is not in `repr()`, public attributes or any error message, and one failing location in a batch of three leaves the other two. Claude's first version of two tests was wrong (it over-claimed what name-mangling hides, and compared a rounded float too tightly); the tests were fixed, not the code.

**Kept as-is / changed / rejected:** Kept. Claude deliberately catches `requests` exceptions and re-raises with `from None`, because `requests` error text contains the full URL including `appid`. **Group review.** Not yet verified: live calls against the real API (the sandbox that built this had no access to openweathermap.org).

---

## Feature: domain models, comfort index, special methods (`models.py`)

**Asked:** Classes for locations, weather snapshots and attractions, with at least one special method beyond `__init__` and a `@property` that computes something.

**Got:** Frozen dataclasses `Location`, `WeatherSnapshot`, `Attraction`, `Assessment`; `Verdict` enum; `WeatherSnapshot.__str__/__repr__/__lt__` and properties `comfort_index`, `feels_like_delta`, `precip_mm`, `group`, `local_time`; a vectorised `comfort_score()` used both by the property and by the pandas code so the formula exists once.

**Checked:** Hand-computed examples: ideal conditions score 100; feels-like 31 °C, 80 % humidity, 9 m/s wind, 1 mm rain scores 100 − 30 − 6 − 6 − 5 = 53; extreme inputs clip to 0; `sorted()` orders snapshots by temperature; 3-hour forecast rain is converted to an hourly rate.

**Kept as-is / changed / rejected:** Kept the weights (3 / 0.3 / 1.5 / 5) as Claude's own choice. **Group review:** these weights are a judgement call; we should be able to justify or change them.

---

## Feature: analytics and attraction rules (`transform.py`)

**Asked:** At least three analytical views across two categories (trend, comparison, derived metric) computed on the logged data, plus a way to decide whether an attraction can be visited given the weather at its location.

**Got:** `city_trend`, `daily_summary`, `latest_per_city`, `rank_cities`, `extremes`, `city_comparison`; `build_grid` and `nearest_snapshot` (so each attraction uses the nearest sampled weather point); `assess_attraction` with documented thresholds (Beaufort-based wind limits, 7.6 mm/h heavy rain, heat/cold limits, wind-sensitive sites such as cable cars); `visit_outlook` and `best_day` over the 5-day forecast.

**Checked:** 19 parametrised verdict cases (e.g. the same 14 m/s wind is ⛔ for a boat tour and only ⚠️ for a park; thunderstorms never affect a museum), grid geometry, great-circle distance Madrid–London ≈ 1264 km, trend/daily-range/comparison values on a tiny hand-made history, forecast day verdicts on a clear day vs a thunderstorm day.

**Kept as-is / changed / rejected:** Kept. **Group review:** the thresholds are reasonable defaults taken from the Beaufort scale and WMO rain classes, not official opening rules; we should decide whether to tune them.

---

## Feature: logging and catalogues (`load.py`, `data/`)

**Asked:** Append every successful fetch to a CSV with a timestamp so the log accumulates over time; keep curated city and attraction lists in `data/`.

**Got:** `append_snapshots` (writes a header once, skips a city logged less than 10 minutes ago), `load_history` (parses timestamps, adds derived columns, empty frame if no file), `load_cities`/`save_user_city`, `load_attractions`; `data/cities.csv` (20 cities) and `data/attractions.csv` (80 attractions, indoor/outdoor/mixed, wind-sensitive flag).

**Checked:** Tests for header-once, accumulation, the 10-minute duplicate guard, missing file, and consistency of the shipped catalogues (every attraction's city exists, valid settings and coordinate ranges).

**Kept as-is / changed / rejected:** Code kept. **Group review (important):** Claude wrote the attraction names, coordinates and indoor/outdoor labels **from memory**; they are plausible but unverified. Spot-check them on a map before submitting.

---

## Feature: Streamlit app and charts (`ui.py`, `charts.py`, `main.py`)

**Asked:** Three tabs: a world overview of major cities, a city close-up with weather overlaid and attractions, and analytics; user interaction (add a city, choose grid size / radius / overlay); at least two labelled charts; the key must never reach the UI.

**Got:** Plotly map and chart builders, a Streamlit UI with cached data access (10-minute TTL so reruns do not spam the API or the log), a sidebar to add cities (validated and geocoded, each failure mode with its own message) and force a refresh, and `main.py` as the single entry point (`python main.py` launches the app; `--refresh` runs the fetch-and-log job headless).

**Checked:** The Streamlit and Plotly libraries could not be installed in the sandbox, so `ui.py`/`charts.py` were run end-to-end only against stand-in modules and a fake weather client (all tabs execute, the log fills, tables and metrics are built). The real rendering has **not** been seen.

**Kept as-is / changed / rejected:** Kept. The close-up map draws sampled values as coloured markers rather than an interpolated density map, because a density layer would show where samples overlap rather than the actual values. **Group review:** run `uv run python main.py`, click through every tab, and note what looks wrong here.

---

## Feature: round 2 – capitals, 10 attractions per city, verdict cards, editable assumptions, all-city analytics

**Asked:** (1) Use choropleth maps or something more homogeneous than circles across the app, but ask first if that has side effects on other features. (2) Include the capitals of Lebanon, the US, Ecuador, the UAE, Poland, the Philippines and Spain by default. (3) Up to 10 attractions per city and a clearer way to show good-to-go or not. (4) In Analytics, remove the tables, make every chart cover all cities, show all main metrics (temperature, humidity, rain...) and overlay our acceptable ranges. (5) Sidebar controls for those assumptions: a temperature-range slider and one or two more.

**Got:**
- *Choropleth:* the close-up map is now a grid of coloured square cells (`charts.grid_cells_geojson`, `px.choropleth_map`), which has no side effects. For the world map Claude asked first, because a country-level choropleth averages several cities in one country (US, UAE), leaves countries with no tracked city blank, and hides city detail. We chose **country choropleth + city dots**: `charts.world_map` shades each country with the mean of its tracked cities (`transform.country_summary`, using `data/country_codes.csv` for ISO-2 -> ISO-3) and keeps one dot per city, with the same colour scale, on top. Side effect accepted: the world map is now a plain projection map rather than street tiles.
- *Data:* added Beirut, Washington D.C., Quito, Abu Dhabi, Warsaw and Manila (Madrid was already there); `attractions.csv` now has exactly 10 per city (260 rows). Pergamon Museum was replaced by the Neues Museum (Pergamon is closed for renovation) and Cape Point by Signal Hill (it is 48 km from Cape Town, outside any close-up grid).
- *Verdict cards:* each attraction is a bordered card with a large coloured verdict (green / orange / red) and the reasons; map pins are labelled with the verdict emoji; sorted best first with a filter.
- *Assumptions:* new `Assumptions` dataclass (temperature range on feels-like, max wind, max rain) with derived limits, passed through `assess_attraction`, `assess_city` and `visit_outlook`; three sidebar sliders plus a reset button.
- *Analytics:* tables removed; one tab per metric with an all-city time series (line per city) and an all-city bar chart; the acceptable range is shaded green for temperature, feels-like, wind and rain; bars are green inside / red outside the range. Humidity, cloud cover and the derived metrics have no assumption, so they have no band (the tab says so). `city_comparison` was deleted as dead code.

**Checked:** 89 tests pass (up from 78): rules re-run against the new `Assumptions` (e.g. 35 °C feels-like is ⚠️ by default but ✅ with a 36 °C limit; the same 12 m/s wind is ⚠️ at the default limit and ✅ at 14; all wind thresholds scale together; heavy rain stays ⛔ whatever the rain tolerance), validation of impossible settings, all-city time series and daily summaries on a hand-made history. A new data test checks every default capital is present, every city has 8–10 attractions, and every attraction is within 45 km of its city: it caught Cape Point. The analytics data functions were also run on our real `weather_log.csv` (21 cities) for every metric and resolution. As before, Streamlit and Plotly could not be installed in the build sandbox, so the new maps, cards and charts were only exercised against stand-in modules.

**Kept as-is / changed / rejected:** Kept. **Group review:** (a) the default assumptions (5–32 °C, 10.8 m/s, 0 mm/h) are Claude's choice, so decide what our group considers acceptable; (b) the 6 new cities' attractions, and the rest of the 260 rows, were again written from memory, so spot-check names and coordinates; (c) `country_codes.csv` (181 countries) was also written from memory, so a wrong code would only leave that country unshaded; the dots still show.

---

## Feature: world map redone as a smooth colour field (replaces the country choropleth)

**Asked:** "Could you fix the choropleth to look more detailed rather than a single color per country?" with a screenshot of Apple Weather's precipitation map.

**Got:** The country choropleth (and its `country_codes.csv` lookup) was removed. `transform.interpolate_field` estimates the metric on a fine grid between our cities with inverse-distance weighting (rows spaced evenly in Web-Mercator so the picture is not distorted), and fades its opacity to 0 beyond 3,500 km from the nearest city so the map does not invent weather over empty oceans. `charts.field_to_rgba` colours it with a per-metric ramp, `charts.rgba_to_png_data_uri` encodes it as a PNG (standard library + numpy only) and `charts.world_map` lays it over a dark map as an image layer, with a labelled dot per city (white halo, same colour scale, colourbar).

**Checked:** Rendered the raster from our real `weather_log.csv` and looked at it. Claude compared four interpolation settings: plain inverse-distance showed a "bullseye" around every city, Gaussian weighting looked smooth but exaggerated the extremes, so it picked inverse-distance with a 300 km softening (smooth, and the colours stay faithful to the readings). 7 new tests: field shape/geometry/bounds (never outside the min-max of the readings), values follow the nearest city, opacity fades far away, PNG round-trip (CRC and pixel-exact), colour ramp endpoints, constant-field edge case. Streamlit/Plotly could not be run in the build sandbox, so the image layer on the real dark basemap has not been seen.

**Kept as-is / changed / rejected:** Kept. Rejected: the country choropleth (flat colour per country), Gaussian weighting. **Group review:** the field is an *estimate* between cities (we say so in the caption), not measured data; the fade distances (1,200 km full, 3,500 km zero) and 300 km softening are Claude's choices. If the image does not appear on the map, the dots still work, and the layer setup in `charts.world_map` is the place to look.

---

## Feature: round 3 – wider sliders, smoother close-up, rain scale from 0, bar sorting

**Asked:** (1) Bigger slider ranges for grid points and radius, to go more granular. (2) The choropleth still looks too basic: make it a more continuous spectrum, without running out of the free API calls. (3) Precipitation range should start at 0 so no rain shows as white (no negative precipitation). (4) On the Analytics page, sort the latest observations properly so the acceptable-range cities are somewhere in the middle.

**Got:**
- *Sliders:* grid 2–10 points per side (was 3–6) and radius 1–100 km (was 5–40). Because bigger grids mean more calls, Claude added a `RateLimiter` (sliding window, shared by all threads, 50 calls/minute by default, under the free plan's 60) to the generic `JSONAPIClient`, and put the two sliders behind an "Apply" button so dragging cannot fire 100 requests. The caption shows the number of calls and the expected first-load time.
- *Continuous look without extra calls:* `transform.smooth_closeup_field` passes a Catmull-Rom (cubic) surface through the real n x n readings and samples it on a 320 x 320 raster (rows even in Mercator y), shown as an image layer on the map with the real readings as hoverable dots. Failed readings are filled in from neighbours. A "Smooth / Cells" switch keeps the old squares for comparison. The world field was also made to reach further (full strength 1,800 km, fade to 4,500 km from the nearest city), at the cost of estimating over a larger area.
- *Rain from 0:* `transform.value_range` makes rain and wind scales start at 0 (rain spans at least 1 mm/h, wind at least 5 m/s); a new `precip_mm` palette runs white -> blue -> violet, so dry areas are white and the scale never goes negative.
- *Sorting:* the cause was Plotly, not the data: colouring bars by "within / outside range" creates one trace per colour and stacks categories in trace order, grouping the in-range cities together. The order is now pinned to the values (`charts.bar_order`), so for temperature the acceptable cities sit between the too-cold and too-hot ones.

**Checked:** 104 tests (up from 93): the limiter against a fake clock (a burst passes, the 3rd call waits for the window, 100 calls take ~60 s at 50/min, shared by every call of a client); the smooth field reproduces a planar surface exactly, never overshoots the readings, is a gentle gradient rather than a step, fills failed readings, and handles a one-point grid; rain/wind scales; white-at-zero; bar order. Rendered the smooth and cell versions of the same synthetic 5 x 5 readings side by side and looked at them. As before, the Plotly/Streamlit rendering itself could not be run in the build sandbox.

**Kept as-is / changed / rejected:** Kept. **Group review:** (a) the smooth surface is an interpolation, not measured data between the readings (the caption says so); (b) for wind and rain the acceptable cities are the *lowest* values, so they end up at one end of the sorted bars rather than the middle (only temperature has a two-sided range); (c) the 50 calls/minute pacing and the world fade distances are Claude's choices.

---

## Feature: round 4 – fixed, consistent colour scales

**Asked:** "Set the temperature colour scale to be from 0 to 38 °C, and set the other metric scales so colours become more consistent."

**Got:** `transform.COLOUR_RANGES` now fixes the scale of every map metric, so the same colour always means the same value on the world map, the close-up and between refreshes: temperature and feels-like 0–38 °C, humidity / cloud cover / comfort index 0–100, wind 0–20 m/s, rain 0–5 mm/h (white = dry). Values beyond a range simply take the end colour. `transform.value_range(metric, values, fit=False)` returns the fixed range; with `fit=True` it falls back to the data's own min-max (rain and wind still start at 0). Because a fixed 0–38 °C scale makes small differences inside one city hard to see, the close-up has a "Fit colours to this city" checkbox (off by default).

**Checked:** 106 tests (3 new/changed: fixed scales by default, fitted scales follow the data, 0 and 38 °C map exactly to the end colours of the ramp). Rendered the world field from our real `weather_log.csv` with the 0–38 scale and looked at it. Streamlit/Plotly rendering itself still could not be run in the build sandbox.

**Kept as-is / changed / rejected:** Kept. **Group review:** the ranges for wind (20 m/s), rain (5 mm/h) and the 0–100 scales are Claude's choices; adjust `COLOUR_RANGES` if the group prefers others.
