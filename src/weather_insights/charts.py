"""Plotly figure builders. Pure functions: DataFrame in, Figure out (no Streamlit here)."""

from __future__ import annotations

import base64
import math
import struct
import zlib

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from .transform import METRICS, value_range

MAP_STYLE = "open-street-map"  # free tiles, no map token needed
VERDICT_COLORS = {"good": "#2e9e5b", "caution": "#e0a100", "avoid": "#d64545"}
VERDICT_NAMES = {"good": "Good to go", "caution": "Not ideal", "avoid": "Not recommended"}


# ---- world map: smooth interpolated field + city dots ---------------------------------------------
RAMPS = {  # low -> high colours, one ramp per metric (Plotly colour scales are built from these too)
    "temp": ["#313695", "#4575b4", "#74add1", "#abd9e9", "#e0f3f8", "#ffffbf", "#fee090", "#fdae61", "#f46d43", "#d73027", "#a50026"],
    "humidity": ["#f7fbff", "#c6dbef", "#6baed6", "#2171b5", "#08306b"],
    "wind_speed": ["#fcfbfd", "#dadaeb", "#9e9ac8", "#6a51a3", "#3f007d"],
    "clouds": ["#f7f7f7", "#cccccc", "#969696", "#525252", "#252525"],
    "comfort_index": ["#a50026", "#f46d43", "#fee08b", "#a6d96a", "#1a9850", "#006837"],
    # white = no rain; light blue -> deep blue -> violet as it gets heavier
    "precip_mm": ["#ffffff", "#deebf7", "#9ecae1", "#4292c6", "#2171b5", "#08306b", "#54278f"],
}
RAMPS["feels_like"] = RAMPS["temp"]
DARK_MAP_STYLE = "carto-darkmatter"


def _ramp(metric: str) -> list[str]:
    return RAMPS.get(metric, RAMPS["temp"])


def ramp_colorscale(metric: str) -> list[list]:
    ramp = _ramp(metric)
    return [[i / (len(ramp) - 1), c] for i, c in enumerate(ramp)]


def _hex_to_rgb(colour: str) -> tuple[int, int, int]:
    return int(colour[1:3], 16), int(colour[3:5], 16), int(colour[5:7], 16)


def field_to_rgba(values: np.ndarray, alpha: np.ndarray, vmin: float, vmax: float, metric: str,
                  max_opacity: float = 0.8) -> np.ndarray:
    """Colour a 2-D field with the metric's ramp; returns an (rows, cols, 4) uint8 array."""
    ramp = np.array([_hex_to_rgb(c) for c in _ramp(metric)], dtype=float)
    stops = np.linspace(0, 1, len(ramp))
    t = np.full_like(values, 0.5) if vmax == vmin else np.clip((values - vmin) / (vmax - vmin), 0, 1)
    rgba = np.empty(values.shape + (4,), dtype=np.uint8)
    for channel in range(3):
        rgba[..., channel] = np.interp(t, stops, ramp[:, channel]).round().astype(np.uint8)
    rgba[..., 3] = (alpha * max_opacity * 255).round().astype(np.uint8)
    return rgba


