# terrain/api/hydrology.py
from __future__ import annotations

import math
from io import BytesIO
from typing import List

import numpy as np
import rasterio
from fastapi import APIRouter, HTTPException, Response
from PIL import Image
from pydantic import BaseModel, Field
from rasterio.io import DatasetReader
from rasterio.windows import Window

from terrain.colorizers import (
    colorize_accum_rgba,
    colorize_dem_rgba,
    colorize_discharge_rgba,
    colorize_fsi_rgba,
    colorize_rain_rgba,
)
from terrain.job_engine import HydrologyJobEngine
from terrain.rain_interpolation import RainStation
import json

router = APIRouter(prefix="/hydrology", tags=["hydrology"])
job_engine = HydrologyJobEngine()


# ---------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------
class StationIn(BaseModel):
    lat: float
    lon: float
    rain_mm: float = Field(...)


class CreateJobRequest(BaseModel):
    stations: List[StationIn]
    event_duration_hours: float = Field(..., gt=0)
    runoff_coeff: float = Field(0.6, ge=0, le=1)


class CreateJobResponse(BaseModel):
    job_id: str
    status: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    error: str | None = None
    params: dict | None = None


# ---------------------------------------------------------------------
# Tile helpers
# ---------------------------------------------------------------------
def _tile_bounds_webmercator(z: int, x: int, y: int):
    """Exact XYZ → lon/lat convert used previously."""
    n = 2.0**z
    lon_min = x / n * 360.0 - 180.0
    lon_max = (x + 1) / n * 360.0 - 180.0

    def tile2lat(ty: int):
        yy = ty / n * math.pi * 2.0
        return math.degrees(math.atan(math.sinh(math.pi - yy)))

    lat_max = tile2lat(y)
    lat_min = tile2lat(y + 1)

    return lon_min, lat_min, lon_max, lat_max


def _open_job_raster(job_id: str, layer: str) -> DatasetReader:
    """Open DEM, accumulation, rain, discharge, FSI — exactly like original."""
    job_dir = job_engine._job_path(job_id)

    if layer == "dem":
        path = job_engine.base_hydrology_dir / "dem.tif"
    elif layer == "accum":
        path = job_engine.base_hydrology_dir / "d8_accum.tif"
    elif layer == "rain":
        path = job_dir / "rain_mm.tif"
    elif layer == "discharge":
        path = job_dir / "discharge_m3s.tif"
    elif layer == "fsi":
        path = job_dir / "fsi.tif"
    else:
        raise HTTPException(400, f"Unknown layer: {layer}")

    if not path.exists():
        raise HTTPException(404, f"Raster for layer '{layer}' not found")

    return rasterio.open(path)


# Global cache for FSI stats per job (vmin, vmax)
_fsi_stats_cache: dict[str, tuple[float, float]] = {}


def _get_fsi_vmin_vmax(job_id: str) -> tuple[float, float]:
    """
    Get or compute global FSI vmin/vmax for this job using Option B1:

      • Only FSI values INSIDE rain footprints on LAND are used
        to derive percentiles (5–95%).

    The result is cached into job.json under params["fsi_vmin"/"fsi_vmax"].
    """
    # 1) Try to read from job metadata
    job = job_engine.get_job(job_id)
    if job.params and "fsi_vmin" in job.params and "fsi_vmax" in job.params:
        return float(job.params["fsi_vmin"]), float(job.params["fsi_vmax"])

    # 2) Compute once from full rasters
    job_dir = job_engine._job_path(job_id)
    fsi_path = job_dir / "fsi.tif"
    rain_path = job_dir / "rain_mm.tif"
    dem_path = job_engine.base_hydrology_dir / "dem.tif"

    if not (fsi_path.exists() and rain_path.exists() and dem_path.exists()):
        # Safe fallback
        return 0.0, 1.0

    with rasterio.open(fsi_path) as fsi_src, rasterio.open(
        rain_path
    ) as rain_src, rasterio.open(dem_path) as dem_src:
        fsi_full = fsi_src.read(1).astype("float32")
        fsi_nodata = fsi_src.nodata

        rain_full = rain_src.read(1).astype("float32")
        rain_nodata = rain_src.nodata

        dem_full = dem_src.read(1).astype("float32")
        dem_nodata = dem_src.nodata

    # Land mask from DEM
    if dem_nodata is None:
        land_mask = np.isfinite(dem_full) & (dem_full > 0)
    else:
        land_mask = (dem_full != dem_nodata) & np.isfinite(dem_full) & (dem_full > 0)

    # FSI valid + land
    if fsi_nodata is None:
        f_valid = np.isfinite(fsi_full) & land_mask
    else:
        f_valid = (fsi_full != fsi_nodata) & np.isfinite(fsi_full) & land_mask

    # Rain footprint
    if rain_nodata is None:
        r_valid = (rain_full > 0) & np.isfinite(rain_full)
    else:
        r_valid = (rain_full != rain_nodata) & (rain_full > 0) & np.isfinite(rain_full)

    mask = f_valid & r_valid

    if np.any(mask):
        vals = fsi_full[mask]
    else:
        # If no FSI under rain (edge case), fall back to all land FSI
        vals = fsi_full[f_valid]

    if vals.size == 0:
        vmin, vmax = 0.0, 1.0
    else:
        vmin = float(np.percentile(vals, 5))
        vmax = float(np.percentile(vals, 95))
        if vmax <= vmin:
            vmax = vmin + 1e-6

    # 3) Persist back into job.json for next tiles
    meta_path = job_dir / "job.json"
    try:
        meta = json.loads(meta_path.read_text())
    except FileNotFoundError:
        meta = {"job_id": job_id, "status": getattr(job, "status", "unknown")}

    params = meta.get("params") or {}
    params["fsi_vmin"] = vmin
    params["fsi_vmax"] = vmax
    meta["params"] = params
    meta_path.write_text(json.dumps(meta, indent=2))

    return vmin, vmax


