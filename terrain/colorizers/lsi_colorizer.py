from __future__ import annotations

import numpy as np


def _valid_mask(x: np.ndarray, nodata: float | None) -> np.ndarray:
    if nodata is None:
        return np.isfinite(x)
    return (x != nodata) & np.isfinite(x)


def _rgba_empty(h: int, w: int) -> np.ndarray:
    return np.zeros((h, w, 4), dtype="uint8")


def _lerp(a, b, t):
    return a + (b - a) * t


# ------------------------------------------------------------
# Palettes
# ------------------------------------------------------------
def _palette_yellow_orange_red(
    t: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    0 -> pale yellow
    0.6 -> orange
    1 -> deep red
    """
    c0 = np.array([255, 245, 200], dtype="float32")  # pale yellow
    c1 = np.array([255, 165, 60], dtype="float32")  # orange
    c2 = np.array([200, 30, 30], dtype="float32")  # deep red

    r = np.zeros_like(t, dtype="float32")
    g = np.zeros_like(t, dtype="float32")
    b = np.zeros_like(t, dtype="float32")

    m = t <= 0.6
    if np.any(m):
        tt = (t[m] / 0.6).astype("float32")
        rgb = _lerp(c0, c1, tt[:, None])
        r[m], g[m], b[m] = rgb[:, 0], rgb[:, 1], rgb[:, 2]

    m2 = ~m
    if np.any(m2):
        tt = ((t[m2] - 0.6) / 0.4).astype("float32")
        rgb = _lerp(c1, c2, tt[:, None])
        r[m2], g[m2], b[m2] = rgb[:, 0], rgb[:, 1], rgb[:, 2]

    return r, g, b


def _palette_teal_yellow_pink(
    t: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Piecewise palette:
      0.0 -> deep teal
      0.5 -> warm yellow
      1.0 -> hot pink/red
    """
    # anchors (R,G,B)
    c0 = np.array([20, 60, 90], dtype="float32")  # teal
    c1 = np.array([255, 210, 90], dtype="float32")  # yellow
    c2 = np.array([255, 60, 140], dtype="float32")  # pink/red

    r = np.zeros_like(t, dtype="float32")
    g = np.zeros_like(t, dtype="float32")
    b = np.zeros_like(t, dtype="float32")

    m = t <= 0.5
    if np.any(m):
        tt = (t[m] / 0.5).astype("float32")
        rgb = _lerp(c0, c1, tt[:, None])
        r[m], g[m], b[m] = rgb[:, 0], rgb[:, 1], rgb[:, 2]

    m2 = ~m
    if np.any(m2):
        tt = ((t[m2] - 0.5) / 0.5).astype("float32")
        rgb = _lerp(c1, c2, tt[:, None])
        r[m2], g[m2], b[m2] = rgb[:, 0], rgb[:, 1], rgb[:, 2]

    return r, g, b


# ------------------------------------------------------------
# Base (static predisposition)
# ------------------------------------------------------------
def colorize_lsi_base_rgba(lsi, lsi_nodata, *, background_alpha: int = 210):
    h, w = lsi.shape
    rgba = np.zeros((h, w, 4), dtype="uint8")

    valid = _valid_mask(lsi, lsi_nodata)
    if not np.any(valid):
        return rgba

    t = np.clip(lsi.astype("float32"), 0.0, 1.0)
    r, g, b = _palette_teal_yellow_pink(t[valid])

    rgba[..., 0][valid] = r.astype("uint8")
    rgba[..., 1][valid] = g.astype("uint8")
    rgba[..., 2][valid] = b.astype("uint8")
    rgba[..., 3][valid] = np.uint8(background_alpha)
    return rgba


# ------------------------------------------------------------
# Hazard (base × trigger)
# ------------------------------------------------------------
def colorize_lsi_hazard_rgba(
    hazard: np.ndarray,
    hazard_nodata: float | None,
    *,
    show_from: float = 0.12,  # start showing hazard at ~12%
    gamma: float = 0.6,  # <1 boosts subtle mids
    alpha_min: int = 70,  # minimum visible once activated
    alpha_max: int = 255,
) -> np.ndarray:
    h, w = hazard.shape
    rgba = _rgba_empty(h, w)

    valid = _valid_mask(hazard, hazard_nodata)
    if not np.any(valid):
        return rgba

    t = np.clip(hazard.astype("float32"), 0.0, 1.0)

    # palette
    r, g, b = _palette_teal_yellow_pink(t[valid])
    rgba[..., 0][valid] = r.astype("uint8")
    rgba[..., 1][valid] = g.astype("uint8")
    rgba[..., 2][valid] = b.astype("uint8")

    # alpha ramp (thresholded)
    x = (t - show_from) / max(1e-6, (1.0 - show_from))
    x = np.clip(x, 0.0, 1.0)
    x = np.power(x, gamma)

    # boost low/mid values so it doesn’t disappear
    a = np.power(t, 0.55)  # 0.5–0.7 range works well
    alpha = (alpha_min + (alpha_max - alpha_min) * a).astype("float32")

    # guarantee visibility for anything above a small threshold
    on = valid & (t > 0.05)
    alpha[on] = np.maximum(alpha[on], 80.0)

    rgba[..., 3][valid] = np.clip(alpha[valid], 0, 255).astype("uint8")
    return rgba


# def colorize_lsi_hazard_rgba(
#     hazard: np.ndarray,
#     hazard_nodata: float | None,
#     *,
#     # These are the knobs that make hotspots pop.
#     show_from: float = 0.30,  # hard threshold: below this => transparent
#     alpha_min: int = 0,
#     alpha_max: int = 255,
#     gamma: float = 2.2,  # strong emphasis of high hazard
# ) -> np.ndarray:
#     h, w = hazard.shape
#     rgba = _rgba_empty(h, w)

#     valid = _valid_mask(hazard, hazard_nodata)
#     if not np.any(valid):
#         return rgba

#     t = np.clip(hazard.astype("float32"), 0.0, 1.0)

#     # hard gate low hazard to avoid “everything slightly tinted”
#     vis = valid & (t >= float(show_from))
#     if not np.any(vis):
#         return rgba

#     r, g, b = _palette_yellow_orange_red(t[vis])

#     rgba[..., 0][vis] = r.astype("uint8")
#     rgba[..., 1][vis] = g.astype("uint8")
#     rgba[..., 2][vis] = b.astype("uint8")

#     # alpha ramps only over [show_from..1]
#     tt = np.clip((t - float(show_from)) / max(1e-6, (1.0 - float(show_from))), 0.0, 1.0)
#     a = np.power(tt, float(gamma))
#     alpha = (alpha_min + (alpha_max - alpha_min) * a).astype("float32")
#     rgba[..., 3][vis] = np.clip(alpha[vis], 0, 255).astype("uint8")

#     return rgba


# ------------------------------------------------------------
# Trigger (rain intensity forcing)
# ------------------------------------------------------------
def colorize_lsi_trigger_rgba(
    intensity_mmhr: np.ndarray,
    intensity_nodata: float | None,
    *,
    # Absolute thresholds (mm/hr) so drizzle doesn’t make a “red circle”
    t0: float = 5.0,  # below -> transparent
    t1: float = 15.0,
    t2: float = 30.0,
    t3: float = 60.0,  # above -> saturated
    alpha_max: int = 230,
) -> np.ndarray:
    """
    Visual meaning:
      transparent: < t0
      yellow:      t0..t1
      orange:      t1..t2
      red:         t2..t3+
    """
    h, w = intensity_mmhr.shape
    rgba = _rgba_empty(h, w)

    valid = _valid_mask(intensity_mmhr, intensity_nodata)
    if not np.any(valid):
        return rgba

    x = intensity_mmhr.astype("float32", copy=False)
    vis = valid & (x >= float(t0)) & np.isfinite(x)
    if not np.any(vis):
        return rgba

    # Normalize to 0..1 using absolute scale
    # clamp at t3 so huge values don’t blow the ramp
    t = (np.clip(x, t0, t3) - float(t0)) / max(1e-6, (float(t3) - float(t0)))
    t = np.clip(t, 0.0, 1.0)

    r, g, b = _palette_yellow_orange_red(t[vis])
    rgba[..., 0][vis] = r.astype("uint8")
    rgba[..., 1][vis] = g.astype("uint8")
    rgba[..., 2][vis] = b.astype("uint8")

    # Alpha: keep lows faint, highs strong
    a = np.power(t, 1.6)
    rgba[..., 3][vis] = np.clip(a[vis] * float(alpha_max), 0, 255).astype("uint8")

    return rgba


# # terrain/colorizers/lsi_colorizer.py
# from __future__ import annotations

# import numpy as np


# def _valid_mask(x: np.ndarray, nodata: float | None) -> np.ndarray:
#     if nodata is None:
#         return np.isfinite(x)
#     return (x != nodata) & np.isfinite(x)


# def _rgba_empty(h: int, w: int) -> np.ndarray:
#     return np.zeros((h, w, 4), dtype="uint8")


# def _lerp(a, b, t):
#     return a + (b - a) * t


# def _palette_teal_yellow_pink(
#     t: np.ndarray,
# ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
#     """
#     Piecewise palette:
#       0.0 -> deep teal
#       0.5 -> warm yellow
#       1.0 -> hot pink/red
#     """
#     # anchors (R,G,B)
#     c0 = np.array([20, 60, 90], dtype="float32")  # teal
#     c1 = np.array([255, 210, 90], dtype="float32")  # yellow
#     c2 = np.array([255, 60, 140], dtype="float32")  # pink/red

#     r = np.zeros_like(t, dtype="float32")
#     g = np.zeros_like(t, dtype="float32")
#     b = np.zeros_like(t, dtype="float32")

#     m = t <= 0.5
#     if np.any(m):
#         tt = (t[m] / 0.5).astype("float32")
#         rgb = _lerp(c0, c1, tt[:, None])
#         r[m], g[m], b[m] = rgb[:, 0], rgb[:, 1], rgb[:, 2]

#     m2 = ~m
#     if np.any(m2):
#         tt = ((t[m2] - 0.5) / 0.5).astype("float32")
#         rgb = _lerp(c1, c2, tt[:, None])
#         r[m2], g[m2], b[m2] = rgb[:, 0], rgb[:, 1], rgb[:, 2]

#     return r, g, b


# def colorize_lsi_base_rgba(
#     lsi: np.ndarray,
#     lsi_nodata: float | None,
#     *,
#     background_alpha: int = 190,
# ) -> np.ndarray:
#     """
#     Base susceptibility:
#       - visible over all land (alpha ~ constant)
#       - palette distinct from FSI
#     """
#     h, w = lsi.shape
#     rgba = _rgba_empty(h, w)

#     valid = _valid_mask(lsi, lsi_nodata)
#     if not np.any(valid):
#         return rgba

#     t = np.clip(lsi.astype("float32"), 0.0, 1.0)
#     r, g, b = _palette_teal_yellow_pink(t[valid])

#     rgba[..., 0][valid] = r.astype("uint8")
#     rgba[..., 1][valid] = g.astype("uint8")
#     rgba[..., 2][valid] = b.astype("uint8")
#     rgba[..., 3][valid] = np.uint8(background_alpha)

#     return rgba


# def colorize_lsi_hazard_rgba(
#     hazard: np.ndarray,
#     hazard_nodata: float | None,
#     *,
#     alpha_min: int = 0,
#     alpha_max: int = 255,
# ) -> np.ndarray:
#     """
#     Hazard:
#       - alpha scales with hazard itself (so only the activated zones pop)
#       - same palette, but visually “hotter” where it matters
#     """
#     h, w = hazard.shape
#     rgba = _rgba_empty(h, w)

#     valid = _valid_mask(hazard, hazard_nodata)
#     if not np.any(valid):
#         return rgba

#     t = np.clip(hazard.astype("float32"), 0.0, 1.0)
#     r, g, b = _palette_teal_yellow_pink(t[valid])

#     rgba[..., 0][valid] = r.astype("uint8")
#     rgba[..., 1][valid] = g.astype("uint8")
#     rgba[..., 2][valid] = b.astype("uint8")

#     # alpha proportional to hazard, with optional curve
#     a = t * t  # emphasize extremes
#     alpha = (alpha_min + (alpha_max - alpha_min) * a).astype("float32")
#     rgba[..., 3][valid] = np.clip(alpha[valid], 0, 255).astype("uint8")

#     return rgba


# # def colorize_lsi_rgba(
# #     *,
# #     lsi: np.ndarray,
# #     lsi_nodata: float | None,
# #     dem: np.ndarray | None = None,
# #     dem_nodata: float | None = None,
# #     background_alpha: int = 0,
# # ) -> np.ndarray:
# #     """
# #     LSI colorizer:
# #       - Transparent where invalid/ocean
# #       - Green → Yellow → Orange → Red as susceptibility rises
# #     """
# #     lsi = lsi.astype("float32", copy=False)
# #     h, w = lsi.shape
# #     rgba = np.zeros((h, w, 4), dtype="uint8")

# #     # valid mask from LSI
# #     if lsi_nodata is None:
# #         valid = np.isfinite(lsi)
# #     else:
# #         valid = np.isfinite(lsi) & (lsi != lsi_nodata)

# #     # optional land mask from DEM
# #     if dem is not None:
# #         dem = dem.astype("float32", copy=False)
# #         if dem_nodata is None:
# #             land = np.isfinite(dem) & (dem > 0)
# #         else:
# #             land = np.isfinite(dem) & (dem != dem_nodata) & (dem > 0)
# #         valid = valid & land

# #     if not np.any(valid):
# #         return rgba

# #     v = np.clip(lsi, 0.0, 1.0)

# #     # Simple 4-stop ramp:
# #     # 0.00 -> green
# #     # 0.40 -> yellow
# #     # 0.70 -> orange
# #     # 1.00 -> red
# #     r = np.zeros((h, w), dtype="float32")
# #     g = np.zeros((h, w), dtype="float32")
# #     b = np.zeros((h, w), dtype="float32")

# #     # segment 1: green -> yellow
# #     m1 = valid & (v <= 0.40)
# #     if np.any(m1):
# #         t = v[m1] / 0.40
# #         r[m1] = 40.0 * (1 - t) + 255.0 * t
# #         g[m1] = 200.0 * (1 - t) + 230.0 * t
# #         b[m1] = 60.0 * (1 - t) + 60.0 * t

# #     # segment 2: yellow -> orange
# #     m2 = valid & (v > 0.40) & (v <= 0.70)
# #     if np.any(m2):
# #         t = (v[m2] - 0.40) / 0.30
# #         r[m2] = 255.0
# #         g[m2] = 230.0 * (1 - t) + 140.0 * t
# #         b[m2] = 60.0 * (1 - t) + 40.0 * t

# #     # segment 3: orange -> red
# #     m3 = valid & (v > 0.70)
# #     if np.any(m3):
# #         t = (v[m3] - 0.70) / 0.30
# #         t = np.clip(t, 0.0, 1.0)
# #         r[m3] = 255.0
# #         g[m3] = 140.0 * (1 - t) + 40.0 * t
# #         b[m3] = 40.0 * (1 - t) + 40.0 * t

# #     rgba[..., 0] = r.astype("uint8")
# #     rgba[..., 1] = g.astype("uint8")
# #     rgba[..., 2] = b.astype("uint8")

# #     # alpha: opaque on valid; optional background alpha elsewhere
# #     rgba[..., 3][valid] = 220
# #     if background_alpha > 0:
# #         rgba[..., 3][~valid] = background_alpha

# #     return rgba