def rgba_to_png_data_uri(rgba: np.ndarray) -> str:
    """Encode an RGBA array as a PNG data URI (pure standard library + numpy, no Pillow needed)."""
    rows, cols, _ = rgba.shape

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    raw = np.concatenate([np.zeros((rows, 1), dtype=np.uint8), rgba.reshape(rows, cols * 4)], axis=1).tobytes()
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", cols, rows, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def world_map(df: pd.DataFrame, metric: str, field: dict) -> go.Figure:
    """Smooth weather field (interpolated between the cities) on a dark map, with a dot per city."""
    label = METRICS[metric]
    lo, hi = value_range(metric, df[metric])
    scale = ramp_colorscale(metric)
    rgba = field_to_rgba(field["values"], field["alpha"], lo, hi, metric)

    hover = [f"<b>{c}, {k}</b><br>{label}: {v:.1f}<br>Temp {t:.1f}°C · feels {f:.1f}°C<br>"
             f"Humidity {h:.0f}% · wind {w:.1f} m/s<br>{d}"
             for c, k, v, t, f, h, w, d in zip(df["city"], df["country"], df[metric], df["temp"], df["feels_like"],
                                              df["humidity"], df["wind_speed"], df["description"])]
    fig = go.Figure()
    fig.add_trace(go.Scattermap(  # white halo so the dots stand out on any colour
        lat=df["lat"], lon=df["lon"], mode="markers", marker={"size": 17, "color": "white", "opacity": 0.9},
        hoverinfo="skip", showlegend=False))
    fig.add_trace(go.Scattermap(
        lat=df["lat"], lon=df["lon"], mode="markers+text", text=df["city"], textposition="top right",
        textfont={"size": 11, "color": "white"}, customdata=hover, hovertemplate="%{customdata}<extra></extra>",
        marker={"size": 12, "color": df[metric], "colorscale": scale, "cmin": lo, "cmax": hi,
                "colorbar": {"title": label}},
        showlegend=False))
    fig.update_layout(
        map={"style": DARK_MAP_STYLE, "center": {"lat": 25, "lon": 10}, "zoom": 1.1,
             "layers": [{
                 "sourcetype": "image", "source": rgba_to_png_data_uri(rgba), "opacity": 1.0,
                 "coordinates": [[field["west"], field["north"]], [field["east"], field["north"]],
                                 [field["east"], field["south"]], [field["west"], field["south"]]],
                 "below": "traces",
             }]},
        margin={"l": 0, "r": 0, "t": 0, "b": 0}, height=600)
    return fig


def zoom_for_radius(radius_km: float) -> float:
    return max(5.0, 11.5 - math.log2(max(radius_km, 1) / 5))


def grid_cells_geojson(grid: pd.DataFrame, radius_km: float, grid_size: int) -> dict:
    """One square polygon per weather sample, so the samples tile the map without gaps.

    A sample sits in the middle of its cell; neighbouring cells meet halfway between samples.
    """
    half_km = radius_km / (grid_size - 1) if grid_size > 1 else radius_km
    features = []
    for cell_id, lat, lon in zip(grid["cell"], grid["lat"], grid["lon"]):
        dlat = half_km / 111.32
        dlon = half_km / (111.32 * max(math.cos(math.radians(lat)), 0.01))
        ring = [[lon - dlon, lat - dlat], [lon + dlon, lat - dlat], [lon + dlon, lat + dlat],
                [lon - dlon, lat + dlat], [lon - dlon, lat - dlat]]
        features.append({"type": "Feature", "id": cell_id, "properties": {},
                         "geometry": {"type": "Polygon", "coordinates": [ring]}})
    return {"type": "FeatureCollection", "features": features}


def _sample_hover(grid: pd.DataFrame, layer: str) -> list[str]:
    label = METRICS[layer]
    return [f"<b>{label}: {v:.1f}</b><br>{d}<br>Temp {t:.1f}°C · feels {f:.1f}°C<br>Humidity {h:.0f}% · wind {w:.1f} m/s"
            for v, d, t, f, h, w in zip(grid[layer], grid["description"], grid["temp"], grid["feels_like"],
                                       grid["humidity"], grid["wind_speed"])]


def _attraction_traces(fig: go.Figure, attractions: pd.DataFrame) -> None:
    for key in ("good", "caution", "avoid"):
        sub = attractions[attractions["verdict_key"] == key] if not attractions.empty else attractions
        if sub.empty:
            continue
        fig.add_trace(go.Scattermap(
            lat=sub["lat"], lon=sub["lon"], mode="markers+text", name=VERDICT_NAMES[key],
            text=sub["Label"], textposition="top center", textfont={"size": 13},
            customdata=sub[["Verdict", "Why"]].values,
            hovertemplate="<b>%{text}</b><br>%{customdata[0]}<br>%{customdata[1]}<extra></extra>",
            marker={"size": 18, "color": VERDICT_COLORS[key]},
        ))


