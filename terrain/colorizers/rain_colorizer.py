# terrain/colorizers/rain_colorizer.py

from __future__ import annotations

import numpy as np

# -------------------------------------------------------
# Helpers: distance + wind-aware effective distance
# -------------------------------------------------------


def _distance_km_latlon(
    lat0: float,
    lon0: float,
    lat_grid: np.ndarray,
    lon_grid: np.ndarray,
) -> np.ndarray:
    """
    Equirectangular distance (km) from (lat0, lon0) to each cell.
    Good enough at Timor scales.
    """
    R = 6371.0  # km

    lat0_rad = np.deg2rad(lat0)
    lat_rad = np.deg2rad(lat_grid)

    dlat = lat_rad - lat0_rad
    dlon = np.deg2rad(lon_grid - lon0)

    x = dlon * np.cos((lat_rad + lat0_rad) / 2.0)
    y = dlat

    dist_km = R * np.sqrt(x * x + y * y)
    return dist_km


def _effective_distance_with_wind(
    dist_km: np.ndarray,
    lat0: float,
    lon0: float,
    lat_grid: np.ndarray,
    lon_grid: np.ndarray,
    wind_speed_ms: float,
    wind_dir_deg_from: float,
    anisotropy_factor: float = 0.08,
) -> np.ndarray:
    """
    Same idea as your interpolation:
    shrink distance downwind, stretch upwind.
    """
    lat0_rad = np.deg2rad(lat0)
    lat_rad = np.deg2rad(lat_grid)

    dlat = lat_rad - lat0_rad
    dlon = np.deg2rad(lon_grid - lon0)

    x = dlon * np.cos((lat_rad + lat0_rad) / 2.0)  # east
    y = dlat  # north

    # Bearing station -> cell (0 north, pi/2 east)
    bearing = np.arctan2(x, y)

    # Wind "to" direction (downwind)
    downwind_deg = (wind_dir_deg_from + 180.0) % 360.0
    downwind_rad = np.deg2rad(downwind_deg)

    delta = bearing - downwind_rad
    cos_delta = np.cos(delta)

    scale = 1.0 + anisotropy_factor * wind_speed_ms * cos_delta
    scale = np.clip(scale, 0.25, 2.5)

    return dist_km / scale


# -------------------------------------------------------
# 1) Existing WMO gradient: unchanged
# -------------------------------------------------------


