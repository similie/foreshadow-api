# terrain/job_engine.py

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
import uuid
import traceback
import json

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.warp import reproject, Resampling, calculate_default_transform
from terrain.analysis.flood_index import compute_fsi

from .rain_interpolation import RainStation, interpolate_rainfall_to_grid
from .runoff import (
    runoff_depth_from_rain,
    drainage_area_from_accum,
    discharge_from_runoff,
    cell_area_m2_at_lat,
)


@dataclass
class HydrologyJob:
    job_id: str
    status: str
    error: Optional[str] = None
    params: Optional[dict] = None


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def _save_geotiff_4326(path: Path, data: np.ndarray, transform, nodata=np.nan):
    """
    Save a 2D float32 raster in EPSG:4326 with the given transform.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    nodata_val = -9999 if np.isnan(nodata) else float(nodata)

    profile = {
        "driver": "GTiff",
        "height": data.shape[0],
        "width": data.shape[1],
        "count": 1,
        "dtype": "float32",
        "crs": CRS.from_epsg(4326),
        "transform": transform,
        "nodata": nodata_val,
        "compress": "LZW",
    }

    arr = np.where(np.isnan(data), nodata_val, data).astype("float32")

    with rasterio.open(path, "w", **profile) as dst:
        dst.write(arr, 1)


def _reproject_to_3857(
    src_path: Path,
    dst_path: Path,
    resampling: Resampling = Resampling.nearest,
):
    """
    Reproject a raster on disk (assumed EPSG:4326) to EPSG:3857.

    For rain / discharge we use NEAREST to avoid nodata smearing.
    """
    dst_path = Path(dst_path)
    dst_path.parent.mkdir(parents=True, exist_ok=True)

    with rasterio.open(src_path) as src:
        dst_crs = CRS.from_epsg(3857)

        transform, width, height = calculate_default_transform(
            src.crs, dst_crs, src.width, src.height, *src.bounds
        )

        profile = src.profile.copy()
        profile.update(
            {
                "crs": dst_crs,
                "transform": transform,
                "width": width,
                "height": height,
                "dtype": "float32",
                "compress": "LZW",
            }
        )

        with rasterio.open(dst_path, "w", **profile) as dst:
            reproject(
                source=rasterio.band(src, 1),
                destination=rasterio.band(dst, 1),
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=transform,
                dst_crs=dst_crs,
                resampling=resampling,
            )

        print(f"  ✔ Reprojected → {dst_path}")


# ---------------------------------------------------------------------
# Job Engine
# ---------------------------------------------------------------------
class HydrologyJobEngine:
    def __init__(self, base_dir: str | Path = "data/hydrology"):
        self.base_hydrology_dir = Path(base_dir)
        self.jobs_root = Path("data/hydrology_jobs")
        self.jobs_root.mkdir(parents=True, exist_ok=True)

    def _job_path(self, job_id: str) -> Path:
        return self.jobs_root / job_id

    def get_job(self, job_id: str) -> HydrologyJob:
        job_dir = self._job_path(job_id)
        if not job_dir.exists():
            raise FileNotFoundError(job_id)

        meta_file = job_dir / "job.json"
        if not meta_file.exists():
            return HydrologyJob(job_id=job_id, status="unknown")

        meta = json.loads(meta_file.read_text())
        return HydrologyJob(**meta)

    def _write_meta(self, job: HydrologyJob):
        job_dir = self._job_path(job.job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "job.json").write_text(json.dumps(job.__dict__, indent=2))

    def create_job(
        self,
        stations: List[RainStation],
        event_duration_hours: float,
        runoff_coeff: float,
        rain_radius_km: Optional[float] = None,
    ) -> HydrologyJob:
        """
        Create a hydrology job. If rain_radius_km is None, defaults to 10 km.
        """
        radius_km = rain_radius_km if rain_radius_km is not None else 10.0

        job_id = uuid.uuid4().hex
        job_dir = self._job_path(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)

        job = HydrologyJob(
            job_id=job_id,
            status="running",
            params={
                "stations": [s.__dict__ for s in stations],
                "duration_h": event_duration_hours,
                "runoff_coeff": runoff_coeff,
                "rain_radius_km": radius_km,
            },
        )
        self._write_meta(job)

        try:
            self._run_job(
                job_id,
                job_dir,
                stations,
                event_duration_hours,
                runoff_coeff,
                radius_km,
            )
            job.status = "completed"
        except Exception as e:
            job.status = "failed"
            job.error = f"{e}\n{traceback.format_exc()}"

        self._write_meta(job)
        return job

    def _run_job(
        self,
        job_id: str,
        job_dir: Path,
        stations: List[RainStation],
        event_duration_hours: float,
        runoff_coeff: float,
        rain_radius_km: float,
    ):
        """
        Correct, contamination-proof hydrology job pipeline:

        1. Load DEM + ACCUM (EPSG:4326)
        2. Interpolate rainfall → rain_mm grid
        3. Compute runoff depth
        4. Compute drainage area (from accum)
        5. Compute discharge (runoff × area / duration)
        6. Force discharge = 0 wherever rainfall exists but drainage area is tiny
        7. Save clean rain_mm.tif & discharge_m3s.tif (4326)
        8. Reproject both to EPSG:3857 with STRICT nearest (no merging)
        """

        # -------------------------------
        # 1) Load DEM + ACCUM
        # -------------------------------
        dem_path = self.base_hydrology_dir / "dem.tif"
        accum_path = self.base_hydrology_dir / "d8_accum.tif"

        with rasterio.open(dem_path) as dem_src:
            dem = dem_src.read(1).astype("float32")
            dem_transform = dem_src.transform

        with rasterio.open(accum_path) as acc_src:
            accum = acc_src.read(1).astype("float32")

        rows, cols = dem.shape
        print(
            f"[job {job_id}] DEM shape={dem.shape}, transform={dem_transform}, "
            f"rain_radius_km={rain_radius_km}"
        )

        # -------------------------------
        # 2) Rainfall interpolation
        # -------------------------------
        rain_mm = interpolate_rainfall_to_grid(
            dem_array=dem,
            transform=dem_transform,
            stations=stations,
            power=2.0,
            min_dist_cells=1.0,
            rain_radius_km=rain_radius_km,
            use_wind=True,  # NEW
        ).astype("float32")

        # Guarantee no negative values (rare interpolation artifact)
        rain_mm = np.clip(rain_mm, 0, None)

        _save_geotiff_4326(job_dir / "rain_mm.tif", rain_mm, dem_transform)

        # -------------------------------
        # 3) Runoff depth
        # -------------------------------
        runoff_depth = runoff_depth_from_rain(rain_mm, runoff_coeff)

        # -------------------------------
        # 4) Drainage area (from ACCUM)
        # -------------------------------
        cellsize_deg = abs(dem_transform.a)
        lat_north = dem_transform.f
        lat_south = lat_north + dem_transform.e * rows
        lat_center = 0.5 * (lat_north + lat_south)

        cell_area_m2 = cell_area_m2_at_lat(lat_center, cellsize_deg)
        drainage_area = drainage_area_from_accum(accum, cell_area_m2)

        # -------------------------------
        # 5) Discharge (m³/s)
        # -------------------------------
        duration_seconds = event_duration_hours * 3600.0

        discharge = discharge_from_runoff(
            runoff_depth_m=runoff_depth,
            drainage_area_m2=drainage_area,
            duration_seconds=duration_seconds,
        ).astype("float32")

        discharge = np.clip(discharge, 0, None)

        # -------------------------------
        # ⭐ 6) Remove rainfall halo contamination completely
        # -------------------------------
        #
        # RULE:
        #   If rain exists but drainage is tiny, discharge MUST be 0.
        #
        # This eliminates all artificial “blue circles” and all leakage.
        #
        tiny_drainage = drainage_area < (cell_area_m2 * 2)  # 1–2 cells drainage area
        rainfall_exists = rain_mm > 0

        contamination_mask = rainfall_exists & tiny_drainage

        discharge[contamination_mask] = 0.0

        # Also guarantee no residuals:
        discharge[np.isnan(discharge)] = 0.0

        # -------------------------------
        # 7) Save uncontaminated discharge (4326)
        # -------------------------------
        _save_geotiff_4326(job_dir / "discharge_m3s.tif", discharge, dem_transform)

        # -------------------------------
        # 8) Reproject → EPSG:3857 (STRICT nearest)
        # -------------------------------
        print(f"[job {job_id}] Reprojecting job rasters to EPSG:3857 ...")

        _reproject_to_3857(
            src_path=job_dir / "rain_mm.tif",
            dst_path=job_dir / "rain_mm_3857.tif",
            resampling=Resampling.nearest,  # IMPORTANT
        )

        _reproject_to_3857(
            src_path=job_dir / "discharge_m3s.tif",
            dst_path=job_dir / "discharge_m3s_3857.tif",
            resampling=Resampling.nearest,  # IMPORTANT
        )

        # 6) FSI computation
        fsi = compute_fsi(
            dem=dem,
            accum=accum,
            discharge=discharge,
            rain_mm=rain_mm,
            transform=dem_transform,
        )

        # save EPSG:4326 FSI
        _save_geotiff_4326(job_dir / "fsi.tif", fsi, dem_transform)

        # reproject to 3857
        _reproject_to_3857(
            src_path=job_dir / "fsi.tif",
            dst_path=job_dir / "fsi_3857.tif",
            resampling=Resampling.bilinear,
        )

        print(f"[job {job_id}] ✔ Hydrology job complete.")

    def _run_job_bak(
        self,
        job_id: str,
        job_dir: Path,
        stations: List[RainStation],
        event_duration_hours: float,
        runoff_coeff: float,
        rain_radius_km: float,
    ):
        """
        Hydrology computation for a job:

          1. Load DEM & d8_accum in EPSG:4326
          2. Interpolate rainfall onto DEM grid (radius-limited)
          3. Compute runoff depth, drainage area, discharge
          4. Save rain_mm.tif & discharge_m3s.tif in EPSG:4326
          5. Reproject both to EPSG:3857
        """
        dem_path = self.base_hydrology_dir / "dem.tif"
        accum_path = self.base_hydrology_dir / "d8_accum.tif"

        with rasterio.open(dem_path) as dem_src:
            dem = dem_src.read(1).astype("float32")
            dem_transform = dem_src.transform

        with rasterio.open(accum_path) as acc_src:
            accum = acc_src.read(1).astype("float32")

        rows, cols = dem.shape
        print(
            f"[job {job_id}] DEM shape: {dem.shape}, transform: {dem_transform}, "
            f"rain_radius_km={rain_radius_km}"
        )

        # 1) Rainfall interpolation → rain_mm grid
        rain_mm = interpolate_rainfall_to_grid(
            dem_array=dem,
            transform=dem_transform,
            stations=stations,
            power=2.0,
            min_dist_cells=1.0,
            rain_radius_km=rain_radius_km,
        ).astype("float32")

        _save_geotiff_4326(job_dir / "rain_mm.tif", rain_mm, dem_transform)

        # 2) Runoff depth
        runoff_depth = runoff_depth_from_rain(rain_mm, runoff_coeff)

        # 3) Drainage area
        cellsize_deg = abs(dem_transform.a)  # lon spacing ~ lat spacing here
        lat_north = dem_transform.f
        lat_south = lat_north + dem_transform.e * rows  # e is negative
        lat_center = 0.5 * (lat_north + lat_south)

        cell_area = cell_area_m2_at_lat(lat_center, cellsize_deg)
        drainage_area = drainage_area_from_accum(accum, cell_area)

        # 4) Discharge
        duration_sec = event_duration_hours * 3600.0

        discharge = discharge_from_runoff(
            runoff_depth_m=runoff_depth,
            drainage_area_m2=drainage_area,
            duration_seconds=duration_sec,
        ).astype("float32")

        _save_geotiff_4326(job_dir / "discharge_m3s.tif", discharge, dem_transform)

        # 5) Reproject job rasters to WebMercator (use NEAREST)
        print(f"[job {job_id}] Reprojecting job rasters to EPSG:3857 ...")

        _reproject_to_3857(
            src_path=job_dir / "rain_mm.tif",
            dst_path=job_dir / "rain_mm_3857.tif",
            resampling=Resampling.nearest,
        )

        _reproject_to_3857(
            src_path=job_dir / "discharge_m3s.tif",
            dst_path=job_dir / "discharge_m3s_3857.tif",
            resampling=Resampling.nearest,
        )

        print(f"[job {job_id}] ✔ Hydrology job complete.")


# # terrain/job_engine.py

# from __future__ import annotations

# from dataclasses import dataclass
# from pathlib import Path
# from typing import List, Optional
# import uuid
# import traceback
# import json

# import numpy as np
# import rasterio
# from rasterio.crs import CRS
# from rasterio.warp import reproject, Resampling

# from .rain_interpolation import RainStation, interpolate_rainfall_to_grid
# from .runoff import (
#     runoff_depth_from_rain,
#     drainage_area_from_accum,
#     discharge_from_runoff,
#     cell_area_m2_at_lat,
# )


# @dataclass
# class HydrologyJob:
#     job_id: str
#     status: str
#     error: Optional[str] = None
#     params: Optional[dict] = None


# # ---------------------------------------------------------------------
# # Helpers
# # ---------------------------------------------------------------------
# def _save_geotiff_4326(path: Path, data: np.ndarray, transform, nodata=np.nan):
#     """
#     Save a 2D float32 raster in EPSG:4326 with the given transform.
#     """
#     path.parent.mkdir(parents=True, exist_ok=True)