def _get_fsi_vmin_vmax_BAK(job_id: str) -> tuple[float, float]:
    """
    Get or compute global FSI vmin/vmax for this job using Option B1:

      • Only FSI values INSIDE rain footprints on LAND are used
        to derive percentiles (5–95%).

    The result is cached into job.json under params["fsi_vmin"/"fsi_vmax"].
    """
    # 1) Try to read from job metadata
    job = job_engine.get_job(job_id)
    if job.params and "fsi_vmin" in job.params and "fsi_vmax" in job.params:
        return float(job.params["fsi_vmin"]), float(job.params["fsi_vmax"])

    # 2) Compute once from full rasters
    job_dir = job_engine._job_path(job_id)
    fsi_path = job_dir / "fsi.tif"
    rain_path = job_dir / "rain_mm.tif"
    dem_path = job_engine.base_hydrology_dir / "dem.tif"

    if not (fsi_path.exists() and rain_path.exists() and dem_path.exists()):
        # Safe fallback
        return 0.0, 1.0

    import rasterio

    with rasterio.open(fsi_path) as fsi_src, rasterio.open(
        rain_path
    ) as rain_src, rasterio.open(dem_path) as dem_src:
        fsi_full = fsi_src.read(1).astype("float32")
        fsi_nodata = fsi_src.nodata

        rain_full = rain_src.read(1).astype("float32")
        rain_nodata = rain_src.nodata

        dem_full = dem_src.read(1).astype("float32")
        dem_nodata = dem_src.nodata

    # Land mask from DEM
    if dem_nodata is None:
        land_mask = np.isfinite(dem_full) & (dem_full > 0)
    else:
        land_mask = (dem_full != dem_nodata) & np.isfinite(dem_full) & (dem_full > 0)

    # FSI valid + land
    if fsi_nodata is None:
        f_valid = np.isfinite(fsi_full) & land_mask
    else:
        f_valid = (fsi_full != fsi_nodata) & np.isfinite(fsi_full) & land_mask

    # Rain footprint
    if rain_nodata is None:
        r_valid = (rain_full > 0) & np.isfinite(rain_full)
    else:
        r_valid = (rain_full != rain_nodata) & (rain_full > 0) & np.isfinite(rain_full)

    mask = f_valid & r_valid

    if np.any(mask):
        vals = fsi_full[mask]
    else:
        # If no FSI under rain (weird edge case), fall back to all land FSI
        vals = fsi_full[f_valid]

    if vals.size == 0:
        vmin, vmax = 0.0, 1.0
    else:
        vmin = float(np.percentile(vals, 5))
        vmax = float(np.percentile(vals, 95))
        if vmax <= vmin:
            vmax = vmin + 1e-6

    # 3) Persist back into job.json for next tiles
    meta_path = job_dir / "job.json"
    try:
        meta = json.loads(meta_path.read_text())
    except FileNotFoundError:
        meta = {"job_id": job_id, "status": getattr(job, "status", "unknown")}

    params = meta.get("params") or {}
    params["fsi_vmin"] = vmin
    params["fsi_vmax"] = vmax
    meta["params"] = params
    meta_path.write_text(json.dumps(meta, indent=2))

    return vmin, vmax