def _wmo_rain_rgb(
    rain_mm: np.ndarray,
    rain_nodata: float | None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    Your original WMO-ish gradient -> returns (r,g,b,valid_mask).
    """
    rain = rain_mm.astype("float32")
    h, w = rain.shape

    if rain_nodata is None:
        valid = (rain > 0) & np.isfinite(rain)
    else:
        valid = (rain != rain_nodata) & (rain > 0) & np.isfinite(rain)

    r = np.zeros((h, w), dtype="float32")
    g = np.zeros((h, w), dtype="float32")
    b = np.zeros((h, w), dtype="float32")

    if not np.any(valid):
        return r.astype("uint8"), g.astype("uint8"), b.astype("uint8"), valid

    # Normalize 0–100 mm, gamma to pop high values
    R_MAX = 100.0
    norm = np.clip(rain / R_MAX, 0.0, 1.0)
    norm = norm**0.6

    # ---- segments ----
    m1 = valid & (norm <= 0.15)
    if np.any(m1):
        t = norm[m1] / 0.15
        r[m1] = 210.0 * (1.0 - t)
        g[m1] = 229.0 * (1.0 - t) + 128.0 * t
        b[m1] = 255.0

    m2 = valid & (norm > 0.15) & (norm <= 0.35)
    if np.any(m2):
        t = (norm[m2] - 0.15) / 0.20
        r[m2] = 0.0
        g[m2] = 128.0 * (1.0 - t) + 255.0 * t
        b[m2] = 255.0

    m3 = valid & (norm > 0.35) & (norm <= 0.60)
    if np.any(m3):
        t = (norm[m3] - 0.35) / 0.25
        r[m3] = 255.0 * t
        g[m3] = 255.0
        b[m3] = 255.0 * (1.0 - t)

    m4 = valid & (norm > 0.60) & (norm <= 0.85)
    if np.any(m4):
        t = (norm[m4] - 0.60) / 0.25
        r[m4] = 255.0
        g[m4] = 255.0 * (1.0 - t)
        b[m4] = 0.0

    m5 = valid & (norm > 0.85)
    if np.any(m5):
        t = np.clip((norm[m5] - 0.85) / 0.15, 0.0, 1.0)
        r[m5] = 255.0
        g[m5] = 0.0
        b[m5] = 255.0 * t

    return (
        r.astype("uint8"),
        g.astype("uint8"),
        b.astype("uint8"),
        valid,
    )


# -------------------------------------------------------
# 2) Final colorizer with per-station radial alpha
# -------------------------------------------------------
def _getattr_or_key(obj, key, default=None):
    """Safely get obj[key] or obj.key or return default."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def colorize_rain_rgba(
    rain_mm: np.ndarray,
    rain_nodata: float | None,
    *,
    lat_grid: np.ndarray | None = None,
    lon_grid: np.ndarray | None = None,
    stations: list[dict] | None = None,
    rain_radius_km: float | None = None,
) -> np.ndarray:
    """
    Rainfall colorizer:

    • RGB = your WMO gradient (unchanged)
    • Alpha:
        - If lat_grid/stations/radius not provided → fully opaque where valid.
        - If provided → for each station:
            α_station = (1 - ρ)^γ,  ρ = r_eff / rain_radius_km
          combine all stations with max() so center is dense,
          edges fade smoothly along the same wind-skewed geometry.
    """
    h, w = rain_mm.shape
    rgba = np.zeros((h, w, 4), dtype="uint8")

    # --- 1) RGB + valid mask from existing logic ---
    r, g, b, valid = _wmo_rain_rgb(rain_mm, rain_nodata)
    rgba[..., 0] = r
    rgba[..., 1] = g
    rgba[..., 2] = b

    if not np.any(valid):
        # nothing to draw
        return rgba

    # ------------------------------------------------
    # 2) If we do NOT have geometric info, just full alpha
    # ------------------------------------------------
    if (
        lat_grid is None
        or lon_grid is None
        or stations is None
        or rain_radius_km is None
        or len(stations) == 0
    ):
        rgba[..., 3][valid] = 255
        rgba[..., 3][~valid] = 0
        return rgba

    # ------------------------------------------------
    # 3) Per-station radial alpha in wind-skewed space
    # ------------------------------------------------
    alpha_field = np.zeros((h, w), dtype="float32")

    for s in stations:
        lat0 = float(_getattr_or_key(s, "lat", 0.0))
        lon0 = float(_getattr_or_key(s, "lon", 0.0))

        rain_s = float(_getattr_or_key(s, "rain_mm", 0.0))

        if rain_s <= 0:
            continue

        ws = s.get("wind_speed_ms", None)
        wd = s.get("wind_dir_deg", None)

        # Base distance from this station to all pixels in tile
        dist_km = _distance_km_latlon(lat0, lon0, lat_grid, lon_grid)

        # If we have wind info, warp distance like interpolation
        if ws is not None and wd is not None and ws > 0:
            dist_eff = _effective_distance_with_wind(
                dist_km=dist_km,
                lat0=lat0,
                lon0=lon0,
                lat_grid=lat_grid,
                lon_grid=lon_grid,
                wind_speed_ms=float(ws),
                wind_dir_deg_from=float(wd),
                anisotropy_factor=0.08,  # match interpolation
            )
        else:
            dist_eff = dist_km

        # Normalized radius ρ = r_eff / R; only inside footprint matters
        rho = dist_eff / float(rain_radius_km)
        mask = rho <= 1.0
        if not np.any(mask):
            continue

        rho_clipped = np.clip(rho, 0.0, 1.0)

        # Alpha contribution: center = 1, edge = 0
        # gamma < 1 → stronger center, gentle falloff
        gamma = 0.8
        contrib = (1.0 - rho_clipped) ** gamma

        # combine by taking the maximum over all stations
        alpha_field = np.maximum(alpha_field, contrib)

    # restrict to pixels where we actually have rainfall
    alpha_field[~valid] = 0.0

    # scale to 0–255
    rgba[..., 3] = (alpha_field * 255.0).astype("uint8")

    return rgba