def closeup_map(grid: pd.DataFrame, attractions: pd.DataFrame, centre: tuple[float, float],
                layer: str, radius_km: float, grid_size: int, field: dict | None = None,
                fit_colours: bool = False) -> go.Figure:
    """Weather around the city + attraction pins.

    With ``field`` (from ``transform.smooth_closeup_field``) the weather is a continuous colour
    surface interpolated between the real readings, which are drawn as small dots you can hover.
    Without it, each reading is shown as a flat square cell. Colours use the fixed scale of the
    metric unless ``fit_colours`` stretches them to this city's own range.
    """
    lo, hi = value_range(layer, grid[layer], fit=fit_colours)
    label = METRICS[layer]
    zoom = zoom_for_radius(radius_km)

    if field is not None:
        fig = go.Figure()
        for size, colour in ((13, "white"), (9, None)):  # white halo, then the coloured reading
            fig.add_trace(go.Scattermap(
                lat=grid["lat"], lon=grid["lon"], mode="markers", showlegend=False,
                marker=({"size": size, "color": colour, "opacity": 0.9} if colour else
                        {"size": size, "color": grid[layer], "colorscale": ramp_colorscale(layer), "cmin": lo, "cmax": hi,
                         "colorbar": {"title": label}}),
                **({"hoverinfo": "skip"} if colour else {"text": _sample_hover(grid, layer), "hoverinfo": "text"}),
            ))
        rgba = field_to_rgba(field["values"], np.ones_like(field["values"]), lo, hi, layer,
                              max_opacity=0.55 if layer == "precip_mm" else 0.68)
        layers = [{
            "sourcetype": "image", "source": rgba_to_png_data_uri(rgba), "opacity": 1.0, "below": "traces",
            "coordinates": [[field["west"], field["north"]], [field["east"], field["north"]],
                            [field["east"], field["south"]], [field["west"], field["south"]]],
        }]
        _attraction_traces(fig, attractions)
        fig.update_layout(map={"style": MAP_STYLE, "center": {"lat": centre[0], "lon": centre[1]},
                               "zoom": zoom, "layers": layers}, height=640)
    else:
        cells = grid.reset_index(drop=True).assign(cell=lambda d: d.index.astype(str))
        fig = px.choropleth_map(
            cells, geojson=grid_cells_geojson(cells, radius_km, grid_size), locations="cell",
            color=layer, color_continuous_scale=_ramp(layer), opacity=0.6,
            range_color=(lo, hi) if hi > lo else None, hover_name="description",
            hover_data={"cell": False, "temp": ":.1f", "feels_like": ":.1f", "humidity": ":.0f",
                        "wind_speed": ":.1f", layer: ":.1f"},
            labels={layer: label, "temp": "Temp (°C)", "feels_like": "Feels like (°C)",
                    "humidity": "Humidity (%)", "wind_speed": "Wind (m/s)"},
            center={"lat": centre[0], "lon": centre[1]}, zoom=zoom, map_style=MAP_STYLE, height=640,
        )
        _attraction_traces(fig, attractions)

    fig.update_layout(margin={"l": 0, "r": 0, "t": 0, "b": 0},
                      legend={"orientation": "h", "yanchor": "bottom", "y": 1.01, "x": 0})
    return fig


# ---- analytics (all cities) ----------------------------------------------------------------------
BAND_COLOR = "#2e9e5b"
OUTSIDE_COLOR = "#d64545"


def _padded_range(values: pd.Series, band: tuple[float, float] | None) -> list[float] | None:
    """Axis range that always includes the acceptable band as well as the data."""
    if values.empty:
        return None
    lo, hi = float(values.min()), float(values.max())
    if band:
        lo, hi = min(lo, band[0]), max(hi, band[1])
    pad = (hi - lo) * 0.08 or 1.0
    return [lo - pad, hi + pad]