#     nodata_val = -9999 if np.isnan(nodata) else float(nodata)

#     profile = {
#         "driver": "GTiff",
#         "height": data.shape[0],
#         "width": data.shape[1],
#         "count": 1,
#         "dtype": "float32",
#         "crs": CRS.from_epsg(4326),
#         "transform": transform,
#         "nodata": nodata_val,
#         "compress": "LZW",
#     }

#     arr = np.where(np.isnan(data), nodata_val, data).astype("float32")

#     with rasterio.open(path, "w", **profile) as dst:
#         dst.write(arr, 1)


# def _reproject_to_3857(src_path: Path, dst_path: Path, resampling=Resampling.bilinear):
#     """
#     Reproject a raster on disk (assumed EPSG:4326) to EPSG:3857.
#     """
#     dst_path = Path(dst_path)
#     dst_path.parent.mkdir(parents=True, exist_ok=True)

#     with rasterio.open(src_path) as src:
#         dst_crs = CRS.from_epsg(3857)

#         transform, width, height = rasterio.warp.calculate_default_transform(
#             src.crs, dst_crs, src.width, src.height, *src.bounds
#         )

#         profile = src.profile.copy()
#         profile.update(
#             {
#                 "crs": dst_crs,
#                 "transform": transform,
#                 "width": width,
#                 "height": height,
#                 "dtype": "float32",
#                 "compress": "LZW",
#             }
#         )