# import numpy as np
# from scipy.ndimage import distance_transform_edt


# def existing_wmo_colorizer(
#     rain_mm: np.ndarray,
#     rain_nodata: float | None,
#     event_duration_hours: float = 100,
# ) -> np.ndarray:
#     rain = rain_mm.astype("float32")
#     h, w = rain.shape
#     rgba = np.zeros((h, w, 4), dtype="uint8")

#     # valid mask
#     if rain_nodata is None:
#         valid = (rain > 0) & np.isfinite(rain)
#     else:
#         valid = (rain != rain_nodata) & (rain > 0) & np.isfinite(rain)

#     if not np.any(valid):
#         return rgba

#     # normalize
#     R_MAX = 100.0
#     norm = np.clip(rain / R_MAX, 0.0, 1.0)
#     norm = norm**0.6  # gamma

#     # gradient setup
#     r = np.zeros_like(norm, "float32")
#     g = np.zeros_like(norm, "float32")
#     b = np.zeros_like(norm, "float32")

#     # 0–0.15 light blue → blue
#     m1 = valid & (norm <= 0.15)
#     if np.any(m1):
#         t = norm[m1] / 0.15
#         r[m1] = 210 * (1 - t)
#         g[m1] = 229 * (1 - t) + 128 * t
#         b[m1] = 255

#     # 0.15–0.35 blue → cyan
#     m2 = valid & (norm > 0.15) & (norm <= 0.35)
#     if np.any(m2):
#         t = (norm[m2] - 0.15) / 0.20
#         r[m2] = 0
#         g[m2] = 128 * (1 - t) + 255 * t
#         b[m2] = 255

#     # 0.35–0.60 cyan → yellow
#     m3 = valid & (norm > 0.35) & (norm <= 0.60)
#     if np.any(m3):
#         t = (norm[m3] - 0.35) / 0.25
#         r[m3] = 255 * t
#         g[m3] = 255
#         b[m3] = 255 * (1 - t)

#     # 0.60–0.85 yellow → red
#     m4 = valid & (norm > 0.60) & (norm <= 0.85)
#     if np.any(m4):
#         t = (norm[m4] - 0.60) / 0.25
#         r[m4] = 255
#         g[m4] = 255 * (1 - t)
#         b[m4] = 0

#     # >0.85 red → magenta
#     m5 = valid & (norm > 0.85)
#     if np.any(m5):
#         t = np.clip((norm[m5] - 0.85) / 0.15, 0, 1)
#         r[m5] = 255
#         g[m5] = 0
#         b[m5] = 255 * t

#     rgba[..., 0] = r.astype("uint8")
#     rgba[..., 1] = g.astype("uint8")
#     rgba[..., 2] = b.astype("uint8")
#     rgba[..., 3][valid] = 255

#     return rgba


# def colorize_rain_rgba(
#     rain_mm: np.ndarray,
#     rain_nodata: float | None,
#     event_duration_hours: float = 1,
# ) -> np.ndarray:
#     """
#     WMO gradient (unchanged) + radial alpha fade from center→edge of
#     the *actual* rain footprint (including wind-skewed shapes).
#     """

#     # 1) Get your existing colors (RGB + solid alpha)
#     rgba = existing_wmo_colorizer(rain_mm, rain_nodata)

#     # return rgba

#     rain = rain_mm.astype("float32")

#     # same valid mask
#     if rain_nodata is None:
#         valid = (rain > 0) & np.isfinite(rain)
#     else:
#         valid = (rain != rain_nodata) & (rain > 0) & np.isfinite(rain)

#     if not np.any(valid):
#         return rgba

#     # 2) Distance transform INSIDE the rain footprint
#     dist = distance_transform_edt(valid)

#     if dist is None:
#         return rgba

#     # valid distances to compute fade
#     valid_dist = dist[valid]

#     # if rainfall mask extremely small, skip fading
#     if valid_dist.size == 0:
#         return rgba

