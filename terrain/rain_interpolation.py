# terrain/rain_interpolation.py

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

import numpy as np
from rasterio.transform import Affine


@dataclass
class RainStation:
    lat: float
    lon: float
    rain_mm: float  # event rainfall or accumulated rainfall


def latlon_to_rowcol(transform: Affine, lat: float, lon: float) -> Tuple[int, int]:
    """
    Convert (lat, lon) in EPSG:4326 to (row, col) given an Affine transform.

    For north-up rasters:
      col = (lon - c) / a
      row = (f - lat) / |e|
    """
    a = transform.a
    c = transform.c
    e = transform.e
    f = transform.f

    col = (lon - c) / a
    row = (f - lat) / abs(e)

    return int(round(row)), int(round(col))


def interpolate_rainfall_to_grid(
    dem_array: np.ndarray,
    transform: Affine,
    stations: List[RainStation],
    power: float = 2.0,
    min_dist_cells: float = 1.0,
    rain_radius_km: float = 10.0,
) -> np.ndarray:
    """
    Radius-limited IDW interpolation of station rainfall onto the DEM grid.

    Distances are measured in grid cells, but the radius is provided in km.
    We approximate km→cells using the lat cell size and ~111.32 km per degree.
    """
    rows, cols = dem_array.shape
    rain = np.zeros((rows, cols), dtype="float32")

    if not stations:
        return rain

    # Approximate cell size in km using latitude spacing
    cellsize_deg_lat = abs(transform.e) if transform.e != 0 else abs(transform.a)
    # 1 degree lat ~ 111.32 km
    cellsize_km = cellsize_deg_lat * 111.32
    if cellsize_km <= 0:
        cellsize_km = 1.0  # safety

    radius_cells = max(rain_radius_km / cellsize_km, 1.0)
    max_radius_cells2 = float(radius_cells * radius_cells)

    # Map stations to grid coordinates
    st_pix = []
    st_val = []
    for st in stations:
        try:
            r, c = latlon_to_rowcol(transform, st.lat, st.lon)
            if 0 <= r < rows and 0 <= c < cols:
                st_pix.append((r, c))
                st_val.append(st.rain_mm)
        except Exception:
            # station outside DEM or transform issues -> ignore
            continue

    if not st_pix:
        return rain

    st_pix = np.array(st_pix, dtype="int32")
    st_val = np.array(st_val, dtype="float32")

    rr = np.arange(rows, dtype="int32")[:, None]  # (rows, 1)
    cc = np.arange(cols, dtype="int32")[None, :]  # (1, cols)

    num = np.zeros_like(rain)
    den = np.zeros_like(rain)
    min_d2 = float(min_dist_cells * min_dist_cells)

    for (sr, sc), val in zip(st_pix, st_val):
        dr = rr - sr
        dc = cc - sc
        dist2 = (dr * dr + dc * dc).astype("float32")

        # Mask out cells beyond the influence radius
        mask_far = dist2 > max_radius_cells2
        dist2[mask_far] = np.nan

        # Enforce minimum distance to avoid blowups
        dist2[dist2 < min_d2] = min_d2

        w = 1.0 / (dist2 ** (power / 2.0))
        w = np.nan_to_num(w, nan=0.0)

        num += w * val
        den += w

        # at the end of interpolate_rainfall_to_grid
    with np.errstate(invalid="ignore", divide="ignore"):
        rain = np.where(den > 0, num / den, 0.0).astype("float32")
    return rain


# # terrain/rain_interpolation.py

# from __future__ import annotations

# from dataclasses import dataclass
# from typing import List, Tuple

# import numpy as np
# from rasterio.transform import Affine


# @dataclass
# class RainStation:
#     lat: float
#     lon: float
#     rain_mm: float  # event rainfall or accumulated rainfall


# def latlon_to_rowcol(transform: Affine, lat: float, lon: float) -> Tuple[int, int]:
#     """
#     Convert (lat, lon) in EPSG:4326 to (row, col) given an Affine transform.

#     For north-up rasters:
#       col = (lon - c) / a
#       row = (f - lat) / |e|
#     """
#     a = transform.a
#     c = transform.c
#     e = transform.e
#     f = transform.f

#     col = (lon - c) / a
#     row = (f - lat) / abs(e)

#     return int(round(row)), int(round(col))


# def interpolate_rainfall_to_grid(
#     dem_array: np.ndarray,
#     transform: Affine,
#     stations: List[RainStation],
#     power: float = 2.0,
#     min_dist_cells: float = 1.0,
#     max_radius_cells: float | None = None,
# ) -> np.ndarray:
#     """
#     Simple IDW interpolation of station rainfall onto the DEM grid.