#         with rasterio.open(dst_path, "w", **profile) as dst:
#             reproject(
#                 source=rasterio.band(src, 1),
#                 destination=rasterio.band(dst, 1),
#                 src_transform=src.transform,
#                 src_crs=src.crs,
#                 dst_transform=transform,
#                 dst_crs=dst_crs,
#                 resampling=resampling,
#             )

#         print(f"  ✔ Reprojected → {dst_path}")


# # ---------------------------------------------------------------------
# # Job Engine
# # ---------------------------------------------------------------------
# class HydrologyJobEngine:
#     def __init__(self, base_dir: str | Path = "data/hydrology"):
#         self.base_hydrology_dir = Path(base_dir)
#         self.jobs_root = Path("data/hydrology_jobs")
#         self.jobs_root.mkdir(parents=True, exist_ok=True)

#     def _job_path(self, job_id: str) -> Path:
#         return self.jobs_root / job_id

#     def get_job(self, job_id: str) -> HydrologyJob:
#         job_dir = self._job_path(job_id)
#         if not job_dir.exists():
#             raise FileNotFoundError(job_id)

#         meta_file = job_dir / "job.json"
#         if not meta_file.exists():
#             return HydrologyJob(job_id=job_id, status="unknown")

#         meta = json.loads(meta_file.read_text())
#         return HydrologyJob(**meta)

