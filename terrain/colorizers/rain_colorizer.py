import numpy as np


def colorize_rain_rgba(rain_mm, rain_nodata, event_duration_hours):
    """
    WMO rainfall colorizer.
    Completely transparent outside rainfall footprint.
    """
    rain_mm = rain_mm.astype("float32")

    h, w = rain_mm.shape
    rgba = np.zeros((h, w, 4), dtype="uint8")

    # valid rain
    if rain_nodata is None:
        r_valid = (rain_mm > 0) & np.isfinite(rain_mm)
    else:
        r_valid = (rain_mm != rain_nodata) & (rain_mm > 0) & np.isfinite(rain_mm)

    # everything else transparent
    rgba[~r_valid] = [0, 0, 0, 0]

    if np.any(r_valid):
        # Normalize for mm/hr
        rain_rate = rain_mm[r_valid] / max(event_duration_hours, 1e-6)

        # ---- WMO color scale ----
        # rgba_all = np.zeros((h, w, 4), dtype="uint8")
        rain_norm = np.clip(rain_rate / 100.0, 0, 1)

        # Blue → Green → Yellow → Red → Purple
        r = np.zeros_like(rain_norm)
        g = np.zeros_like(rain_norm)
        b = np.zeros_like(rain_norm)

        # 0–0.25 blue -> green
        m1 = rain_norm <= 0.25
        t1 = rain_norm[m1] / 0.25
        r[m1] = 0
        g[m1] = 255 * t1
        b[m1] = 255

        # 0.25–0.5 green -> yellow
        m2 = (rain_norm > 0.25) & (rain_norm <= 0.5)
        t2 = (rain_norm[m2] - 0.25) / 0.25
        r[m2] = 255 * t2
        g[m2] = 255
        b[m2] = 0

        # 0.5–0.75 yellow -> red
        m3 = (rain_norm > 0.5) & (rain_norm <= 0.75)
        t3 = (rain_norm[m3] - 0.5) / 0.25
        r[m3] = 255
        g[m3] = 255 * (1 - t3)
        b[m3] = 0

        # 0.75–1 red -> purple
        m4 = rain_norm > 0.75
        t4 = (rain_norm[m4] - 0.75) / 0.25
        r[m4] = 255
        g[m4] = 0
        b[m4] = 255 * t4

        rgba[..., 0][r_valid] = r.astype("uint8")
        rgba[..., 1][r_valid] = g.astype("uint8")
        rgba[..., 2][r_valid] = b.astype("uint8")
        rgba[..., 3][r_valid] = 255

    return rgba