def _get_fsi_stats(job_id: str) -> tuple[float, float]:
    """
    Compute (and cache) global FSI vmin/vmax for a job from the full fsi.tif.

    This ensures tile-to-tile consistency: no per-tile percentile seams.
    """
    if job_id in _fsi_stats_cache:
        return _fsi_stats_cache[job_id]

    job_dir = job_engine._job_path(job_id)
    fsi_path = job_dir / "fsi.tif"

    if not fsi_path.exists():
        # Fallback: avoid crash, everything will normalize to 0
        vmin, vmax = 0.0, 1.0
        _fsi_stats_cache[job_id] = (vmin, vmax)
        return vmin, vmax

    with rasterio.open(fsi_path) as src:
        band = src.read(1).astype("float32")
        nodata = src.nodata

        if nodata is None:
            mask = np.isfinite(band)
        else:
            mask = (band != nodata) & np.isfinite(band)

        if not np.any(mask):
            vmin, vmax = 0.0, 1.0
        else:
            vals = band[mask]
            vmin = float(np.percentile(vals, 5))
            vmax = float(np.percentile(vals, 95))
            if vmax <= vmin:
                vmax = vmin + 1e-6

    _fsi_stats_cache[job_id] = (vmin, vmax)
    return vmin, vmax


def _read_window_for_tile(src: DatasetReader, z: int, x: int, y: int):
    """Preserves your original lat/lon → row/col mapping."""
    lon_min, lat_min, lon_max, lat_max = _tile_bounds_webmercator(z, x, y)

    t = src.transform

    col_min = int((lon_min - t.c) / t.a)
    col_max = int((lon_max - t.c) / t.a)
    row_min = int((t.f - lat_max) / abs(t.e))
    row_max = int((t.f - lat_min) / abs(t.e))

    col_min = max(col_min, 0)
    row_min = max(row_min, 0)
    col_max = min(col_max, src.width)
    row_max = min(row_max, src.height)

    if col_max <= col_min or row_max <= row_min:
        raise ValueError("Tile outside raster extent")

    window = Window.from_slices((row_min, row_max), (col_min, col_max))
    data = src.read(1, window=window).astype("float32")

    return data, window


def _pil_from_rgba(rgba: np.ndarray, size: int = 256):
    """256px output tile."""
    img = Image.fromarray(rgba, mode="RGBA")
    if img.size != (size, size):
        img = img.resize((size, size), resample=Image.Resampling.BILINEAR)
    return img