#     def _write_meta(self, job: HydrologyJob):
#         job_dir = self._job_path(job.job_id)
#         job_dir.mkdir(parents=True, exist_ok=True)
#         (job_dir / "job.json").write_text(json.dumps(job.__dict__, indent=2))

#     def create_job(
#         self,
#         stations: List[RainStation],
#         event_duration_hours: float,
#         runoff_coeff: float,
#     ) -> HydrologyJob:
#         job_id = uuid.uuid4().hex
#         job_dir = self._job_path(job_id)
#         job_dir.mkdir(parents=True, exist_ok=True)

#         job = HydrologyJob(
#             job_id=job_id,
#             status="running",
#             params={
#                 "stations": [s.__dict__ for s in stations],
#                 "duration_h": event_duration_hours,
#                 "runoff_coeff": runoff_coeff,
#             },
#         )
#         self._write_meta(job)

#         try:
#             self._run_job(job_id, job_dir, stations, event_duration_hours, runoff_coeff)
#             job.status = "completed"
#         except Exception as e:
#             job.status = "failed"
#             job.error = f"{e}\n{traceback.format_exc()}"

#         self._write_meta(job)
#         return job

#     def _run_job(
#         self,
#         job_id: str,
#         job_dir: Path,
#         stations: List[RainStation],
#         event_duration_hours: float,
#         runoff_coeff: float,
#     ):
#         """
#         Hydrology computation for a job:

#           1. Load DEM & d8_accum in EPSG:4326
#           2. Interpolate rainfall onto DEM grid
#           3. Compute runoff depth, drainage area, discharge
#           4. Save rain_mm.tif & discharge_m3s.tif in EPSG:4326
#           5. Reproject both to EPSG:3857
#         """
#         dem_path = self.base_hydrology_dir / "dem.tif"
#         accum_path = self.base_hydrology_dir / "d8_accum.tif"

