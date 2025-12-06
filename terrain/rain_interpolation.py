from __future__ import annotations

from dataclasses import dataclass
from typing import List

import numpy as np
from rasterio.transform import Affine


@dataclass
class RainStation:
    lat: float
    lon: float
    rain_mm: float  # event rainfall or accumulated rainfall
    wind_speed_ms: float | None = None  # instantaneous wind speed
    wind_dir_deg: float | None = None  # degrees FROM which wind blows (met convention)


def _latlon_grid(
    transform: Affine, rows: int, cols: int
) -> tuple[np.ndarray, np.ndarray]:
    """
    Build 2D arrays of lat, lon for each cell center.
    """
    r_idx, c_idx = np.indices((rows, cols))

    lon = transform.c + (c_idx + 0.5) * transform.a + (r_idx + 0.5) * transform.b
    lat = transform.f + (c_idx + 0.5) * transform.d + (r_idx + 0.5) * transform.e

    return lat.astype("float32"), lon.astype("float32")


def _distance_km(
    lat0: float, lon0: float, lat: np.ndarray, lon: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Approximate dx, dy and great-circle distance in km from (lat0, lon0)
    to each grid point.
    """
    R_earth = 6371.0  # km

    lat0_rad = np.deg2rad(lat0)
    lat_rad = np.deg2rad(lat)

    dlat = lat_rad - lat0_rad
    dlon = np.deg2rad(lon - lon0)

    # equirectangular projection: x=east, y=north
    x = dlon * np.cos((lat_rad + lat0_rad) / 2.0)
    y = dlat

    dx_km = R_earth * x
    dy_km = R_earth * y

    dist_km = np.sqrt(dx_km**2 + dy_km**2)
    return dx_km, dy_km, dist_km


def _elliptical_wind_distance(
    dx_km: np.ndarray,
    dy_km: np.ndarray,
    wind_speed_ms: float | None,
    wind_dir_deg_from: float | None,
    base_radius_km: float,
    anisotropy_strength: float = 0.03,
    q_max: float = 2.0,
) -> np.ndarray:
    """
    Return an *effective distance* [km] that defines an ellipse aligned
    with the downwind direction, but preserves total area ~ π * R^2.

    - For zero / missing wind: returns isotropic distance (sqrt(dx^2+dy^2)).
    - For stronger winds: stretches downwind, squeezes cross-wind, but the
      radius_km is still the "nominal" footprint size.

    Geometry:
        q = 1 + s * wind_speed
        a = R * q           (semi-axis along downwind)
        b = R / q           (semi-axis cross-wind)
        s^2 = (x_par^2 / a^2) + (y_perp^2 / b^2)
        rho = R * s         (effective distance used for weighting)
    """
    # No wind: just Euclidean distance
    if (
        wind_speed_ms is None
        or wind_dir_deg_from is None
        or wind_speed_ms <= 0.0
        or anisotropy_strength <= 0.0
    ):
        return np.sqrt(dx_km**2 + dy_km**2)

    # Convert "from" direction to downwind "to" direction (deg from north, clockwise)
    theta_from = np.deg2rad(wind_dir_deg_from)
    theta_to = (theta_from + np.pi) % (2.0 * np.pi)

    # Unit vector downwind in our (x=east, y=north) basis
    ux = np.sin(theta_to)  # east component
    uy = np.cos(theta_to)  # north component

    # Rotate coordinates into wind frame
    # x_par  : axis along downwind direction
    # y_perp : axis perpendicular to downwind
    x_par = dx_km * ux + dy_km * uy
    y_perp = -dx_km * uy + dy_km * ux

    # Anisotropy ratio q: how elongated along wind
    q = 1.0 + anisotropy_strength * wind_speed_ms
    q = np.clip(q, 1.0, q_max)

    # Semi-axes of ellipse
    a = base_radius_km * q
    b = base_radius_km / q

    # Elliptical radius (normalized)
    # s=1 defines the ellipse boundary
    a2 = (a * a) + 1e-6
    b2 = (b * b) + 1e-6
    s2 = (x_par**2) / a2 + (y_perp**2) / b2
    s = np.sqrt(s2)

    # Effective distance in km
    rho = base_radius_km * s
    return rho


def interpolate_rainfall_to_grid(
    dem_array: np.ndarray,
    transform: Affine,
    stations: List[RainStation],
    power: float = 2.0,
    min_dist_cells: float = 1.0,
    rain_radius_km: float = 10.0,
    use_wind: bool = True,
) -> np.ndarray:
    """
    Inverse-distance weighted rainfall interpolation with optional
    wind-aware, *area-preserving* elliptical kernel.

    - If wind is missing or disabled → simple circular IDW within rain_radius_km.
    - If wind is present → elliptical footprint aligned with downwind direction,
      stretched by wind_speed, but total "area of influence" stays ~constant.
    """

    rows, cols = dem_array.shape
    lat_grid, lon_grid = _latlon_grid(transform, rows, cols)

    # approximate km per cell in latitude for min_dist
    cell_dlat = abs(transform.e)
    cell_km = 111.0 * cell_dlat if cell_dlat > 0 else 1.0
    min_dist_km = max(min_dist_cells * cell_km, 0.01)

    rain_sum = np.zeros((rows, cols), dtype="float32")
    weight_sum = np.zeros((rows, cols), dtype="float32")

    for st in stations:
        if st.rain_mm <= 0:
            continue

        dx_km, dy_km, dist_km = _distance_km(st.lat, st.lon, lat_grid, lon_grid)

        if use_wind:
            dist_eff_km = _elliptical_wind_distance(
                dx_km=dx_km,
                dy_km=dy_km,
                wind_speed_ms=st.wind_speed_ms,
                wind_dir_deg_from=st.wind_dir_deg,
                base_radius_km=rain_radius_km,
                anisotropy_strength=0.03,  # tweak: smaller → less elongation
                q_max=2.0,  # max elongation factor
            )
        else:
            dist_eff_km = dist_km

        # Final radius mask: effective distance <= rain_radius_km
        mask = dist_eff_km <= rain_radius_km
        if not np.any(mask):
            continue

        # Avoid singularity at station
        dist_eff_km = np.maximum(dist_eff_km, min_dist_km)

        w = np.zeros_like(dist_eff_km, dtype="float32")
        w[mask] = 1.0 / (dist_eff_km[mask] ** power)

        rain_sum[mask] += st.rain_mm * w[mask]
        weight_sum[mask] += w[mask]

    result = np.zeros_like(rain_sum, dtype="float32")
    valid = weight_sum > 0
    result[valid] = rain_sum[valid] / weight_sum[valid]

    return result


# from __future__ import annotations

# from dataclasses import dataclass
# from typing import List

# import numpy as np
# from rasterio.transform import Affine


# @dataclass
# class RainStation:
#     lat: float
#     lon: float
#     rain_mm: float
#     wind_speed_ms: float | None = None
#     wind_dir_deg: float | None = None


# def _latlon_grid(transform: Affine, rows: int, cols: int):
#     r_idx, c_idx = np.indices((rows, cols))
#     lon = transform.c + (c_idx + 0.5) * transform.a
#     lat = transform.f + (r_idx + 0.5) * transform.e
#     return lat.astype("float32"), lon.astype("float32")


# def _distance_km(lat0, lon0, lat, lon):
#     R = 6371.0
#     lat0r = np.deg2rad(lat0)
#     latr = np.deg2rad(lat)
#     dlat = latr - lat0r
#     dlon = np.deg2rad(lon - lon0)
#     x = dlon * np.cos((latr + lat0r) / 2)
#     y = dlat
#     dx = R * x
#     dy = R * y
#     dist = np.sqrt(dx * dx + dy * dy)
#     return dx, dy, dist


# def _apply_wind_anisotropy(
#     dx_km,
#     dy_km,
#     dist_km,
#     wind_speed_ms,
#     wind_dir_deg_from,
#     anisotropy_factor=0.10,
# ):
#     """
#     Converts circular distance -> wind-skewed effective distance.
#     Preserves radius, creates realistic teardrop/guitar-pick shapes.
#     """

#     # bearing station → cell
#     bearing = np.arctan2(dx_km, dy_km)

#     # wind comes FROM direction; storm pushed TO opposite
#     downwind_rad = np.deg2rad((wind_dir_deg_from + 180) % 360)

#     # angle difference
#     delta = bearing - downwind_rad
#     cos_delta = np.cos(delta)

#     # scale shrinks downwind, enlarges upwind
#     scale = 1.0 - anisotropy_factor * wind_speed_ms * cos_delta
#     scale = np.clip(scale, 0.4, 2.0)  # prevents wild shapes

#     dist_eff = dist_km * scale
#     return dist_eff


# def interpolate_rainfall_to_grid(
#     dem_array: np.ndarray,
#     transform: Affine,
#     stations: List[RainStation],
#     power: float = 2.0,
#     min_dist_cells: float = 1.0,
#     rain_radius_km: float = 10.0,
#     use_wind: bool = True,
# ) -> np.ndarray:
#     rows, cols = dem_array.shape
#     lat_grid, lon_grid = _latlon_grid(transform, rows, cols)

#     # km per cell for minimum stabilizer
#     cell_dlat = abs(transform.e)
#     km_per_cell = 111.0 * cell_dlat
#     min_dist_km = max(km_per_cell * min_dist_cells, 0.01)

#     rain_sum = np.zeros((rows, cols), dtype="float32")
#     weight_sum = np.zeros((rows, cols), dtype="float32")

#     for st in stations:
#         if st.rain_mm <= 0:
#             continue

#         dx_km, dy_km, dist_km = _distance_km(st.lat, st.lon, lat_grid, lon_grid)

#         # apply wind skew
#         if use_wind and st.wind_speed_ms and st.wind_dir_deg is not None:
#             dist_eff = _apply_wind_anisotropy(
#                 dx_km,
#                 dy_km,
#                 dist_km,
#                 st.wind_speed_ms,
#                 st.wind_dir_deg,
#                 anisotropy_factor=0.10,
#             )
#         else:
#             dist_eff = dist_km

#         # radius filter (ENSURES correct radius_km)
#         mask = dist_eff <= rain_radius_km
#         if not np.any(mask):
#             continue

#         dist_eff = np.maximum(dist_eff, min_dist_km)

#         w = np.zeros_like(dist_eff, dtype="float32")
#         w[mask] = 1.0 / (dist_eff[mask] ** power)

#         rain_sum[mask] += st.rain_mm * w[mask]
#         weight_sum[mask] += w[mask]

#     out = np.zeros_like(rain_sum)
#     valid = weight_sum > 0
#     out[valid] = rain_sum[valid] / weight_sum[valid]
#     return out


# def interpolate_rainfall_to_grid(
#     dem_array: np.ndarray,
#     transform: Affine,
#     stations: List[RainStation],
#     power: float = 2.2,
#     min_dist_cells: float = 1.0,
#     rain_radius_km: float = 5.0,
#     use_wind: bool = True,
# ):
#     rows, cols = dem_array.shape
#     lat_grid, lon_grid = _latlon_grid(transform, rows, cols)

#     cell_dlat = abs(transform.e)
#     cell_km = max(111.0 * cell_dlat, 0.01)
#     min_dist_km = max(cell_km * min_dist_cells, 0.05)

#     rain_sum = np.zeros((rows, cols), "float32")
#     weight_sum = np.zeros((rows, cols), "float32")

#     for st in stations:
#         if st.rain_mm <= 0:
#             continue

#         dx, dy, dist = _distance_km(st.lat, st.lon, lat_grid, lon_grid)

#         # Strong optimization mask: only evaluate inside radius × 1.2
#         pre_mask = dist <= rain_radius_km * 1.2
#         if not np.any(pre_mask):
#             continue

#         # Wind influence
#         if use_wind and st.wind_speed_ms is not None and st.wind_dir_deg is not None:
#             dist_eff = _apply_wind_anisotropy(
#                 dx, dy, dist, st.wind_speed_ms, st.wind_dir_deg
#             )
#         else:
#             dist_eff = dist

#         # *** HARD RADIUS CAP ***
#         mask = dist_eff <= rain_radius_km
#         if not np.any(mask):
#             continue

#         dist_eff = np.maximum(dist_eff, min_dist_km)

#         w = np.zeros_like(dist_eff, "float32")
#         w[mask] = 1.0 / (dist_eff[mask] ** power)

#         rain_sum[mask] += st.rain_mm * w[mask]
#         weight_sum[mask] += w[mask]

#     result = np.zeros_like(rain_sum, "float32")
#     valid = weight_sum > 0
#     result[valid] = rain_sum[valid] / weight_sum[valid]
#     return result


# def interpolate_rainfall_to_grid(
#     dem_array: np.ndarray,
#     transform: Affine,
#     stations: List[RainStation],
#     power: float = 2.0,  # kept for compatibility, but Gaussian dominates
#     min_dist_cells: float = 1.0,  # retained (unused now but harmless)
#     rain_radius_km: float = 10.0,
#     use_wind: bool = True,
#     event_duration_hours: float = 1.0,
#     advection_level: str = "moderate",  # "low" | "moderate" | "high"
#     intensity_radius_scaling: bool = True,  # shrink/expand radius with rain_mm
# ) -> np.ndarray:
#     """
#     Wind-aware Gaussian rainfall interpolation.

#     For each station:
#       - Build a Gaussian kernel in (x,y) with:
#           * Footprint ~ rain_radius_km (elliptical under wind)
#           * Center shifted downwind by an amount based on wind & advection_level
#       - Weight decays smoothly with dimensionless radius rho.

#     We then accumulate:  rain_mm * w  / sum(w)
#     """

#     rows, cols = dem_array.shape
#     lat_grid, lon_grid = _latlon_grid(transform, rows, cols)

#     # accumulators
#     rain_sum = np.zeros((rows, cols), dtype="float32")
#     weight_sum = np.zeros((rows, cols), dtype="float32")

#     for st in stations:
#         if st.rain_mm <= 0:
#             continue

#         # Distances from this station to every grid cell
#         dx_km, dy_km, dist_km = _distance_km(st.lat, st.lon, lat_grid, lon_grid)

#         # Pre-mask to keep compute in a reasonable neighborhood
#         # (2x radius gives room for tail, but doesn't blow up computation too much)
#         initial_mask = dist_km <= (rain_radius_km * 2.0)
#         if not np.any(initial_mask):
#             continue

#         # Compute dimensionless radius rho
#         rho = _effective_rho_gaussian(
#             dx_km=dx_km,
#             dy_km=dy_km,
#             dist_km=dist_km,
#             rain_radius_km=rain_radius_km,
#             rain_mm=st.rain_mm,
#             wind_speed_ms=st.wind_speed_ms,
#             wind_dir_deg_from=st.wind_dir_deg,
#             event_duration_hours=event_duration_hours,
#             advection_level=advection_level,
#             use_wind=use_wind,
#             intensity_radius_scaling=intensity_radius_scaling,
#         )

#         # Final mask based on rho (core + tail)
#         mask = initial_mask & (rho <= 1.5)
#         if not np.any(mask):
#             continue

#         # Gaussian weight: compact, smooth
#         # rho=0 → 1 ; rho=1 → ~0.22 ; rho=1.5 → ~0.01
#         w = np.zeros_like(rho, dtype="float32")
#         w[mask] = np.exp(-(rho[mask] ** 2) * 1.5)

#         rain_sum[mask] += st.rain_mm * w[mask]
#         weight_sum[mask] += w[mask]

#     result = np.zeros_like(rain_sum, dtype="float32")
#     valid = weight_sum > 0
#     result[valid] = rain_sum[valid] / weight_sum[valid]

#     return result


# from __future__ import annotations

# from dataclasses import dataclass
# from typing import List

# import numpy as np
# from rasterio.transform import Affine


# @dataclass
# class RainStation:
#     lat: float
#     lon: float
#     rain_mm: float  # event rainfall or accumulated rainfall
#     wind_speed_ms: float | None = None  # e.g. ws
#     wind_dir_deg: float | None = None  # e.g. wd (degrees FROM which wind blows)


# def _latlon_grid(
#     transform: Affine, rows: int, cols: int
# ) -> tuple[np.ndarray, np.ndarray]:
#     """
#     Build 2D arrays of lat, lon for each cell center.
#     """
#     r_idx, c_idx = np.indices((rows, cols))

#     # For north-up geotiffs: a>0 (lon step), e<0 (lat step), b=d=0
#     lon = transform.c + (c_idx + 0.5) * transform.a
#     lat = transform.f + (r_idx + 0.5) * transform.e

#     return lat.astype("float32"), lon.astype("float32")


# def _distance_km(
#     lat0: float, lon0: float, lat: np.ndarray, lon: np.ndarray
# ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
#     """
#     Approximate dx, dy and great-circle distance in km from (lat0, lon0)
#     to each grid point.
#     """
#     R_earth = 6371.0  # km

#     lat0_rad = np.deg2rad(lat0)
#     lat_rad = np.deg2rad(lat)

#     dlat = lat_rad - lat0_rad
#     dlon = np.deg2rad(lon - lon0)

#     # equirectangular projection
#     x = dlon * np.cos((lat_rad + lat0_rad) / 2.0)
#     y = dlat

#     dx_km = R_earth * x  # east-west
#     dy_km = R_earth * y  # north-south

#     dist_km = np.sqrt(dx_km**2 + dy_km**2)
#     return dx_km, dy_km, dist_km


# def _apply_wind_anisotropy(
#     dx_km: np.ndarray,
#     dy_km: np.ndarray,
#     rain_radius_km: float,
#     wind_speed_ms: float,
#     wind_dir_deg_from: float,
#     k: float = 0.10,  # tunable (0.05–0.15 recommended)
# ) -> np.ndarray:
#     """
#     Return dimensionless elliptical distance ρ for each cell.

#     ρ <= 1 → inside rainfall footprint
#     ρ > 1 → outside

#     This is meteorologically sane:
#     - Downwind radius increases with wind speed
#     - Upwind radius decreases (but mildly)
#     - Crosswind radius unchanged
#     - Total footprint stays approximately same scale as rain_radius_km
#     """

#     # ----------------------------
#     # 1. Convert to wind-aligned frame
#     # ----------------------------
#     # Wind direction is FROM; convert to TO (downwind)
#     downwind_deg = (wind_dir_deg_from + 180.0) % 360.0
#     theta = np.deg2rad(downwind_deg)

#     # rotation: x' = downwind axis, y' = crosswind axis
#     x_prime = dx_km * np.sin(theta) + dy_km * np.cos(theta)
#     y_prime = dx_km * np.cos(theta) - dy_km * np.sin(theta)

#     # ----------------------------
#     # 2. Define directional radii
#     # ----------------------------
#     ws = max(wind_speed_ms, 0.0)

#     R0 = rain_radius_km

#     # Downwind radius longer
#     R_down = R0 * (1 + k * ws)

#     # Upwind shorter but not drastically
#     R_up = R0 * (1 - 0.5 * k * ws)
#     R_up = max(R_up, 0.5 * R0)  # clamp (never collapse too much)

#     # Crosswind radius mildly unchanged
#     R_cross = R0

#     # ----------------------------
#     # 3. Compute dimensionless radius ρ
#     # ----------------------------
#     rho = np.zeros_like(x_prime, dtype="float32")

#     # downwind
#     mask_down = x_prime >= 0
#     rho[mask_down] = (x_prime[mask_down] / R_down) ** 2 + (
#         y_prime[mask_down] / R_cross
#     ) ** 2

#     # upwind
#     mask_up = ~mask_down
#     rho[mask_up] = (x_prime[mask_up] / R_up) ** 2 + (y_prime[mask_up] / R_cross) ** 2

#     # ρ as "effective radius"
#     rho = np.sqrt(rho)

#     return rho


# # def _apply_wind_anisotropy(
# #     dx_km: np.ndarray,
# #     dy_km: np.ndarray,
# #     dist_km: np.ndarray,
# #     wind_speed_ms: float,
# #     wind_dir_deg_from: float,
# #     anisotropy_factor: float = 0.15,
# # ) -> np.ndarray:
# #     """
# #     Adjust distances using wind direction/speed without changing the
# #     *outer radius* of influence; we only change relative weights.

# #     - bearing: direction station -> cell (0 = north, pi/2 = east)
# #     - wind_dir_deg_from: direction FROM which wind blows (met convention)
# #     - downwind direction = from + 180°
# #     - scale = 1 + k * ws * cos(delta)
# #       * cos(delta)=1  → fully downwind → smaller effective distance
# #       * cos(delta)=-1 → fully upwind   → larger effective distance
# #     """
# #     # Bearing from station to cell
# #     bearing = np.arctan2(dx_km, dy_km)

# #     # Convert "from" direction to "to" (downwind)
# #     downwind_deg = (wind_dir_deg_from + 180.0) % 360.0
# #     downwind_rad = np.deg2rad(downwind_deg)

# #     delta = bearing - downwind_rad
# #     cos_delta = np.cos(delta)

# #     # scale factor (clipped so we don't get crazy ellipses)
# #     scale = 1.0 + anisotropy_factor * wind_speed_ms * cos_delta
# #     scale = np.clip(scale, 0.5, 2.0)

# #     # Effective distance INSIDE radius_km
# #     return dist_km / scale


# def interpolate_rainfall_to_grid(
#     dem_array: np.ndarray,
#     transform: Affine,
#     stations: List[RainStation],
#     power: float = 2.0,
#     min_dist_cells: float = 1.0,
#     rain_radius_km: float = 10.0,
#     use_wind: bool = True,
# ) -> np.ndarray:
#     """
#     Wind-aware inverse-distance rainfall interpolation using a
#     meteorologically realistic elliptical kernel.

#     This version replaces all previous anisotropy logic and ensures:

#     - radius remains exactly rain_radius_km
#     - footprints become smooth ellipses aligned with the wind
#     - downwind extension grows with wind speed
#     - upwind radius shrinks mildly
#     - crosswind unchanged
#     """

#     rows, cols = dem_array.shape
#     lat_grid, lon_grid = _latlon_grid(transform, rows, cols)

#     # approximate km per cell vertically
#     cell_dlat = abs(transform.e)
#     cell_km = 111.0 * cell_dlat if cell_dlat > 0 else 1.0
#     min_dist_km = max(min_dist_cells * cell_km, 0.01)

#     rain_sum = np.zeros((rows, cols), dtype="float32")
#     weight_sum = np.zeros((rows, cols), dtype="float32")

#     for st in stations:
#         if st.rain_mm <= 0:
#             continue

#         # -------------------------
#         # 1) Distance grid
#         # -------------------------
#         dx_km, dy_km, dist_km = _distance_km(st.lat, st.lon, lat_grid, lon_grid)

#         # -------------------------
#         # 2) Compute ρ = elliptical normalized distance
#         # -------------------------
#         if use_wind and st.wind_speed_ms is not None and st.wind_dir_deg is not None:
#             rho = _apply_wind_anisotropy(
#                 dx_km=dx_km,
#                 dy_km=dy_km,
#                 rain_radius_km=rain_radius_km,
#                 wind_speed_ms=st.wind_speed_ms,
#                 wind_dir_deg_from=st.wind_dir_deg,
#                 k=0.10,  # tunable anisotropy strength
#             )
#         else:
#             # isotropic fallback
#             rho = dist_km / rain_radius_km

#         # ------------------------------------------
#         # 3) Footprint mask (ellipse instead of circle)
#         # ------------------------------------------
#         mask = rho <= 1.0
#         if not np.any(mask):
#             continue

#         # effective IDW distance:
#         #   convert ρ back to km for weighting
#         d_eff = np.maximum(rho * rain_radius_km, min_dist_km)

#         # -------------------------
#         # 4) Weight computation
#         # -------------------------
#         w = np.zeros_like(d_eff, dtype="float32")
#         w[mask] = 1.0 / (d_eff[mask] ** power)

#         rain_sum[mask] += st.rain_mm * w[mask]
#         weight_sum[mask] += w[mask]

#     # -------------------------
#     # 5) Final grid
#     # -------------------------
#     result = np.zeros_like(rain_sum, dtype="float32")
#     valid = weight_sum > 0
#     result[valid] = rain_sum[valid] / weight_sum[valid]

#     return result
#     # def interpolate_rainfall_to_grid(
#     #     dem_array: np.ndarray,
#     #     transform: Affine,
#     #     stations: List[RainStation],
#     #     power: float = 2.0,
#     #     min_dist_cells: float = 1.0,
#     #     rain_radius_km: float = 10.0,
#     #     use_wind: bool = True,
#     # ) -> np.ndarray:
#     #     """
#     #     Inverse-distance weighted rainfall interpolation with optional
#     #     wind-aware anisotropy.

#     #     IMPORTANT:
#     #     - Radius of influence is ALWAYS rain_radius_km (no expansion).
#     #     - Wind only modifies weights inside that radius (downwind heavier,
#     #       upwind lighter).
#     #     """

#     #     rows, cols = dem_array.shape
#     #     lat_grid, lon_grid = _latlon_grid(transform, rows, cols)

#     #     # Approximate km per cell (for min_dist_km)
#     #     cell_dlat = abs(transform.e)
#     #     cell_km = 111.0 * cell_dlat if cell_dlat > 0 else 1.0
#     #     min_dist_km = max(min_dist_cells * cell_km, 0.01)

#     #     rain_sum = np.zeros((rows, cols), dtype="float32")
#     #     weight_sum = np.zeros((rows, cols), dtype="float32")

#     #     for st in stations:
#     #         if st.rain_mm <= 0:
#     #             continue

#     #         dx_km, dy_km, dist_km = _distance_km(st.lat, st.lon, lat_grid, lon_grid)

#     #         # Apply wind anisotropy (no initial mask!)
#     #         if use_wind and st.wind_speed_ms is not None and st.wind_dir_deg is not None:
#     #             dist_eff_km = _apply_wind_anisotropy(
#     #                 dx_km=dx_km,
#     #                 dy_km=dy_km,
#     #                 rain_radius_km=rain_radius_km,
#     #                 # dist_km=dist_km,
#     #                 wind_speed_ms=st.wind_speed_ms,
#     #                 wind_dir_deg_from=st.wind_dir_deg,
#     #                 k=0.10,  # tunable (0.05–0.15 recommended)
#     #                 # anisotropy_factor=0.25,  # try 0.20–0.35
#     #             )
#     #         else:
#     #             dist_eff_km = dist_km

#     #         # THIS is the new single mask
#     #         mask = dist_eff_km <= rain_radius_km
#     #         if not np.any(mask):
#     #             continue

#     #         # Prevent singularities
#     #         dist_eff_km = np.maximum(dist_eff_km, min_dist_km)

#     #         # IDW weighting
#     #         w = np.zeros_like(dist_eff_km, dtype="float32")
#     #         w[mask] = 1.0 / (dist_eff_km[mask] ** power)

#     #         rain_sum[mask] += st.rain_mm * w[mask]
#     #         weight_sum[mask] += w[mask]
#     # for st in stations:
#     #     if st.rain_mm <= 0:
#     #         continue

#     #     dx_km, dy_km, dist_km = _distance_km(st.lat, st.lon, lat_grid, lon_grid)

#     #     # HARD radius cap: do not allow influence beyond rain_radius_km
#     #     mask = dist_km <= rain_radius_km
#     #     if not np.any(mask):
#     #         continue

#     #     dist_eff = dist_km.copy()

#     #     if use_wind and st.wind_speed_ms is not None and st.wind_dir_deg is not None:
#     #         dist_eff = _apply_wind_anisotropy(
#     #             dx_km=dx_km,
#     #             dy_km=dy_km,
#     #             dist_km=dist_km,
#     #             wind_speed_ms=st.wind_speed_ms,
#     #             wind_dir_deg_from=st.wind_dir_deg,
#     #             anisotropy_factor=0.15,  # tune for “how elongated”
#     #         )

#     #     # NOTE: we still respect the outer radius based on *original* dist_km
#     #     # dist_eff only affects weights inside that radius
#     #     dist_eff = np.maximum(dist_eff, min_dist_km)

#     #     w = np.zeros_like(dist_eff, dtype="float32")
#     #     w[mask] = 1.0 / (dist_eff[mask] ** power)

#     #     rain_sum[mask] += st.rain_mm * w[mask]
#     #     weight_sum[mask] += w[mask]

#     result = np.zeros_like(rain_sum, dtype="float32")
#     valid = weight_sum > 0
#     result[valid] = rain_sum[valid] / weight_sum[valid]

#     return result


# # # terrain/rain_interpolation.py

# # from __future__ import annotations

# # from dataclasses import dataclass
# # from typing import List

# # import numpy as np
# # from rasterio.transform import Affine


# # @dataclass
# # class RainStation:
# #     lat: float
# #     lon: float
# #     rain_mm: float  # event rainfall or accumulated rainfall
# #     wind_speed_ms: float | None = None  # e.g. ws
# #     wind_dir_deg: float | None = None  # e.g. wd (degrees FROM which wind blows)


# # # terrain/rain_interpolation.py (continued)


# # def _latlon_grid(
# #     transform: Affine, rows: int, cols: int
# # ) -> tuple[np.ndarray, np.ndarray]:
# #     """
# #     Build 2D arrays of lat, lon for each cell center.
# #     """
# #     # row, col indices
# #     r_idx, c_idx = np.indices((rows, cols))

# #     # cell centers
# #     lon = transform.c + (c_idx + 0.5) * transform.a + (r_idx + 0.5) * transform.b
# #     lat = transform.f + (c_idx + 0.5) * transform.d + (r_idx + 0.5) * transform.e

# #     return lat.astype("float32"), lon.astype("float32")


# # def _distance_km(
# #     lat0: float, lon0: float, lat: np.ndarray, lon: np.ndarray
# # ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
# #     """
# #     Approximate dx, dy and great-circle distance in km from (lat0, lon0)
# #     to each grid point.
# #     """
# #     # simple equirectangular approximation is good enough at country scales
# #     R_earth = 6371.0  # km

# #     lat0_rad = np.deg2rad(lat0)
# #     lat_rad = np.deg2rad(lat)

# #     dlat = lat_rad - lat0_rad
# #     dlon = np.deg2rad(lon - lon0)

# #     # equirectangular projection
# #     x = dlon * np.cos((lat_rad + lat0_rad) / 2.0)
# #     y = dlat

# #     dx_km = R_earth * x
# #     dy_km = R_earth * y

# #     dist_km = np.sqrt(dx_km**2 + dy_km**2)
# #     return dx_km, dy_km, dist_km


# # def _apply_wind_anisotropy(
# #     dx_km: np.ndarray,
# #     dy_km: np.ndarray,
# #     dist_km: np.ndarray,
# #     wind_speed_ms: float,
# #     wind_dir_deg_from: float,
# #     anisotropy_factor: float = 0.25,
# # ) -> np.ndarray:
# #     bearing = np.arctan2(dx_km, dy_km)

# #     downwind_rad = np.deg2rad((wind_dir_deg_from + 180) % 360)

# #     delta = bearing - downwind_rad
# #     cos_delta = np.cos(delta)

# #     scale = 1.0 + anisotropy_factor * wind_speed_ms * cos_delta
# #     scale = np.clip(scale, 0.1, 3.0)

# #     return dist_km / scale


# # def interpolate_rainfall_to_grid(
# #     dem_array: np.ndarray,
# #     transform: Affine,
# #     stations: List[RainStation],
# #     min_dist_cells: float = 1.0,
# #     rain_radius_km: float = 10.0,
# #     use_wind: bool = True,
# # ) -> np.ndarray:
# #     """
# #     Rainfall interpolation with optional wind-aware anisotropy.

# #     Changes from the pure IDW version:
# #       - Uses a Gaussian-like decay: w = exp(-(dist_eff / radius)^2)
# #       - Wind shrinks/extends distances along the downwind axis
# #       - No hard circular cutoff after anisotropy → visible ellipses/plumes
# #     """

# #     rows, cols = dem_array.shape
# #     lat_grid, lon_grid = _latlon_grid(transform, rows, cols)

# #     # approximate km per cell in latitude for min_dist
# #     cell_dlat = np.abs(transform.e)
# #     cell_km = 111.0 * cell_dlat if cell_dlat > 0 else 1.0
# #     min_dist_km = max(min_dist_cells * cell_km, 0.01)

# #     rain_sum = np.zeros((rows, cols), dtype="float32")
# #     weight_sum = np.zeros((rows, cols), dtype="float32")

# #     for st in stations:
# #         if st.rain_mm <= 0:
# #             continue

# #         dx_km, dy_km, dist_km = _distance_km(st.lat, st.lon, lat_grid, lon_grid)

# #         # Broad pre-mask so we don't compute on entire country
# #         initial_mask = dist_km <= (rain_radius_km * 3.0)
# #         if not np.any(initial_mask):
# #             continue

# #         # Wind anisotropy: make this strong enough to matter
# #         if use_wind and st.wind_speed_ms is not None and st.wind_dir_deg is not None:
# #             dist_eff_km = _apply_wind_anisotropy(
# #                 dx_km=dx_km,
# #                 dy_km=dy_km,
# #                 dist_km=dist_km,
# #                 wind_speed_ms=st.wind_speed_ms,
# #                 wind_dir_deg_from=st.wind_dir_deg,
# #                 anisotropy_factor=0.12,
# #             )
# #         else:
# #             dist_eff_km = dist_km

# #         # STRICT mask: only accept pixels inside *both* radii
# #         mask = (dist_eff_km <= rain_radius_km) & (dist_km <= rain_radius_km)

# #         if not np.any(mask):
# #             continue

# #         # Avoid singularities
# #         dist_eff_km = np.maximum(dist_eff_km, min_dist_km)

# #         # --- HERE is the new weight function ---
# #         # Gaussian-like decay: strong in center, smooth taper with distance
# #         w = np.zeros_like(dist_eff_km, dtype="float32")
# #         # radius controls how quickly it falls off; you can try 0.5 * radius if you want sharper blobs
# #         w[mask] = np.exp(-((dist_eff_km[mask] / rain_radius_km) ** 2))

# #         rain_sum[mask] += st.rain_mm * w[mask]
# #         weight_sum[mask] += w[mask]

# #     result = np.zeros_like(rain_sum, dtype="float32")
# #     valid = weight_sum > 0
# #     result[valid] = rain_sum[valid] / weight_sum[valid]

# #     return result


# # # def interpolate_rainfall_to_grid(
# # #     dem_array: np.ndarray,
# # #     transform: Affine,
# # #     stations: List[RainStation],
# # #     power: float = 2.0,
# # #     min_dist_cells: float = 1.0,
# # #     rain_radius_km: float = 10.0,
# # #     use_wind: bool = True,
# # # ) -> np.ndarray:
# # #     """
# # #     Inverse-distance weighted rainfall interpolation with optional
# # #     wind-aware anisotropy.

# # #     - When station.wind_speed_ms / wind_dir_deg are None:
# # #         behaves exactly like isotropic circular radius_km kernel.
# # #     - When provided:
# # #         shrinks distances downwind, stretches upwind → elliptical footprint.
# # #     """

# # #     rows, cols = dem_array.shape
# # #     lat_grid, lon_grid = _latlon_grid(transform, rows, cols)

# # #     # approximate km per cell in latitude for min_dist
# # #     # (not perfect near poles, but Timor etc is fine)
# # #     cell_dlat = np.abs(transform.e)
# # #     cell_km = 111.0 * cell_dlat if cell_dlat > 0 else 1.0
# # #     min_dist_km = max(min_dist_cells * cell_km, 0.01)

# # #     # accumulators
# # #     rain_sum = np.zeros((rows, cols), dtype="float32")
# # #     weight_sum = np.zeros((rows, cols), dtype="float32")

# # #     for st in stations:
# # #         if st.rain_mm <= 0:
# # #             continue

# # #         # distances from this station to every grid cell
# # #         dx_km, dy_km, dist_km = _distance_km(st.lat, st.lon, lat_grid, lon_grid)

# # #         # initial radius mask
# # #         # mask = dist_km <= rain_radius_km
# # #         # allow wind to expand or shrink radius
# # #         # so start with full grid (or a larger pre-mask)
# # #         # mask = dist_km <= rain_radius_km  # recommended
# # #         initial_mask = dist_km <= (rain_radius_km * 2.0)
# # #         if not np.any(initial_mask):
# # #             continue

# # #         # apply wind anisotropy if we have wind and it's enabled
# # #         if use_wind and st.wind_speed_ms is not None and st.wind_dir_deg is not None:
# # #             dist_eff_km = _apply_wind_anisotropy(
# # #                 dx_km=dx_km,
# # #                 dy_km=dy_km,
# # #                 dist_km=dist_km,
# # #                 wind_speed_ms=st.wind_speed_ms,
# # #                 wind_dir_deg_from=st.wind_dir_deg,
# # #                 anisotropy_factor=0.08,  # tunable
# # #             )
# # #         else:
# # #             dist_eff_km = dist_km

# # #         # updated radius mask with effective distance
# # #         mask = initial_mask & (dist_eff_km <= rain_radius_km)
# # #         if not np.any(mask):
# # #             continue

# # #         # avoid singularities near station
# # #         dist_eff_km = np.maximum(dist_eff_km, min_dist_km)

# # #         w = np.zeros_like(dist_eff_km, dtype="float32")
# # #         w[mask] = 1.0 / (dist_eff_km[mask] ** power)

# # #         rain_sum[mask] += st.rain_mm * w[mask]
# # #         weight_sum[mask] += w[mask]

# # #     result = np.zeros_like(rain_sum, dtype="float32")
# # #     valid = weight_sum > 0
# # #     result[valid] = rain_sum[valid] / weight_sum[valid]

# # #     return result
# # #


# # # def _apply_wind_anisotropy(
# # #     dx_km: np.ndarray,
# # #     dy_km: np.ndarray,
# # #     base_dist_km: np.ndarray,
# # #     wind_speed_ms: float,
# # #     wind_dir_deg_from: float,
# # #     anisotropy_factor: float = 0.08,
# # # ) -> np.ndarray:
# # #     """
# # #     Adjust distances using wind direction/speed.

# # #     Idea:
# # #       - Compute bearing from station -> cell
# # #       - Compute 'downwind' direction = wind_dir + 180°
# # #       - Use cosine between them to shrink distance downwind and
# # #         enlarge distance upwind.

# # #     r_eff = r / scale
# # #     scale = 1 + k * ws * cos(delta)
# # #     clamped to [0.3, 2.0] to avoid degeneracy.
# # #     """

# # #     # Bearing from station to cell (0 = north, pi/2 = east)
# # #     bearing = np.arctan2(dx_km, dy_km)  # x = east, y = north

# # #     # Wind is given as "from" direction; we want direction "to" (downwind)
# # #     downwind_deg = (wind_dir_deg_from + 180.0) % 360.0
# # #     downwind_rad = np.deg2rad(downwind_deg)

# # #     delta = bearing - downwind_rad
# # #     cos_delta = np.cos(delta)

# # #     # scale factor
# # #     scale = 1.0 + anisotropy_factor * wind_speed_ms * cos_delta
# # #     # clamp to reasonable range
# # #     scale = np.clip(scale, 0.3, 2.0)

# # #     # Effective distance
# # #     r_eff = base_dist_km / scale
# # #     return r_eff
# # # def latlon_to_rowcol(transform: Affine, lat: float, lon: float) -> Tuple[int, int]:
# # #     """
# # #     Convert (lat, lon) in EPSG:4326 to (row, col) given an Affine transform.

# # #     For north-up rasters:
# # #       col = (lon - c) / a
# # #       row = (f - lat) / |e|
# # #     """
# # #     a = transform.a
# # #     c = transform.c
# # #     e = transform.e
# # #     f = transform.f

# # #     col = (lon - c) / a
# # #     row = (f - lat) / abs(e)

# # #     return int(round(row)), int(round(col))


# # # def interpolate_rainfall_to_grid(
# # #     dem_array: np.ndarray,
# # #     transform: Affine,
# # #     stations: List[RainStation],
# # #     power: float = 2.0,
# # #     min_dist_cells: float = 1.0,
# # #     rain_radius_km: float = 10.0,
# # # ) -> np.ndarray:
# # #     """
# # #     Radius-limited IDW interpolation of station rainfall onto the DEM grid.

# # #     Distances are measured in grid cells, but the radius is provided in km.
# # #     We approximate km→cells using the lat cell size and ~111.32 km per degree.
# # #     """
# # #     rows, cols = dem_array.shape
# # #     rain = np.zeros((rows, cols), dtype="float32")

# # #     if not stations:
# # #         return rain

# # #     # Approximate cell size in km using latitude spacing
# # #     cellsize_deg_lat = abs(transform.e) if transform.e != 0 else abs(transform.a)
# # #     # 1 degree lat ~ 111.32 km
# # #     cellsize_km = cellsize_deg_lat * 111.32
# # #     if cellsize_km <= 0:
# # #         cellsize_km = 1.0  # safety

# # #     radius_cells = max(rain_radius_km / cellsize_km, 1.0)
# # #     max_radius_cells2 = float(radius_cells * radius_cells)

# # #     # Map stations to grid coordinates
# # #     st_pix = []
# # #     st_val = []
# # #     for st in stations:
# # #         try:
# # #             r, c = latlon_to_rowcol(transform, st.lat, st.lon)
# # #             if 0 <= r < rows and 0 <= c < cols:
# # #                 st_pix.append((r, c))
# # #                 st_val.append(st.rain_mm)
# # #         except Exception:
# # #             # station outside DEM or transform issues -> ignore
# # #             continue

# # #     if not st_pix:
# # #         return rain

# # #     st_pix = np.array(st_pix, dtype="int32")
# # #     st_val = np.array(st_val, dtype="float32")

# # #     rr = np.arange(rows, dtype="int32")[:, None]  # (rows, 1)
# # #     cc = np.arange(cols, dtype="int32")[None, :]  # (1, cols)

# # #     num = np.zeros_like(rain)
# # #     den = np.zeros_like(rain)
# # #     min_d2 = float(min_dist_cells * min_dist_cells)

# # #     for (sr, sc), val in zip(st_pix, st_val):
# # #         dr = rr - sr
# # #         dc = cc - sc
# # #         dist2 = (dr * dr + dc * dc).astype("float32")

# # #         # Mask out cells beyond the influence radius
# # #         mask_far = dist2 > max_radius_cells2
# # #         dist2[mask_far] = np.nan

# # #         # Enforce minimum distance to avoid blowups
# # #         dist2[dist2 < min_d2] = min_d2

# # #         w = 1.0 / (dist2 ** (power / 2.0))
# # #         w = np.nan_to_num(w, nan=0.0)

# # #         num += w * val
# # #         den += w

# # #         # at the end of interpolate_rainfall_to_grid
# # #     with np.errstate(invalid="ignore", divide="ignore"):
# # #         rain = np.where(den > 0, num / den, 0.0).astype("float32")
# # #     return rain


# # # # terrain/rain_interpolation.py

# # # from __future__ import annotations

# # # from dataclasses import dataclass
# # # from typing import List, Tuple

# # # import numpy as np
# # # from rasterio.transform import Affine


# # # @dataclass
# # # class RainStation:
# # #     lat: float
# # #     lon: float
# # #     rain_mm: float  # event rainfall or accumulated rainfall


# # # def latlon_to_rowcol(transform: Affine, lat: float, lon: float) -> Tuple[int, int]:
# # #     """
# # #     Convert (lat, lon) in EPSG:4326 to (row, col) given an Affine transform.

# # #     For north-up rasters:
# # #       col = (lon - c) / a
# # #       row = (f - lat) / |e|
# # #     """
# # #     a = transform.a
# # #     c = transform.c
# # #     e = transform.e
# # #     f = transform.f

# # #     col = (lon - c) / a
# # #     row = (f - lat) / abs(e)

# # #     return int(round(row)), int(round(col))


# # # def interpolate_rainfall_to_grid(
# # #     dem_array: np.ndarray,
# # #     transform: Affine,
# # #     stations: List[RainStation],
# # #     power: float = 2.0,
# # #     min_dist_cells: float = 1.0,
# # #     max_radius_cells: float | None = None,
# # # ) -> np.ndarray:
# # #     """
# # #     Simple IDW interpolation of station rainfall onto the DEM grid.

# # #     Distances are measured in *grid cells* (row/col space). For Timor-sized
# # #     domains this is a reasonable first approximation.

# # #     Returns:
# # #         2D np.ndarray[float32] of shape dem_array.shape.
# # #     """
# # #     rows, cols = dem_array.shape
# # #     rain = np.zeros((rows, cols), dtype="float32")

# # #     if not stations:
# # #         return rain

# # #     # Map stations to grid coordinates
# # #     st_pix = []
# # #     st_val = []
# # #     for st in stations:
# # #         try:
# # #             r, c = latlon_to_rowcol(transform, st.lat, st.lon)
# # #             if 0 <= r < rows and 0 <= c < cols:
# # #                 st_pix.append((r, c))
# # #                 st_val.append(st.rain_mm)
# # #         except Exception:
# # #             # station outside DEM or transform issues -> ignore
# # #             continue

# # #     if not st_pix:
# # #         return rain

# # #     st_pix = np.array(st_pix, dtype="int32")
# # #     st_val = np.array(st_val, dtype="float32")

# # #     rr = np.arange(rows, dtype="int32")[:, None]  # (rows, 1)
# # #     cc = np.arange(cols, dtype="int32")[None, :]  # (1, cols)

# # #     num = np.zeros_like(rain)
# # #     den = np.zeros_like(rain)

# # #     min_d2 = float(min_dist_cells**2)

# # #     for (sr, sc), val in zip(st_pix, st_val):
# # #         dr = rr - sr
# # #         dc = cc - sc
# # #         dist2 = (dr * dr + dc * dc).astype("float32")

# # #         if max_radius_cells is not None:
# # #             # mask out cells beyond max_radius
# # #             mask_far = dist2 > float(max_radius_cells**2)
# # #             dist2[mask_far] = np.nan

# # #         # enforce minimum distance to avoid blowups
# # #         dist2[dist2 < min_d2] = min_d2

# # #         w = 1.0 / (dist2 ** (power / 2.0))
# # #         w = np.nan_to_num(w, nan=0.0)

# # #         num += w * val
# # #         den += w

# # #     rain = np.where(den > 0, num / den, 0.0).astype("float32")
# # #     return rain


# # # # # terrain/rain_interpolation.py

# # # # from __future__ import annotations

# # # # from dataclasses import dataclass
# # # # from typing import List, Tuple

# # # # import numpy as np


# # # # @dataclass
# # # # class RainStation:
# # # #     lat: float
# # # #     lon: float
# # # #     rain_mm: float


# # # # # ==============================================================
# # # # # Helper: convert pixel center to lat/lon using affine transform
# # # # # ==============================================================


# # # # def _rowcol_to_latlon(
# # # #     transform, rows: np.ndarray, cols: np.ndarray
# # # # ) -> Tuple[np.ndarray, np.ndarray]:
# # # #     """
# # # #     Convert 2D arrays of (row, col) to lat/lon using affine transform from rasterio.

# # # #     transform = (a, b, c, d, e, f)
# # # #        x = a * col + b * row + c
# # # #        y = d * col + e * row + f

# # # #     For unrotated rasters (typical DEM/GeoTIFF), b = d = 0.
# # # #     c = lon_west, f = lat_north.

# # # #     Returns:
# # # #         lat (2D array), lon (2D array)
# # # #     """
# # # #     # Full affine (6 numbers)
# # # #     a = transform.a
# # # #     b = transform.b
# # # #     c = transform.c
# # # #     d = transform.d
# # # #     e = transform.e
# # # #     f = transform.f

# # # #     # Compute projected coordinates
# # # #     X = a * cols + b * rows + c
# # # #     Y = d * cols + e * rows + f

# # # #     # For WGS84 input rasters (deg), X=lon, Y=lat
# # # #     lon = X
# # # #     lat = Y

# # # #     return lat, lon


# # # # # ==============================================================
# # # # # Distance approximation in meters (fast equirectangular)
# # # # # ==============================================================

# # # # EARTH_RADIUS = 6371000.0  # meters


# # # # def _latlon_dist_m(lat1, lon1, lat2, lon2):
# # # #     """
# # # #     Fast equirectangular approximation for distance in meters.
# # # #     Accurate for small islands like Timor-Leste.
# # # #     """
# # # #     lat1_r = np.radians(lat1)
# # # #     lat2_r = np.radians(lat2)
# # # #     dlat = lat2_r - lat1_r
# # # #     dlon = np.radians(lon2 - lon1)

# # # #     x = dlon * np.cos((lat1_r + lat2_r) / 2.0)
# # # #     y = dlat
# # # #     return EARTH_RADIUS * np.sqrt(x * x + y * y)


# # # # # ==============================================================
# # # # # Main interpolation
# # # # # ==============================================================


# # # # def interpolate_rainfall_to_grid(
# # # #     dem_array: np.ndarray,
# # # #     transform,
# # # #     stations: List[RainStation],
# # # #     power: float = 2.0,
# # # #     min_dist_m: float = 100.0,
# # # #     max_radius_m: float | None = None,
# # # # ) -> np.ndarray:
# # # #     """
# # # #     IDW interpolation of rainfall onto the DEM grid.

# # # #     Parameters:
# # # #     - dem_array: 2D DEM array (shape defines grid)
# # # #     - transform: affine transform (from rasterio) defining pixel → lat/lon
# # # #     - stations: list of RainStation(lat, lon, rain_mm)
# # # #     - power: IDW exponent (2.0 = standard)
# # # #     - min_dist_m: minimum distance (avoid division by zero)
# # # #     - max_radius_m: optional max influence radius (e.g. 20000 meters)

# # # #     Returns:
# # # #         rain_mm grid (float32)
# # # #     """
# # # #     rows, cols = dem_array.shape
# # # #     rain = np.zeros((rows, cols), dtype="float32")

# # # #     if not stations:
# # # #         return rain

# # # #     # ---------------------------------------------------------------
# # # #     # Precompute grid lat/lon (2D arrays)
# # # #     # ---------------------------------------------------------------
# # # #     rr = np.arange(rows).reshape(rows, 1)
# # # #     cc = np.arange(cols).reshape(1, cols)

# # # #     grid_lat, grid_lon = _rowcol_to_latlon(transform, rr, cc)

# # # #     # ---------------------------------------------------------------
# # # #     # Precompute station coords
# # # #     # ---------------------------------------------------------------
# # # #     st_lats = np.array([s.lat for s in stations], dtype="float32")
# # # #     st_lons = np.array([s.lon for s in stations], dtype="float32")
# # # #     st_vals = np.array([s.rain_mm for s in stations], dtype="float32")

# # # #     # ---------------------------------------------------------------
# # # #     # IDW accumulation
# # # #     # ---------------------------------------------------------------
# # # #     num = np.zeros_like(rain)
# # # #     den = np.zeros_like(rain)

# # # #     for lat_s, lon_s, val in zip(st_lats, st_lons, st_vals):
# # # #         # compute distance (meters)
# # # #         dist = _latlon_dist_m(grid_lat, grid_lon, lat_s, lon_s)

# # # #         # apply min distance threshold
# # # #         dist = np.maximum(dist, min_dist_m)

# # # #         # optional influence radius
# # # #         if max_radius_m is not None:
# # # #             mask = dist <= max_radius_m
# # # #         else:
# # # #             mask = np.ones_like(dist, dtype=bool)

# # # #         w = np.where(mask, 1.0 / (dist**power), 0.0)

# # # #         num += w * val
# # # #         den += w

# # # #     # ---------------------------------------------------------------
# # # #     # Final rainfall field
# # # #     # ---------------------------------------------------------------
# # # #     rain = np.where(den > 0, num / den, 0.0).astype("float32")
# # # #     return rain


# # # # # terrain/rain_interpolation.py

# # # # from __future__ import annotations

# # # # from dataclasses import dataclass
# # # # from typing import List

# # # # import numpy as np

# # # # from .srtm_hgt import DemMosaic


# # # # @dataclass
# # # # class RainStation:
# # # #     lat: float
# # # #     lon: float
# # # #     rain_mm: float  # event rainfall or accumulated rainfall


# # # # def rain_raster_from_stations(
# # # #     mosaic: DemMosaic,
# # # #     stations: List[RainStation],
# # # #     power: float = 2.0,
# # # #     min_dist: float = 1e-3,
# # # # ) -> np.ndarray:
# # # #     """
# # # #     Very simple IDW interpolation of station rainfall onto the DEM grid.

# # # #     NOTE: This is O(N_cells * N_stations). For Timor-sized grids and
# # # #     tens of stations it's probably fine as a first implementation, but
# # # #     you may want to optimize or tile later.
# # # #     """
# # # #     rows, cols = mosaic.data.shape
# # # #     rain = np.zeros((rows, cols), dtype="float32")

# # # #     if not stations:
# # # #         return rain

# # # #     # Precompute station indices (in DEM row/col space)
# # # #     st_rc = []
# # # #     st_val = []
# # # #     for st in stations:
# # # #         try:
# # # #             r, c = mosaic.latlon_to_rowcol(st.lat, st.lon)
# # # #             st_rc.append((r, c))
# # # #             st_val.append(st.rain_mm)
# # # #         except ValueError:
# # # #             # station outside DEM → ignore
# # # #             continue

# # # #     if not st_rc:
# # # #         return rain

# # # #     st_rc = np.array(st_rc, dtype="int32")
# # # #     st_val = np.array(st_val, dtype="float32")

# # # #     rr = np.arange(rows, dtype="int32")[:, None]  # shape (rows, 1)
# # # #     cc = np.arange(cols, dtype="int32")[None, :]  # shape (1, cols)

# # # #     # For now: loop over stations, accumulate weights
# # # #     # (you can vectorize further later if needed)
# # # #     num = np.zeros_like(rain)
# # # #     den = np.zeros_like(rain)

# # # #     for (sr, sc), val in zip(st_rc, st_val):
# # # #         dr = rr - sr
# # # #         dc = cc - sc
# # # #         dist2 = dr * dr + dc * dc
# # # #         dist2 = dist2.astype("float32")
# # # #         dist2[dist2 < min_dist * min_dist] = min_dist * min_dist

# # # #         w = 1.0 / (dist2 ** (power / 2.0))
# # # #         num += w * val
# # # #         den += w

# # # #     rain = np.where(den > 0, num / den, 0.0).astype("float32")
# # # #     return rain
