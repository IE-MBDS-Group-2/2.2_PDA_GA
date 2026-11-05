"""Orchestrator: the single entry point that runs everything.

    uv run python main.py             # launch the Streamlit app
    uv run python main.py --refresh   # fetch + log weather for all tracked cities (no UI)
    uv run streamlit run main.py      # same as the first line, using Streamlit directly

``--refresh`` is what you schedule (cron / Task Scheduler, e.g. hourly) so the log in
data/weather_log.csv accumulates real history for the Analytics tab.
"""

from __future__ import annotations

import argparse
import subprocess
import sys


def _running_inside_streamlit() -> bool:
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        return get_script_run_ctx(suppress_warning=True) is not None
    except Exception:  # streamlit missing or API changed: treat as plain Python
        return False


def refresh() -> int:
    """CLI job: extract -> transform -> load for every tracked city."""
    from weather_insights import load, pipeline
    from weather_insights.config import MissingAPIKeyError

    try:
        client = pipeline.build_client()
    except MissingAPIKeyError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    cities = load.load_cities()
    result = pipeline.refresh_cities(client, cities)
    print(f"Fetched {result.batch.ok_count}/{len(cities)} cities, logged {result.logged} new rows.")
    for place, reason in result.batch.errors.items():
        print(f"  skipped {place}: {reason}", file=sys.stderr)
    return 0 if result.batch.ok_count else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Weather Insights")
    parser.add_argument("--refresh", action="store_true", help="fetch and log weather, then exit")
    args = parser.parse_args(argv)
    if args.refresh:
        return refresh()
    return subprocess.call([sys.executable, "-m", "streamlit", "run", __file__])


if _running_inside_streamlit():
    from weather_insights import ui

    ui.render()
elif __name__ == "__main__":
    raise SystemExit(main())