#         with rasterio.open(dem_path) as dem_src:
#             dem = dem_src.read(1).astype("float32")
#             dem_transform = dem_src.transform

#         with rasterio.open(accum_path) as acc_src:
#             accum = acc_src.read(1).astype("float32")

#         rows, cols = dem.shape
#         print(f"[job {job_id}] DEM shape: {dem.shape}, transform: {dem_transform}")

#         # 1) Rainfall interpolation → rain_mm grid
#         rain_mm = interpolate_rainfall_to_grid(
#             dem_array=dem,
#             transform=dem_transform,
#             stations=stations,
#             power=2.0,
#             min_dist_cells=1.0,
#             max_radius_cells=None,
#         ).astype("float32")

#         _save_geotiff_4326(job_dir / "rain_mm.tif", rain_mm, dem_transform)

#         # 2) Runoff depth
#         runoff_depth = runoff_depth_from_rain(rain_mm, runoff_coeff)

#         # 3) Drainage area
#         cellsize_deg = abs(dem_transform.a)
#         lat_north = dem_transform.f
#         lat_south = lat_north + dem_transform.e * rows  # e is negative
#         lat_center = 0.5 * (lat_north + lat_south)

#         cell_area = cell_area_m2_at_lat(lat_center, cellsize_deg)
#         drainage_area = drainage_area_from_accum(accum, cell_area)

#         # 4) Discharge
#         duration_sec = event_duration_hours * 3600.0

#         discharge = discharge_from_runoff(
#             runoff_depth_m=runoff_depth,
#             drainage_area_m2=drainage_area,
#             duration_seconds=duration_sec,
#         ).astype("float32")

#         _save_geotiff_4326(job_dir / "discharge_m3s.tif", discharge, dem_transform)

#         # 5) Reproject job rasters to WebMercator
#         print(f"[job {job_id}] Reprojecting job rasters to EPSG:3857 ...")

#         _reproject_to_3857(
#             src_path=job_dir / "rain_mm.tif",
#             dst_path=job_dir / "rain_mm_3857.tif",
#             resampling=Resampling.bilinear,
#         )

#         _reproject_to_3857(
#             src_path=job_dir / "discharge_m3s.tif",
#             dst_path=job_dir / "discharge_m3s_3857.tif",
#             resampling=Resampling.bilinear,
#         )

#         print(f"[job {job_id}] ✔ Hydrology job complete.")


# # # terrain/job_engine.py

# # from __future__ import annotations

# # from dataclasses import dataclass
# # from pathlib import Path
# # from typing import List, Optional
# # import uuid
# # import traceback
# # import json

# # import numpy as np
# # import rasterio
# # from rasterio.crs import CRS
# # from rasterio.warp import reproject, Resampling

# # from .rain_interpolation import RainStation, interpolate_rainfall_to_grid
# # from .runoff import (
# #     runoff_depth_from_rain,
# #     drainage_area_from_accum,
# #     discharge_from_runoff,
# #     cell_area_m2_at_lat,
# # )


# # # ============================================================
# # # Data model
# # # ============================================================


# # @dataclass
# # class HydrologyJob:
# #     job_id: str
# #     status: str
# #     error: Optional[str] = None
# #     params: Optional[dict] = None


# # # ============================================================
# # # Helpers
# # # ============================================================


# # def _save_geotiff_4326(path: Path, data: np.ndarray, transform, nodata=np.nan):
# #     """
# #     Save a 2D float32 raster in EPSG:4326 with the given transform.
# #     """
# #     path.parent.mkdir(parents=True, exist_ok=True)

# #     nodata_val = -9999 if np.isnan(nodata) else float(nodata)

# #     profile = {
# #         "driver": "GTiff",
# #         "height": data.shape[0],
# #         "width": data.shape[1],
# #         "count": 1,
# #         "dtype": "float32",
# #         "crs": CRS.from_epsg(4326),
# #         "transform": transform,
# #         "nodata": nodata_val,
# #         "compress": "LZW",
# #     }

# #     arr = np.where(np.isnan(data), nodata_val, data).astype("float32")

# #     with rasterio.open(path, "w", **profile) as dst:
# #         dst.write(arr, 1)


# # def _reproject_to_3857(src_path: Path, dst_path: Path, resampling=Resampling.bilinear):
# #     """
# #     Reproject a raster on disk (assumed EPSG:4326) to EPSG:3857.
# #     """
# #     dst_path = Path(dst_path)
# #     dst_path.parent.mkdir(parents=True, exist_ok=True)

# #     with rasterio.open(src_path) as src:
# #         dst_crs = CRS.from_epsg(3857)