def metric_timeseries_chart(df: pd.DataFrame, label: str, band: tuple[float, float] | None = None) -> go.Figure:
    """One line per city over time. `band` = acceptable range, shaded green."""
    fig = px.line(df, x="timestamp", y="value", color="city", markers=True,
                  labels={"timestamp": "Time (UTC)", "value": label, "city": "City"})
    if band:
        fig.add_hrect(y0=band[0], y1=band[1], fillcolor=BAND_COLOR, opacity=0.13, line_width=0, layer="below")
        for edge in sorted({band[0], band[1]}):
            fig.add_hline(y=edge, line_dash="dot", line_color=BAND_COLOR, line_width=1.5)
    fig.update_yaxes(range=_padded_range(df["value"], band))
    fig.update_layout(margin={"t": 20}, legend={"title": ""}, height=460)
    return fig


def bar_order(df: pd.DataFrame) -> list[str]:
    """City order for the ranking bars, lowest value first (so the highest ends up on top)."""
    return list(df.sort_values("value", ascending=True)["city"])


def metric_bar_chart(df: pd.DataFrame, label: str, band: tuple[float, float] | None = None) -> go.Figure:
    """One bar per city (columns city, value). With a band, bars are green inside / red outside it."""
    ordered = df.sort_values("value", ascending=True).copy()
    if band:
        inside = ordered["value"].between(band[0], band[1])
        ordered["Status"] = inside.map({True: "Within acceptable range", False: "Outside acceptable range"})
        fig = px.bar(ordered, x="value", y="city", orientation="h", color="Status",
                     color_discrete_map={"Within acceptable range": BAND_COLOR, "Outside acceptable range": OUTSIDE_COLOR},
                     labels={"value": label, "city": ""})
        fig.add_vrect(x0=band[0], x1=band[1], fillcolor=BAND_COLOR, opacity=0.10, line_width=0, layer="below")
        for edge in sorted({band[0], band[1]}):
            fig.add_vline(x=edge, line_dash="dot", line_color=BAND_COLOR, line_width=1.5)
    else:
        fig = px.bar(ordered, x="value", y="city", orientation="h", labels={"value": label, "city": ""})
        fig.update_traces(marker_color="#4c78a8")
    fig.update_xaxes(range=_padded_range(pd.concat([ordered["value"], pd.Series([0.0])]), band))
    # Colouring by status makes Plotly draw one trace per colour and stack the categories in trace
    # order, which groups all "within range" cities together. Pin the order to the values instead.
    fig.update_yaxes(categoryorder="array", categoryarray=bar_order(df))
    fig.update_layout(margin={"t": 20}, legend={"title": "", "orientation": "h", "y": 1.08},
                      height=max(360, 22 * len(ordered) + 140))
    return fig


def outlook_chart(outlook: pd.DataFrame, attraction: str) -> go.Figure:
    """Share of daytime forecast hours with good conditions, coloured by the day's verdict."""
    df = outlook.copy()
    df["good_pct"] = df["good_share"] * 100
    df["date"] = df["date"].astype(str)
    df["Verdict"] = df["verdict"].map(VERDICT_NAMES)
    fig = px.bar(df, x="date", y="good_pct", color="Verdict", title=f"Best day to visit {attraction}",
                 color_discrete_map={VERDICT_NAMES[k]: v for k, v in VERDICT_COLORS.items()},
                 hover_data={"temp_min": ":.0f", "temp_max": ":.0f", "why": True, "good_pct": False},
                 labels={"date": "Day (local)", "good_pct": "Daytime hours with good weather (%)",
                         "temp_min": "Min (°C)", "temp_max": "Max (°C)", "why": "Why"})
    fig.update_yaxes(range=[0, 100])
    fig.update_layout(margin={"t": 50})
    return fig