#     maxd = float(valid_dist.max())
#     if maxd <= 0:
#         # just make valid fully opaque, others transparent
#         rgba[..., 3] = 0
#         rgba[..., 3][valid] = 255
#         return rgba

#     # 3) Build fade: 0 at edge → 1 at center
#     fade = np.zeros_like(dist, dtype="float32")
#     fade[valid] = valid_dist / maxd

#     # tune this exponent for “stronger center” vs “flatter”
#     fade = fade**0.5  # 0.7 = slightly softer; try 0.5 or 0.9 if you want

#     # 4) Apply alpha
#     rgba[..., 3] = 0
#     rgba[..., 3][valid] = (fade[valid] * 255).astype("uint8")

#     return rgba


# def colorize_rain_rgba(
#     rain_mm: np.ndarray,
#     rain_nodata: float | None,
#     event_duration_hours: float = 100,  # kept for API compatibility, not used for now
# ) -> np.ndarray:
#     """
#     Rainfall layer colorizer (event-total rain in mm).

#     • Outside rainfall footprint: fully transparent
#     • 0–100 mm → continuous WMO-style gradient:
#         very light blue → blue → cyan → yellow → red → magenta
#     • Higher rain_mm clearly stands out (gamma-enhanced scaling)
#     """

#     rain = rain_mm.astype("float32")
#     h, w = rain.shape
#     rgba = np.zeros((h, w, 4), dtype="uint8")

#     # --------------------------
#     # 1. Valid rainfall mask
#     # --------------------------
#     if rain_nodata is None:
#         valid = (rain > 0.0) & np.isfinite(rain)
#     else:
#         valid = (rain != rain_nodata) & (rain > 0.0) & np.isfinite(rain)

#     # Outside rainfall footprint → fully transparent
#     if not np.any(valid):
#         return rgba

#     # --------------------------
#     # 2. Normalize rain depth [mm]
#     # --------------------------
#     # Global physical scaling: 0–100 mm per event.
#     # Adjust R_MAX later if your climatology demands it.
#     R_MAX = 100.0  # mm

#     norm = np.clip(rain / R_MAX, 0.0, 1.0)

#     # Gamma < 1.0 → makes high values pop
#     gamma = 0.6
#     norm = norm**gamma

#     # --------------------------
#     # 3. Continuous WMO-style gradient
#     # --------------------------
#     # We build full-size arrays; only "valid" pixels will be written to RGBA.
#     r = np.zeros_like(norm, dtype="float32")
#     g = np.zeros_like(norm, dtype="float32")
#     b = np.zeros_like(norm, dtype="float32")

#     # Segments (on norm):
#     #  0.00–0.15 : very light blue (#d2e5ff) → blue (#0080ff)
#     #  0.15–0.35 : blue (#0080ff) → cyan (#00ffff)
#     #  0.35–0.60 : cyan (#00ffff) → yellow (#ffff00)
#     #  0.60–0.85 : yellow (#ffff00) → red (#ff0000)
#     #  0.85–1.00 : red (#ff0000) → magenta (#ff00ff)

#     # 0–0.15: very light blue → blue
#     m1 = valid & (norm <= 0.15)
#     if np.any(m1):
#         t = norm[m1] / 0.15
#         # from #d2e5ff (210,229,255) to #0080ff (0,128,255)
#         r[m1] = 210.0 * (1.0 - t) + 0.0 * t
#         g[m1] = 229.0 * (1.0 - t) + 128.0 * t
#         b[m1] = 255.0

#     # 0.15–0.35: blue → cyan
#     m2 = valid & (norm > 0.15) & (norm <= 0.35)
#     if np.any(m2):
#         t = (norm[m2] - 0.15) / 0.20
#         # from #0080ff (0,128,255) to #00ffff (0,255,255)
#         r[m2] = 0.0
#         g[m2] = 128.0 * (1.0 - t) + 255.0 * t
#         b[m2] = 255.0

#     # 0.35–0.60: cyan → yellow
#     m3 = valid & (norm > 0.35) & (norm <= 0.60)
#     if np.any(m3):
#         t = (norm[m3] - 0.35) / 0.25
#         # from #00ffff (0,255,255) to #ffff00 (255,255,0)
#         r[m3] = 255.0 * t
#         g[m3] = 255.0
#         b[m3] = 255.0 * (1.0 - t)