# ---------------------------------------------------------------------
# Tiles endpoint
# ---------------------------------------------------------------------
@router.get("/jobs/{job_id}/tiles/{layer}/{z}/{x}/{y}.png")
def get_tile(job_id: str, layer: str, z: int, x: int, y: int):
    try:
        src = _open_job_raster(job_id, layer)
    except HTTPException:
        raise

    try:
        try:
            data, window = _read_window_for_tile(src, z, x, y)
        except ValueError:
            src.close()
            empty = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
            buf = BytesIO()
            empty.save(buf, "PNG")
            return Response(buf.getvalue(), media_type="image/png")

        nodata = src.nodata

        # ----------------------------------------------------------
        # DEM (Sentinel-like)
        # ----------------------------------------------------------
        if layer == "dem":
            rgba = colorize_dem_rgba(data, nodata)

        # ----------------------------------------------------------
        # Accumulation (transparent background)
        # ----------------------------------------------------------
        elif layer == "accum":
            rgba = colorize_accum_rgba(data, nodata)

        # ----------------------------------------------------------
        # Rain (WMO)
        # ----------------------------------------------------------
        elif layer == "rain":
            # get event duration
            event_dur = 1.0
            try:
                job = job_engine.get_job(job_id)
                if job.params and "duration_h" in job.params:
                    event_dur = float(job.params["duration_h"])
            except Exception as e:
                print(f"Error getting event duration: {e}")
                pass

            rgba = colorize_rain_rgba(data, nodata, event_dur)

        # ----------------------------------------------------------
        # Discharge (final working version)
        # ----------------------------------------------------------
        elif layer == "discharge":
            # 1) rain mask tile
            with _open_job_raster(job_id, "rain") as rain_src:
                rain_tile = rain_src.read(1, window=window).astype("float32")
                rain_nodata = rain_src.nodata

            # 2) DEM for ocean masking
            with _open_job_raster(job_id, "dem") as dem_src:
                dem_tile = dem_src.read(1, window=window).astype("float32")
                dem_nodata = dem_src.nodata

            # 3) final discharge renderer
            rgba = colorize_discharge_rgba(
                discharge=data,
                discharge_nodata=nodata,
                rain_mm=rain_tile,
                rain_nodata=rain_nodata,
                dem=dem_tile,
                dem_nodata=dem_nodata,
                background_alpha=166,
            )

        # ----------------------------------------------------------
        # FSI
        # ----------------------------------------------------------
        elif layer == "fsi":
            # FSI tile
            # with _open_job_raster(job_id, "fsi") as fsi_src:
            #     fsi_data, window = _read_window_for_tile(fsi_src, z, x, y)
            #     fsi_nodata = fsi_src.nodata

            # # DEM for ocean / land masking
            # with _open_job_raster(job_id, "dem") as dem_src:
            #     dem_data = dem_src.read(1, window=window).astype("float32")
            #     dem_nodata = dem_src.nodata

            # # Rain for hazard footprint
            # with _open_job_raster(job_id, "rain") as rain_src:
            #     rain_data = rain_src.read(1, window=window).astype("float32")
            #     rain_nodata = rain_src.nodata

            # # Global FSI stats (vmin/vmax) per job
            # fsi_vmin, fsi_vmax = _get_fsi_stats(job_id)

            # rgba = colorize_fsi_rgba(
            #     fsi=fsi_data,
            #     fsi_nodata=fsi_nodata,
            #     dem=dem_data,
            #     dem_nodata=dem_nodata,
            #     rain_mm=rain_data,
            #     rain_nodata=rain_nodata,
            #     fsi_vmin=fsi_vmin,
            #     fsi_vmax=fsi_vmax,
            # )
            with _open_job_raster(job_id, "fsi") as fsi_src:
                fsi_data, window = _read_window_for_tile(fsi_src, z, x, y)
                fsi_nodata = fsi_src.nodata

            # DEM for ocean masking and land context
            with _open_job_raster(job_id, "dem") as dem_src:
                dem_data = dem_src.read(1, window=window).astype("float32")
                dem_nodata = dem_src.nodata

            # Rain for defining hazard zones
            with _open_job_raster(job_id, "rain") as rain_src:
                rain_data = rain_src.read(1, window=window).astype("float32")
                rain_nodata = rain_src.nodata

            # Global job-level vmin/vmax from rain-zone FSI (Option B1)
            # fsi_vmin, fsi_vmax = _get_fsi_vmin_vmax(job_id)

            # rgba = colorize_fsi_rgba(
            #     fsi=fsi_data,
            #     fsi_nodata=fsi_nodata,
            #     dem=dem_data,
            #     dem_nodata=dem_nodata,
            #     rain_mm=rain_data,
            #     rain_nodata=rain_nodata,
            #     vmin=fsi_vmin,
            #     vmax=fsi_vmax,
            # )
            # rgba = colorize_fsi_rgba(
            #     fsi=fsi_data,
            #     fsi_nodata=fsi_nodata,
            #     dem=dem_data,
            #     dem_nodata=dem_nodata,
            #     rain_mm=rain_data,
            #     rain_nodata=rain_nodata,
            # )
            fsi_vmin, fsi_vmax = _get_fsi_vmin_vmax(job_id)

            rgba = colorize_fsi_rgba(
                fsi=fsi_data,
                fsi_nodata=fsi_nodata,
                dem=dem_data,
                dem_nodata=dem_nodata,
                rain_mm=rain_data,
                rain_nodata=rain_nodata,
                vmin=fsi_vmin,
                vmax=fsi_vmax,
            )
        else:
            raise HTTPException(400, f"Unknown layer {layer}")

        src.close()

        img = _pil_from_rgba(rgba)
        buf = BytesIO()
        img.save(buf, "PNG")
        return Response(buf.getvalue(), media_type="image/png")

    finally:
        if not src.closed:
            src.close()


# ---------------------------------------------------------------------
# Job endpoints
# ---------------------------------------------------------------------
@router.post("/jobs", response_model=CreateJobResponse)
def create_hydrology_job(body: CreateJobRequest):
    stations = [
        RainStation(lat=s.lat, lon=s.lon, rain_mm=s.rain_mm) for s in body.stations
    ]

    job = job_engine.create_job(
        stations=stations,
        event_duration_hours=body.event_duration_hours,
        runoff_coeff=body.runoff_coeff,
    )
    return CreateJobResponse(job_id=job.job_id, status=job.status)


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
def get_job_status(job_id: str):
    try:
        job = job_engine.get_job(job_id)
    except FileNotFoundError:
        raise HTTPException(404, "Job not found")

    return JobStatusResponse(
        job_id=job.job_id,
        status=job.status,
        error=job.error,
        params=job.params,
    )