#     Distances are measured in *grid cells* (row/col space). For Timor-sized
#     domains this is a reasonable first approximation.

#     Returns:
#         2D np.ndarray[float32] of shape dem_array.shape.
#     """
#     rows, cols = dem_array.shape
#     rain = np.zeros((rows, cols), dtype="float32")

#     if not stations:
#         return rain

#     # Map stations to grid coordinates
#     st_pix = []
#     st_val = []
#     for st in stations:
#         try:
#             r, c = latlon_to_rowcol(transform, st.lat, st.lon)
#             if 0 <= r < rows and 0 <= c < cols:
#                 st_pix.append((r, c))
#                 st_val.append(st.rain_mm)
#         except Exception:
#             # station outside DEM or transform issues -> ignore
#             continue

#     if not st_pix:
#         return rain

#     st_pix = np.array(st_pix, dtype="int32")
#     st_val = np.array(st_val, dtype="float32")

#     rr = np.arange(rows, dtype="int32")[:, None]  # (rows, 1)
#     cc = np.arange(cols, dtype="int32")[None, :]  # (1, cols)

#     num = np.zeros_like(rain)
#     den = np.zeros_like(rain)

#     min_d2 = float(min_dist_cells**2)

#     for (sr, sc), val in zip(st_pix, st_val):
#         dr = rr - sr
#         dc = cc - sc
#         dist2 = (dr * dr + dc * dc).astype("float32")

#         if max_radius_cells is not None:
#             # mask out cells beyond max_radius
#             mask_far = dist2 > float(max_radius_cells**2)
#             dist2[mask_far] = np.nan

#         # enforce minimum distance to avoid blowups
#         dist2[dist2 < min_d2] = min_d2

#         w = 1.0 / (dist2 ** (power / 2.0))
#         w = np.nan_to_num(w, nan=0.0)

#         num += w * val
#         den += w

#     rain = np.where(den > 0, num / den, 0.0).astype("float32")
#     return rain


# # # terrain/rain_interpolation.py

# # from __future__ import annotations

# # from dataclasses import dataclass
# # from typing import List, Tuple

# # import numpy as np


# # @dataclass
# # class RainStation:
# #     lat: float
# #     lon: float
# #     rain_mm: float


# # # ==============================================================
# # # Helper: convert pixel center to lat/lon using affine transform
# # # ==============================================================


# # def _rowcol_to_latlon(
# #     transform, rows: np.ndarray, cols: np.ndarray
# # ) -> Tuple[np.ndarray, np.ndarray]:
# #     """
# #     Convert 2D arrays of (row, col) to lat/lon using affine transform from rasterio.

# #     transform = (a, b, c, d, e, f)
# #        x = a * col + b * row + c
# #        y = d * col + e * row + f

# #     For unrotated rasters (typical DEM/GeoTIFF), b = d = 0.
# #     c = lon_west, f = lat_north.

# #     Returns:
# #         lat (2D array), lon (2D array)
# #     """
# #     # Full affine (6 numbers)
# #     a = transform.a
# #     b = transform.b
# #     c = transform.c
# #     d = transform.d
# #     e = transform.e
# #     f = transform.f

# #     # Compute projected coordinates
# #     X = a * cols + b * rows + c
# #     Y = d * cols + e * rows + f

# #     # For WGS84 input rasters (deg), X=lon, Y=lat
# #     lon = X
# #     lat = Y

# #     return lat, lon


# # # ==============================================================
# # # Distance approximation in meters (fast equirectangular)
# # # ==============================================================

# # EARTH_RADIUS = 6371000.0  # meters


# # def _latlon_dist_m(lat1, lon1, lat2, lon2):
# #     """
# #     Fast equirectangular approximation for distance in meters.
# #     Accurate for small islands like Timor-Leste.
# #     """
# #     lat1_r = np.radians(lat1)
# #     lat2_r = np.radians(lat2)
# #     dlat = lat2_r - lat1_r
# #     dlon = np.radians(lon2 - lon1)

# #     x = dlon * np.cos((lat1_r + lat2_r) / 2.0)
# #     y = dlat
# #     return EARTH_RADIUS * np.sqrt(x * x + y * y)


# # # ==============================================================
# # # Main interpolation
# # # ==============================================================


# # def interpolate_rainfall_to_grid(
# #     dem_array: np.ndarray,
# #     transform,
# #     stations: List[RainStation],
# #     power: float = 2.0,
# #     min_dist_m: float = 100.0,
# #     max_radius_m: float | None = None,
# # ) -> np.ndarray:
# #     """
# #     IDW interpolation of rainfall onto the DEM grid.