# #         transform, width, height = rasterio.warp.calculate_default_transform(
# #             src.crs, dst_crs, src.width, src.height, *src.bounds
# #         )

# #         profile = src.profile.copy()
# #         profile.update(
# #             {
# #                 "crs": dst_crs,
# #                 "transform": transform,
# #                 "width": width,
# #                 "height": height,
# #                 "dtype": "float32",
# #                 "compress": "LZW",
# #             }
# #         )

# #         with rasterio.open(dst_path, "w", **profile) as dst:
# #             reproject(
# #                 source=rasterio.band(src, 1),
# #                 destination=rasterio.band(dst, 1),
# #                 src_transform=src.transform,
# #                 src_crs=src.crs,
# #                 dst_transform=transform,
# #                 dst_crs=dst_crs,
# #                 resampling=resampling,
# #             )

# #         print(f"  ✔ Reprojected → {dst_path}")


# # # ============================================================
# # # Job Engine
# # # ============================================================


# # class HydrologyJobEngine:
# #     def __init__(self, base_dir: str | Path = "data/hydrology"):
# #         self.base_hydrology_dir = Path(base_dir)
# #         self.jobs_root = Path("data/hydrology_jobs")
# #         self.jobs_root.mkdir(parents=True, exist_ok=True)

# #     # -----------------------------------------
# #     def _job_path(self, job_id: str) -> Path:
# #         return self.jobs_root / job_id

# #     # -----------------------------------------
# #     def get_job(self, job_id: str) -> HydrologyJob:
# #         job_dir = self._job_path(job_id)
# #         if not job_dir.exists():
# #             raise FileNotFoundError(job_id)

# #         meta_file = job_dir / "job.json"
# #         if not meta_file.exists():
# #             return HydrologyJob(job_id=job_id, status="unknown")

# #         meta = json.loads(meta_file.read_text())
# #         return HydrologyJob(**meta)

# #     # -----------------------------------------
# #     def _write_meta(self, job: HydrologyJob):
# #         job_dir = self._job_path(job.job_id)
# #         job_dir.mkdir(parents=True, exist_ok=True)
# #         (job_dir / "job.json").write_text(json.dumps(job.__dict__, indent=2))

# #     # -----------------------------------------
# #     def create_job(
# #         self,
# #         stations: List[RainStation],
# #         event_duration_hours: float,
# #         runoff_coeff: float,
# #     ) -> HydrologyJob:
# #         job_id = uuid.uuid4().hex
# #         job_dir = self._job_path(job_id)
# #         job_dir.mkdir(parents=True, exist_ok=True)

# #         job = HydrologyJob(
# #             job_id=job_id,
# #             status="running",
# #             params={
# #                 "stations": [s.__dict__ for s in stations],
# #                 "duration_h": event_duration_hours,
# #                 "runoff_coeff": runoff_coeff,
# #             },
# #         )
# #         self._write_meta(job)

# #         try:
# #             self._run_job(
# #                 job_id,
# #                 job_dir,
# #                 stations,
# #                 event_duration_hours,
# #                 runoff_coeff,
# #             )
# #             job.status = "completed"
# #         except Exception as e:
# #             job.status = "failed"
# #             job.error = f"{e}\n{traceback.format_exc()}"

# #         self._write_meta(job)
# #         return job

# #     # -----------------------------------------
# #     def _run_job(
# #         self,
# #         job_id: str,
# #         job_dir: Path,
# #         stations: List[RainStation],
# #         event_duration_hours: float,
# #         runoff_coeff: float,
# #     ):
# #         """
# #         Main hydrology computation for a job.

# #         1. Load DEM & d8_accum in EPSG:4326
# #         2. Interpolate rainfall onto DEM grid
# #         3. Compute runoff depth, drainage area, discharge
# #         4. Save rain_mm.tif & discharge_m3s.tif (EPSG:4326)
# #         5. Reproject to EPSG:3857 for tiling
# #         """

# #         # ------------------------------------------------------
# #         # 0. Load DEM + accumulation
# #         # ------------------------------------------------------
# #         dem_path = self.base_hydrology_dir / "dem.tif"
# #         accum_path = self.base_hydrology_dir / "d8_accum.tif"

# #         with rasterio.open(dem_path) as dem_src:
# #             dem = dem_src.read(1).astype("float32")
# #             dem_transform = dem_src.transform

# #         with rasterio.open(accum_path) as acc_src:
# #             accum = acc_src.read(1).astype("float32")

# #         rows, cols = dem.shape