#     # 0.60–0.85: yellow → red
#     m4 = valid & (norm > 0.60) & (norm <= 0.85)
#     if np.any(m4):
#         t = (norm[m4] - 0.60) / 0.25
#         # from #ffff00 (255,255,0) to #ff0000 (255,0,0)
#         r[m4] = 255.0
#         g[m4] = 255.0 * (1.0 - t)
#         b[m4] = 0.0

#     # >0.85: red → magenta
#     m5 = valid & (norm > 0.85)
#     if np.any(m5):
#         t = np.clip((norm[m5] - 0.85) / 0.15, 0.0, 1.0)
#         # from #ff0000 (255,0,0) to #ff00ff (255,0,255)
#         r[m5] = 255.0
#         g[m5] = 0.0
#         b[m5] = 255.0 * t

#     # --------------------------
#     # 4. Write into RGBA
#     # --------------------------
#     rgba[..., 0] = r.astype("uint8")
#     rgba[..., 1] = g.astype("uint8")
#     rgba[..., 2] = b.astype("uint8")
#     rgba[..., 3][valid] = 255  # fully opaque where rain exists

#     # All non-valid remain transparent (alpha=0, rgb=0)
#     return rgba


# import numpy as np


# def colorize_rain_rgba(
#     rain_mm: np.ndarray,
#     rain_nodata: float | None,
#     event_duration_hours: float,
# ) -> np.ndarray:
#     """
#     WMO-ish rainfall colorizer with intensity-based opacity.

#     - Outside rainfall footprint → fully transparent
#     - Color encodes intensity (as before)
#     - Alpha encodes intensity too: strongest at center, fades outward
#     """
#     rain_mm = rain_mm.astype("float32")
#     h, w = rain_mm.shape
#     rgba = np.zeros((h, w, 4), dtype="uint8")

#     # Valid rain mask
#     if rain_nodata is None:
#         r_valid = (rain_mm > 0) & np.isfinite(rain_mm)
#     else:
#         r_valid = (rain_mm != rain_nodata) & (rain_mm > 0) & np.isfinite(rain_mm)

#     # Everything else transparent
#     rgba[~r_valid] = [0, 0, 0, 0]

#     if not np.any(r_valid):
#         return rgba

#     # mm/hr (protect against 0 duration)
#     rain_rate = rain_mm.copy()
#     rain_rate[r_valid] = rain_mm[r_valid] / max(event_duration_hours, 1e-6)

#     # Normalize rain rate roughly over 0–100 mm/hr → 0..1
#     rain_norm = np.zeros_like(rain_rate, dtype="float32")
#     rain_norm[r_valid] = np.clip(rain_rate[r_valid] / 100.0, 0.0, 1.0)

#     # --- Color scale (same style as before) ---
#     r = np.zeros_like(rain_norm, dtype="float32")
#     g = np.zeros_like(rain_norm, dtype="float32")
#     b = np.zeros_like(rain_norm, dtype="float32")

#     # 0–0.25: blue -> green
#     m1 = r_valid & (rain_norm <= 0.25)
#     t1 = rain_norm[m1] / 0.25
#     r[m1] = 0.0
#     g[m1] = 255.0 * t1
#     b[m1] = 255.0

#     # 0.25–0.5: green -> yellow
#     m2 = r_valid & (rain_norm > 0.25) & (rain_norm <= 0.5)
#     t2 = (rain_norm[m2] - 0.25) / 0.25
#     r[m2] = 255.0 * t2
#     g[m2] = 255.0
#     b[m2] = 0.0

#     # 0.5–0.75: yellow -> red
#     m3 = r_valid & (rain_norm > 0.5) & (rain_norm <= 0.75)
#     t3 = (rain_norm[m3] - 0.5) / 0.25
#     r[m3] = 255.0
#     g[m3] = 255.0 * (1.0 - t3)
#     b[m3] = 0.0

#     # 0.75–1.0: red -> magenta
#     m4 = r_valid & (rain_norm > 0.75)
#     t4 = (rain_norm[m4] - 0.75) / 0.25
#     r[m4] = 255.0
#     g[m4] = 0.0
#     b[m4] = 255.0 * t4

