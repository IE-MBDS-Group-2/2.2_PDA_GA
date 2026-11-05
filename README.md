# Weather Insights

A Streamlit app that answers a traveller's real question: **"What can I actually do in this city today, and when is the best day to go?"**

- **🌍 World overview** – live weather for 26 cities (including the capitals of Spain, the US, Lebanon, Ecuador, the UAE, Poland and the Philippines) on a smooth, weather-app-style colour field (interpolated between the cities and fading out far from any of them; colour scales are fixed so colours mean the same thing everywhere: temperature 0–38 °C, humidity/clouds/comfort 0–100, wind 0–20 m/s, rain 0–5 mm/h with white = dry) with a dot per city, plus the hottest / coldest / windiest / most comfortable city and a sortable ranking.
- **🔍 City close-up** – pick a city and the app samples the weather on a grid around it (you choose the grid and radius, 2–10 points and 1–100 km), paints it on the same fixed colour scales (tick *Fit colours to this city* to stretch them to local values) as a continuous colour surface interpolated between the real readings (or, optionally, as flat cells) over a street map, and pins the city's 10 attractions on top. Each attraction gets a verdict card from the weather *at its own location*: ✅ good to go, ⚠️ not ideal, ⛔ not recommended (e.g. cable cars and boat tours stop in strong wind, outdoor sights in thunderstorms), with the reasons. A 5-day forecast tells you the best day to visit a chosen attraction.
- **📈 Analytics** – built from the app's own accumulating log. One tab per metric (temperature, feels-like, humidity, wind, rain/snow, cloud cover, comfort index, feels-like delta, daily range), each with a time series and a ranking bar chart covering **all cities**, and your acceptable range shaded in green where one applies.
- **Sidebar assumptions** – sliders for the acceptable temperature range, maximum wind and maximum rain. They drive the attraction verdicts and the green bands in Analytics.

Data comes from the [OpenWeatherMap](https://openweathermap.org/api) free tier (current weather, 5-day forecast, geocoding). The free tier has no history, so every fetch is appended to `data/weather_log.csv` and the analytics grow as the log grows.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11+.

```bash
uv sync                                   # creates .venv and installs the package + dependencies
cp .env.example .env                      # then paste your own OpenWeatherMap key into .env
```

`.env` is git-ignored. The key is read with `python-dotenv`, is never printed, and is never shown in the UI. New OpenWeatherMap keys can take up to two hours to activate.

## Run

```bash
uv run python main.py                # launches the Streamlit app
uv run python main.py --refresh      # no UI: fetch + log all tracked cities once
uv run pytest                        # run the tests (no network needed)
```

**Building the history.** Run `main.py --refresh` on a schedule (cron on macOS/Linux, Task Scheduler on Windows; hourly is plenty) so `data/weather_log.csv` accumulates real observations. Commit the log: the Analytics tab is only as interesting as the history behind it. The same city is never logged twice within 10 minutes.

**Rate limits.** The free plan allows 60 calls/minute. The world tab uses one call per tracked city; the close-up uses grid² calls (25 by default, up to 100). The client paces itself to at most 50 calls per minute (`RateLimiter` in `extract.py`), so a big grid is slowed down instead of being rejected: 100 calls take about two minutes the first time. Results are cached for 10 minutes; use *Refresh live data* in the sidebar to force a fetch.

## Project structure

```
.
├── main.py                       # orchestrator: launches the app, or --refresh for the CLI job
├── pyproject.toml                # installable package (src layout) + dependencies
├── .env.example                  # copy to .env (git-ignored) and add your key
├── data/
│   ├── cities.csv                # 26 default cities (name, country, lat, lon)
│   ├── attractions.csv           # 260 curated attractions (10 per city): setting (indoor/outdoor/mixed), wind sensitivity
│   ├── user_cities.csv           # cities added through the sidebar (created on first use)
│   └── weather_log.csv           # our own time series, one row per city per fetch
├── src/weather_insights/
│   ├── config.py                 # paths, constants, API key loading
│   ├── models.py                 # Location, WeatherSnapshot, Assumptions, Attraction, Assessment, Verdict
│   ├── extract.py                # JSONAPIClient (generic) -> OpenWeatherClient, RateLimiter, errors, validation, batch fetch
│   ├── transform.py              # analytics, grid builder, attraction rules, forecast outlook
│   ├── load.py                   # catalogues + CSV log (append / read)
│   ├── pipeline.py               # extract -> transform -> load jobs used by the UI and the CLI
│   ├── charts.py                 # Plotly figure builders (incl. the world colour-field raster)
│   └── ui.py                     # Streamlit interface (three tabs)
├── tests/                        # pytest: hand-made cases with known answers, fake HTTP, no network
└── PROMPTS.md                    # log of AI-assisted work
```

## How it works

| Piece | Design |
|---|---|
| **Inheritance** | `JSONAPIClient` handles any JSON API (request, timeout, status → exception). `OpenWeatherClient` adds the OpenWeatherMap endpoints and parsing. A different provider would only need a new subclass. |
| **Encapsulation** | Credentials live in a name-mangled `__auth_params` and are only attached inside `_get()`; `_get`/`_check_status`/`_session` are protected for subclasses and tests; everything callers need is public. |
| **Special methods / properties** | `WeatherSnapshot` defines `__str__`, `__repr__` and `__lt__` (so `sorted(snapshots)` goes coldest → hottest) and properties `comfort_index`, `feels_like_delta`, `precip_mm`, `local_time`. |
| **Errors** | Bad input, bad key, unknown place, rate limit, network failure and server failure each raise their own exception with a distinct user-facing message; the key never appears in them. One failing city never stops a batch. |
| **Comfort index** | 100 − 3 per °C of *feels-like* away from 21 °C − 0.3 per humidity point above 60 % − 1.5 per m/s of wind above 5 − 5 per mm/h of precipitation (max 4); clipped to 0–100. |
| **Assumptions** | `Assumptions` (frozen dataclass, `models.py`) holds the three sidebar settings: temperature range (applied to feels-like, default 5–32 °C), max wind (default 10.8 m/s) and max rain (default 0 mm/h). Other limits are derived from them, so they always move together. |
| **Attraction verdicts** | Outdoor sites: ⚠️ when feels-like is outside your range, wind reaches your limit, or rain exceeds your limit (or visibility is under 1 km); ⛔ for thunderstorms, heavy rain (7.6 mm/h or twice your limit), gales (1.6 × your wind limit), or feels-like 7 °C above / 15 °C below your range. Wind-sensitive sites (cable cars, boats, towers) close earlier: ⚠️ from 0.74 × and ⛔ from 1.29 × your wind limit. Mixed sites are only downgraded by severe weather; indoor sites are always fine except observation decks in high wind. |

## Known limitations

- Verdicts are **weather-based advice**, not official opening information. Check the venue before travelling.
- `data/attractions.csv` is hand-curated (coordinates and the indoor/outdoor classification should be spot-checked before submission). Cities added through the sidebar have no attractions until rows are added to that file.
- OpenWeatherMap's free tier gives current conditions and a 3-hourly 5-day forecast only; there is no historical data, hence the self-built log.

## Team

<!-- Names and a one-line summary of who worked on what. -->