# #         # ------------------------------------------------------
# #         # 1. Interpolate rainfall to grid
# #         # ------------------------------------------------------
# #         rain_mm = interpolate_rainfall_to_grid(
# #             dem_array=dem,
# #             transform=dem_transform,
# #             stations=stations,
# #             power=2.0,
# #             min_dist_m=100.0,
# #             max_radius_m=None,
# #         ).astype("float32")

# #         _save_geotiff_4326(job_dir / "rain_mm.tif", rain_mm, dem_transform)

# #         # ------------------------------------------------------
# #         # 2. Runoff depth from rainfall
# #         # ------------------------------------------------------
# #         runoff_depth = runoff_depth_from_rain(rain_mm, runoff_coeff)

# #         # ------------------------------------------------------
# #         # 3. Drainage area from accumulation
# #         # ------------------------------------------------------
# #         cellsize_deg = abs(dem_transform.a)
# #         lat_north = dem_transform.f
# #         lat_south = lat_north - rows * cellsize_deg
# #         lat_center = 0.5 * (lat_north + lat_south)

# #         cell_area = cell_area_m2_at_lat(lat_center, cellsize_deg)
# #         drainage_area = drainage_area_from_accum(accum, cell_area)

# #         # ------------------------------------------------------
# #         # 4. Discharge (m³/s)
# #         # ------------------------------------------------------
# #         duration_sec = event_duration_hours * 3600.0

# #         discharge = discharge_from_runoff(
# #             runoff_depth,
# #             drainage_area,
# #             duration_sec,
# #         ).astype("float32")

# #         _save_geotiff_4326(job_dir / "discharge_m3s.tif", discharge, dem_transform)

# #         # ------------------------------------------------------
# #         # 5. Reproject job rasters to EPSG:3857
# #         # ------------------------------------------------------
# #         print("🌐 Reprojecting job output rasters to EPSG:3857 ...")

# #         _reproject_to_3857(
# #             job_dir / "rain_mm.tif",
# #             job_dir / "rain_mm_3857.tif",
# #             resampling=Resampling.bilinear,
# #         )

# #         _reproject_to_3857(
# #             job_dir / "discharge_m3s.tif",
# #             job_dir / "discharge_m3s_3857.tif",
# #             resampling=Resampling.bilinear,
# #         )

# #         print(f"✔ Hydrology job {job_id} complete.")


# # # # terrain/preprocess.py

# # # from __future__ import annotations
# # # from pathlib import Path
# # # from typing import List

# # # import numpy as np
# # # import rasterio
# # # from rasterio.transform import from_origin
# # # from rasterio.crs import CRS
# # # from rasterio.warp import reproject, Resampling

# # # from .srtm_hgt import mosaic_hgt_tiles, DemMosaic
# # # from .flow_directions import d8_flow, FlowResult


# # # # ---------------------------------------------------------------------
# # # # Helpers
# # # # ---------------------------------------------------------------------
# # # def find_hgt_files(folder: str | Path) -> List[str]:
# # #     folder = Path(folder)
# # #     return [str(p) for p in folder.glob("*.hgt")]


# # # def save_geotiff(path: str | Path, data: np.ndarray, dem: DemMosaic, nodata=np.nan):
# # #     """
# # #     Save a DEM-aligned raster (2D) to GeoTIFF in EPSG:4326.
# # #     """
# # #     path = Path(path)
# # #     rows, cols = data.shape

# # #     # northern edge
# # #     lat_north = dem.lat0 + dem.cellsize_deg * (rows - 1)
# # #     lon_west = dem.lon0

# # #     transform = from_origin(
# # #         lon_west,
# # #         lat_north,
# # #         dem.cellsize_deg,
# # #         dem.cellsize_deg,
# # #     )

# # #     # choose numeric nodata
# # #     nodata_val = -9999 if np.isnan(nodata) else float(nodata)

# # #     profile = {
# # #         "driver": "GTiff",
# # #         "height": rows,
# # #         "width": cols,
# # #         "count": 1,
# # #         "dtype": str(data.dtype),
# # #         "crs": CRS.from_epsg(4326),
# # #         "transform": transform,
# # #         "nodata": nodata_val,
# # #         "compress": "LZW",
# # #     }

# # #     out = np.where(np.isnan(data), nodata_val, data).astype(profile["dtype"])

# # #     path.parent.mkdir(parents=True, exist_ok=True)
# # #     with rasterio.open(path, "w", **profile) as dst:
# # #         dst.write(out, 1)


# # # def reproject_to_3857(src_path: Path, dst_path: Path, resampling=Resampling.bilinear):
# # #     """
# # #     Reproject a raster on disk to EPSG:3857 WebMercator.
# # #     """
# # #     dst_path = Path(dst_path)
# # #     dst_path.parent.mkdir(parents=True, exist_ok=True)

