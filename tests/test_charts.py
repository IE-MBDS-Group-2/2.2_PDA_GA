"""Raster helpers behind the world map (no Plotly needed: numpy + standard library only)."""

import base64
import struct
import zlib

import numpy as np

import pandas as pd

from weather_insights.charts import bar_order, field_to_rgba, ramp_colorscale, rgba_to_png_data_uri


def decode_png(uri):
    assert uri.startswith("data:image/png;base64,")
    png = base64.b64decode(uri.split(",", 1)[1])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pos, chunks = 8, {}
    while pos < len(png):
        (length,) = struct.unpack(">I", png[pos:pos + 4])
        kind, data = png[pos + 4:pos + 8], png[pos + 8:pos + 8 + length]
        (crc,) = struct.unpack(">I", png[pos + 8 + length:pos + 12 + length])
        assert crc == zlib.crc32(kind + data) & 0xFFFFFFFF
        chunks[kind] = data
        pos += 12 + length
    width, height = struct.unpack(">II", chunks[b"IHDR"][:8])
    raw = np.frombuffer(zlib.decompress(chunks[b"IDAT"]), dtype=np.uint8).reshape(height, 1 + width * 4)
    assert (raw[:, 0] == 0).all()  # filter type 0 on every row
    return raw[:, 1:].reshape(height, width, 4)


def test_png_round_trip_is_lossless():
    rgba = np.random.default_rng(0).integers(0, 256, size=(7, 5, 4), dtype=np.uint8)
    assert (decode_png(rgba_to_png_data_uri(rgba)) == rgba).all()


def test_field_colours_follow_the_ramp_and_alpha():
    values = np.array([[0.0, 10.0]])
    alpha = np.array([[1.0, 0.0]])
    rgba = field_to_rgba(values, alpha, 0.0, 10.0, "temp", max_opacity=1.0)
    assert tuple(rgba[0, 0]) == (0x31, 0x36, 0x95, 255)  # coldest colour, fully opaque
    assert tuple(rgba[0, 1][:3]) == (0xA5, 0x00, 0x26) and rgba[0, 1, 3] == 0  # hottest colour, transparent


def test_constant_field_does_not_divide_by_zero():
    rgba = field_to_rgba(np.full((2, 2), 5.0), np.ones((2, 2)), 5.0, 5.0, "humidity")
    assert rgba.shape == (2, 2, 4)


def test_colorscale_is_valid_for_plotly():
    scale = ramp_colorscale("temp")
    assert scale[0][0] == 0 and scale[-1][0] == 1 and all(0 <= p <= 1 for p, _ in scale)
    assert ramp_colorscale("unknown-metric") == scale  # falls back to the temperature ramp


def test_rain_palette_starts_white_so_dry_means_white():
    scale = ramp_colorscale("precip_mm")
    assert scale[0] == [0.0, "#ffffff"]
    dry = field_to_rgba(np.zeros((1, 1)), np.ones((1, 1)), 0.0, 1.0, "precip_mm", max_opacity=1.0)
    assert tuple(dry[0, 0]) == (255, 255, 255, 255)


def test_bar_order_is_global_by_value_so_in_range_cities_sit_in_the_middle():
    df = pd.DataFrame({"city": ["Hot", "Mild A", "Cold", "Mild B"], "value": [38.0, 18.0, -4.0, 25.0]})
    order = bar_order(df)
    assert order == ["Cold", "Mild A", "Mild B", "Hot"]  # for a 5-32 band the acceptable ones are between the extremes
    inside = [c for c, v in zip(df["city"], df["value"]) if 5 <= v <= 32]
    assert order[1:-1] == [c for c in order if c in inside]


def test_fixed_temperature_scale_maps_0_and_38_to_the_end_colours():
    cold = field_to_rgba(np.array([[-10.0]]), np.ones((1, 1)), 0.0, 38.0, "temp", max_opacity=1.0)  # below range: clipped
    hot = field_to_rgba(np.array([[45.0]]), np.ones((1, 1)), 0.0, 38.0, "temp", max_opacity=1.0)
    assert tuple(cold[0, 0][:3]) == (0x31, 0x36, 0x95) and tuple(hot[0, 0][:3]) == (0xA5, 0x00, 0x26)