#     rgba[..., 0][r_valid] = r[r_valid].astype("uint8")
#     rgba[..., 1][r_valid] = g[r_valid].astype("uint8")
#     rgba[..., 2][r_valid] = b[r_valid].astype("uint8")

#     # --- Intensity-based alpha ---
#     # Use a nonlinear curve so weak rain is quite transparent,
#     # strong rain is solid.
#     alpha = np.zeros_like(rain_norm, dtype="float32")
#     alpha[r_valid] = 255.0 * (rain_norm[r_valid] ** 0.7)  # tweak exponent as desired
#     alpha = np.clip(alpha, 0.0, 255.0)

#     rgba[..., 3][r_valid] = alpha[r_valid].astype("uint8")

#     return rgba


# def colorize_rain_rgba(
#     rain_mm: np.ndarray,
#     rain_nodata: float | None,
#     event_duration_hours: float = 100,  # kept for API compatibility, not used for now
# ) -> np.ndarray:
#     """
#     Rainfall layer colorizer (event-total rain in mm).

#     • Outside rainfall footprint: fully transparent
#     • 0–100 mm → continuous WMO-style gradient:
#         very light blue → blue → cyan → yellow → red → magenta
#     • Higher rain_mm clearly stands out (gamma-enhanced scaling)
#     """

#     rain = rain_mm.astype("float32")
#     h, w = rain.shape
#     rgba = np.zeros((h, w, 4), dtype="uint8")

#     # --------------------------
#     # 1. Valid rainfall mask
#     # --------------------------
#     if rain_nodata is None:
#         valid = (rain > 0.0) & np.isfinite(rain)
#     else:
#         valid = (rain != rain_nodata) & (rain > 0.0) & np.isfinite(rain)

#     # Outside rainfall footprint → fully transparent
#     if not np.any(valid):
#         return rgba

#     # --------------------------
#     # 2. Normalize rain depth [mm]
#     # --------------------------
#     # Global physical scaling: 0–100 mm per event.
#     # Adjust R_MAX later if your climatology demands it.
#     R_MAX = 100.0  # mm

#     norm = np.clip(rain / R_MAX, 0.0, 1.0)

#     # Gamma < 1.0 → makes high values pop
#     gamma = 0.6
#     norm = norm**gamma

#     # --------------------------
#     # 3. Continuous WMO-style gradient
#     # --------------------------
#     # We build full-size arrays; only "valid" pixels will be written to RGBA.
#     r = np.zeros_like(norm, dtype="float32")
#     g = np.zeros_like(norm, dtype="float32")
#     b = np.zeros_like(norm, dtype="float32")

#     # Segments (on norm):
#     #  0.00–0.15 : very light blue (#d2e5ff) → blue (#0080ff)
#     #  0.15–0.35 : blue (#0080ff) → cyan (#00ffff)
#     #  0.35–0.60 : cyan (#00ffff) → yellow (#ffff00)
#     #  0.60–0.85 : yellow (#ffff00) → red (#ff0000)
#     #  0.85–1.00 : red (#ff0000) → magenta (#ff00ff)

#     # 0–0.15: very light blue → blue
#     m1 = valid & (norm <= 0.15)
#     if np.any(m1):
#         t = norm[m1] / 0.15
#         # from #d2e5ff (210,229,255) to #0080ff (0,128,255)
#         r[m1] = 210.0 * (1.0 - t) + 0.0 * t
#         g[m1] = 229.0 * (1.0 - t) + 128.0 * t
#         b[m1] = 255.0

#     # 0.15–0.35: blue → cyan
#     m2 = valid & (norm > 0.15) & (norm <= 0.35)
#     if np.any(m2):
#         t = (norm[m2] - 0.15) / 0.20
#         # from #0080ff (0,128,255) to #00ffff (0,255,255)
#         r[m2] = 0.0
#         g[m2] = 128.0 * (1.0 - t) + 255.0 * t
#         b[m2] = 255.0