# # #     with rasterio.open(src_path) as src:
# # #         # Calculate the destination transform and shape
# # #         dst_crs = CRS.from_epsg(3857)

# # #         # Rasterio can compute the transform + shape automatically
# # #         transform, width, height = rasterio.warp.calculate_default_transform(
# # #             src.crs, dst_crs, src.width, src.height, *src.bounds
# # #         )

# # #         profile = src.profile.copy()
# # #         profile.update(
# # #             {
# # #                 "crs": dst_crs,
# # #                 "transform": transform,
# # #                 "width": width,
# # #                 "height": height,
# # #                 "compress": "LZW",
# # #                 "dtype": "float32",
# # #             }
# # #         )

# # #         with rasterio.open(dst_path, "w", **profile) as dst:
# # #             reproject(
# # #                 source=rasterio.band(src, 1),
# # #                 destination=rasterio.band(dst, 1),
# # #                 src_transform=src.transform,
# # #                 src_crs=src.crs,
# # #                 dst_transform=transform,
# # #                 dst_crs=dst_crs,
# # #                 resampling=resampling,
# # #             )

# # #         print(f"  ✔ Reprojected → {dst_path}")


# # # # ---------------------------------------------------------------------
# # # # Main preprocessing pipeline
# # # # ---------------------------------------------------------------------
# # # def run_preprocessing(
# # #     elevation_dir: str | Path = "data/elevation",
# # #     output_dir: str | Path = "data/hydrology",
# # # ):
# # #     """
# # #     Complete hydrology preprocessing pipeline:

# # #       1. Load + mosaic HGT tiles (EPSG:4326)
# # #       2. Compute D8 directions + accumulation
# # #       3. Save these rasters in EPSG:4326
# # #       4. Reproject DEM + accumulation to EPSG:3857 (for tile server)
# # #     """
# # #     elevation_dir = Path(elevation_dir)
# # #     output_dir = Path(output_dir)

# # #     print(f"🌍 Searching for HGT tiles in {elevation_dir} ...")
# # #     paths = find_hgt_files(elevation_dir)
# # #     if not paths:
# # #         raise ValueError(f"No .hgt files found in {elevation_dir}")

# # #     print(f"  Found {len(paths)} tiles:")
# # #     for p in paths:
# # #         print(f"   - {p}")

# # #     print("\n🧩 Building DEM mosaic ...")
# # #     dem_mosaic: DemMosaic = mosaic_hgt_tiles(paths)
# # #     dem = dem_mosaic.data
# # #     print(f"  DEM shape: {dem.shape}")

# # #     print("\n📐 Computing D8 flow directions + accumulation ...")
# # #     flow: FlowResult = d8_flow(dem)
# # #     dirs = flow.directions.astype("int16")
# # #     accum = flow.accumulation.astype("float32")

# # #     print("\n💾 Saving WGS84 rasters (EPSG:4326) ...")
# # #     save_geotiff(output_dir / "dem.tif", dem.astype("float32"), dem_mosaic)
# # #     save_geotiff(output_dir / "d8_dirs.tif", dirs, dem_mosaic, nodata=-1)
# # #     save_geotiff(output_dir / "d8_accum.tif", accum, dem_mosaic)

# # #     print("\n🌐 Reprojecting to WebMercator (EPSG:3857) ...")
# # #     reproject_to_3857(
# # #         src_path=output_dir / "dem.tif",
# # #         dst_path=output_dir / "dem_3857.tif",
# # #         resampling=Resampling.bilinear,
# # #     )
# # #     reproject_to_3857(
# # #         src_path=output_dir / "d8_accum.tif",
# # #         dst_path=output_dir / "d8_accum_3857.tif",
# # #         resampling=Resampling.bilinear,
# # #     )

# # #     print("\n✅ Preprocessing complete.")


# # # # ---------------------------------------------------------------------
# # # # CLI
# # # # ---------------------------------------------------------------------
# # # if __name__ == "__main__":
# # #     import argparse

# # #     parser = argparse.ArgumentParser(
# # #         description="Preprocess SRTM .hgt tiles → DEM + D8 + accumulation + reprojected rasters"
# # #     )
# # #     parser.add_argument(
# # #         "--elevation",
# # #         default="data/elevation",
# # #         help="Folder containing .hgt files",
# # #     )
# # #     parser.add_argument(
# # #         "--output",
# # #         default="data/hydrology",
# # #         help="Output folder for GeoTIFFs",
# # #     )

# # #     args = parser.parse_args()
# # #     run_preprocessing(args.elevation, args.output)