# #     Parameters:
# #     - dem_array: 2D DEM array (shape defines grid)
# #     - transform: affine transform (from rasterio) defining pixel → lat/lon
# #     - stations: list of RainStation(lat, lon, rain_mm)
# #     - power: IDW exponent (2.0 = standard)
# #     - min_dist_m: minimum distance (avoid division by zero)
# #     - max_radius_m: optional max influence radius (e.g. 20000 meters)

# #     Returns:
# #         rain_mm grid (float32)
# #     """
# #     rows, cols = dem_array.shape
# #     rain = np.zeros((rows, cols), dtype="float32")

# #     if not stations:
# #         return rain

# #     # ---------------------------------------------------------------
# #     # Precompute grid lat/lon (2D arrays)
# #     # ---------------------------------------------------------------
# #     rr = np.arange(rows).reshape(rows, 1)
# #     cc = np.arange(cols).reshape(1, cols)

# #     grid_lat, grid_lon = _rowcol_to_latlon(transform, rr, cc)

# #     # ---------------------------------------------------------------
# #     # Precompute station coords
# #     # ---------------------------------------------------------------
# #     st_lats = np.array([s.lat for s in stations], dtype="float32")
# #     st_lons = np.array([s.lon for s in stations], dtype="float32")
# #     st_vals = np.array([s.rain_mm for s in stations], dtype="float32")

# #     # ---------------------------------------------------------------
# #     # IDW accumulation
# #     # ---------------------------------------------------------------
# #     num = np.zeros_like(rain)
# #     den = np.zeros_like(rain)

# #     for lat_s, lon_s, val in zip(st_lats, st_lons, st_vals):
# #         # compute distance (meters)
# #         dist = _latlon_dist_m(grid_lat, grid_lon, lat_s, lon_s)

# #         # apply min distance threshold
# #         dist = np.maximum(dist, min_dist_m)

# #         # optional influence radius
# #         if max_radius_m is not None:
# #             mask = dist <= max_radius_m
# #         else:
# #             mask = np.ones_like(dist, dtype=bool)

# #         w = np.where(mask, 1.0 / (dist**power), 0.0)

# #         num += w * val
# #         den += w

# #     # ---------------------------------------------------------------
# #     # Final rainfall field
# #     # ---------------------------------------------------------------
# #     rain = np.where(den > 0, num / den, 0.0).astype("float32")
# #     return rain


# # # terrain/rain_interpolation.py

# # from __future__ import annotations

# # from dataclasses import dataclass
# # from typing import List

# # import numpy as np

# # from .srtm_hgt import DemMosaic


# # @dataclass
# # class RainStation:
# #     lat: float
# #     lon: float
# #     rain_mm: float  # event rainfall or accumulated rainfall


# # def rain_raster_from_stations(
# #     mosaic: DemMosaic,
# #     stations: List[RainStation],
# #     power: float = 2.0,
# #     min_dist: float = 1e-3,
# # ) -> np.ndarray:
# #     """
# #     Very simple IDW interpolation of station rainfall onto the DEM grid.

# #     NOTE: This is O(N_cells * N_stations). For Timor-sized grids and
# #     tens of stations it's probably fine as a first implementation, but
# #     you may want to optimize or tile later.
# #     """
# #     rows, cols = mosaic.data.shape
# #     rain = np.zeros((rows, cols), dtype="float32")

# #     if not stations:
# #         return rain

# #     # Precompute station indices (in DEM row/col space)
# #     st_rc = []
# #     st_val = []
# #     for st in stations:
# #         try:
# #             r, c = mosaic.latlon_to_rowcol(st.lat, st.lon)
# #             st_rc.append((r, c))
# #             st_val.append(st.rain_mm)
# #         except ValueError:
# #             # station outside DEM → ignore
# #             continue

# #     if not st_rc:
# #         return rain

# #     st_rc = np.array(st_rc, dtype="int32")
# #     st_val = np.array(st_val, dtype="float32")

# #     rr = np.arange(rows, dtype="int32")[:, None]  # shape (rows, 1)
# #     cc = np.arange(cols, dtype="int32")[None, :]  # shape (1, cols)

# #     # For now: loop over stations, accumulate weights
# #     # (you can vectorize further later if needed)
# #     num = np.zeros_like(rain)
# #     den = np.zeros_like(rain)

# #     for (sr, sc), val in zip(st_rc, st_val):
# #         dr = rr - sr
# #         dc = cc - sc
# #         dist2 = dr * dr + dc * dc
# #         dist2 = dist2.astype("float32")
# #         dist2[dist2 < min_dist * min_dist] = min_dist * min_dist

# #         w = 1.0 / (dist2 ** (power / 2.0))
# #         num += w * val
# #         den += w

# #     rain = np.where(den > 0, num / den, 0.0).astype("float32")
# #     return rain