#     # 0.35–0.60: cyan → yellow
#     m3 = valid & (norm > 0.35) & (norm <= 0.60)
#     if np.any(m3):
#         t = (norm[m3] - 0.35) / 0.25
#         # from #00ffff (0,255,255) to #ffff00 (255,255,0)
#         r[m3] = 255.0 * t
#         g[m3] = 255.0
#         b[m3] = 255.0 * (1.0 - t)

#     # 0.60–0.85: yellow → red
#     m4 = valid & (norm > 0.60) & (norm <= 0.85)
#     if np.any(m4):
#         t = (norm[m4] - 0.60) / 0.25
#         # from #ffff00 (255,255,0) to #ff0000 (255,0,0)
#         r[m4] = 255.0
#         g[m4] = 255.0 * (1.0 - t)
#         b[m4] = 0.0

#     # >0.85: red → magenta
#     m5 = valid & (norm > 0.85)
#     if np.any(m5):
#         t = np.clip((norm[m5] - 0.85) / 0.15, 0.0, 1.0)
#         # from #ff0000 (255,0,0) to #ff00ff (255,0,255)
#         r[m5] = 255.0
#         g[m5] = 0.0
#         b[m5] = 255.0 * t

#     # --------------------------
#     # 4. Write into RGBA
#     # --------------------------
#     rgba[..., 0] = r.astype("uint8")
#     rgba[..., 1] = g.astype("uint8")
#     rgba[..., 2] = b.astype("uint8")
#     rgba[..., 3][valid] = 255  # fully opaque where rain exists

#     # All non-valid remain transparent (alpha=0, rgb=0)
#     return rgba


# def colorize_rain_rgba(rain_mm, rain_nodata, event_duration_hours):
#     """
#     WMO rainfall colorizer.
#     Completely transparent outside rainfall footprint.
#     """
#     rain_mm = rain_mm.astype("float32")

#     h, w = rain_mm.shape
#     rgba = np.zeros((h, w, 4), dtype="uint8")

#     # valid rain
#     if rain_nodata is None:
#         r_valid = (rain_mm > 0) & np.isfinite(rain_mm)
#     else:
#         r_valid = (rain_mm != rain_nodata) & (rain_mm > 0) & np.isfinite(rain_mm)

#     # everything else transparent
#     rgba[~r_valid] = [0, 0, 0, 0]

#     if np.any(r_valid):
#         # Normalize for mm/hr
#         rain_rate = rain_mm[r_valid] / max(event_duration_hours, 1e-6)

#         # ---- WMO color scale ----
#         # rgba_all = np.zeros((h, w, 4), dtype="uint8")
#         rain_norm = np.clip(rain_rate / 100.0, 0, 1)

#         # Blue → Green → Yellow → Red → Purple
#         r = np.zeros_like(rain_norm)
#         g = np.zeros_like(rain_norm)
#         b = np.zeros_like(rain_norm)

#         # 0–0.25 blue -> green
#         m1 = rain_norm <= 0.25
#         t1 = rain_norm[m1] / 0.25
#         r[m1] = 0
#         g[m1] = 255 * t1
#         b[m1] = 255

#         # 0.25–0.5 green -> yellow
#         m2 = (rain_norm > 0.25) & (rain_norm <= 0.5)
#         t2 = (rain_norm[m2] - 0.25) / 0.25
#         r[m2] = 255 * t2
#         g[m2] = 255
#         b[m2] = 0

#         # 0.5–0.75 yellow -> red
#         m3 = (rain_norm > 0.5) & (rain_norm <= 0.75)
#         t3 = (rain_norm[m3] - 0.5) / 0.25
#         r[m3] = 255
#         g[m3] = 255 * (1 - t3)
#         b[m3] = 0

#         # 0.75–1 red -> purple
#         m4 = rain_norm > 0.75
#         t4 = (rain_norm[m4] - 0.75) / 0.25
#         r[m4] = 255
#         g[m4] = 0
#         b[m4] = 255 * t4

#         rgba[..., 0][r_valid] = r.astype("uint8")
#         rgba[..., 1][r_valid] = g.astype("uint8")
#         rgba[..., 2][r_valid] = b.astype("uint8")
#         rgba[..., 3][r_valid] = 255

#     return rgba
