import numpy as np
from numba import njit
from typing import List, Dict, Any, Tuple, Optional
from gfs_render import ModelService, RedisCacheBackend, SystemConfig
from apscheduler.schedulers.asyncio import AsyncIOScheduler

@njit
def bilinear_interpolate(
    lat: float,
    lon: float,
    data: np.ndarray,
    lats: np.ndarray,
    lons: np.ndarray
) -> float:
    # Find insertion indices
    i = np.searchsorted(lats, lat)
    j = np.searchsorted(lons, lon)
    # Clamp to valid range
    i = 1 if i <= 0 else (lats.size - 1 if i >= lats.size else i)
    j = 1 if j <= 0 else (lons.size - 1 if j >= lons.size else j)
    # Corner values
    y0, y1 = lats[i - 1], lats[i]
    x0, x1 = lons[j - 1], lons[j]
    f00 = data[i - 1, j - 1]
    f10 = data[i,     j - 1]
    f01 = data[i - 1, j]
    f11 = data[i,     j]
    # Weight fractions
    t = (lat - y0) / (y1 - y0)
    u = (lon - x0) / (x1 - x0)
    return (1 - t) * (1 - u) * f00 + t * (1 - u) * f10 + (1 - t) * u * f01 + t * u * f11

class NumGridCacheService:
    """
    Preload all surface-level grids into a single 3D array per parameter,
    then serve forecast time-series via fast in-memory slicing.
    """
    def __init__(self):
        cache_backend = RedisCacheBackend()
        self.model_service = ModelService(cache_backend)

        cfg = SystemConfig().get_default_forecast_json()
        self.model = cfg["model"]
        self.param_entries = cfg["param_keys"]  # List[dict]
        self.total_days = cfg.get("total_days", 5)
        self.step_hours = cfg.get("step_hours", 3)
        # time offsets: 0, step_hours, 2*step_hours, ..., total_days*24
        self.offsets = list(range(0, self.total_days * 24 + 1, self.step_hours))

        # grid_store: param_key -> (data_3d: [time, lat, lon], lats1d, lons1d)
        self._grid_store: Dict[str, Tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
        self._preload_all_layers()

    def _preload_all_layers(self) -> None:
        # Temporary container: param_key -> list of 2D grids
        temp: Dict[str, List[np.ndarray]] = {e["param_key"]: [] for e in self.param_entries}

        for off in self.offsets:
            fp = self.model_service.get_grib_file(self.model, off)
            if not fp:
                continue
            grbs = self.model_service._get_raw_grib(self.model, off)
            pm = self.model_service.build_param_map_for_offset(self.model)

            # Extract each parameter's 2D grid at this offset
            for entry in self.param_entries:
                pk = entry["param_key"]
                lvl = entry.get("level")
                tof = entry.get("typeOfLevel")
                stp = entry.get("stepType")

                param_name = pm.get(pk)
                if not param_name:
                    continue

                grb_msg = self.model_service._select_grib_message(
                    grbs, param_name, lvl, tof, stp
                )
                if grb_msg is None:
                    continue

                data2d = grb_msg.values
                temp[pk].append(data2d)

                # On first entry, capture 1D axes
                if off == self.offsets[0] and pk == self.param_entries[0]["param_key"]:
                    lat2d, lon2d = grb_msg.latlons()
                    self.lats1d = lat2d[:, 0]
                    self.lons1d = lon2d[0, :]

            grbs.close()

        # Stack each parameter's time slices into a 3D array
        for pk, grid_list in temp.items():
            if not grid_list:
                continue
            data_3d = np.stack(grid_list, axis=0)  # shape: [ntime, nlat, nlon]
            self._grid_store[pk] = (data_3d, self.lats1d, self.lons1d)

    def get_point_forecast(
        self,
        lat: float,
        lon: float,
        start_hour_offset: int = 0
    ) -> List[Dict[str, Any]]:
        """
        Returns a multi-hour time series for each default parameter.
        Uses a single C-level slice for nearest-neighbor:

            # find grid indices (500 ns)
            lat_idx = np.searchsorted(lats1d, lat)
            lon_idx = np.searchsorted(lons1d, lon)
            # slice across time in pure C
            series = data_3d[:, lat_idx, lon_idx]

        Or, if you prefer bilinear, loop with the JIT'd kernel.
        """
        # Determine which index in `self.offsets` matches the start offset
        try:
            idx0 = self.offsets.index(start_hour_offset)
        except ValueError:
            idx0 = 0

        results: List[Dict[str, Any]] = []
        for pk, (data_3d, lats1d, lons1d) in self._grid_store.items():
            # Nearest-neighbor slice (microseconds)
            lat_idx = np.searchsorted(lats1d, lat)
            lon_idx = np.searchsorted(lons1d, lon)
            # clamp
            lat_idx = max(0, min(lat_idx, lats1d.size - 1))
            lon_idx = max(0, min(lon_idx, lons1d.size - 1))

            # full series across all offsets
            series = data_3d[:, lat_idx, lon_idx]
            # subset from the requested start
            series = series[idx0: idx0 + len(self.offsets)]

            results.append({
                "param_key": pk,
                "values": series.tolist()
            })

        return results

    def refresh(self) -> None:
        """Clear and rebuild all in-memory grids (e.g. when new GRIBs arrive)."""
        self._grid_store.clear()
        self._preload_all_layers()


class LayerCacheService:
    """
    Caches and periodically pre-warms interpolator layers (model/param/time/level/type/step).
    """
    def __init__(
        self,
        model_service: Any,
        model: str,
        param_keys: List[Dict[str, Any]],
        ttl_s: int = 600,
        refresh_interval_s: int = 600,
    ):
        # Underlying ModelService that exposes get_or_build_interpolator()
        self.model_service = model_service
        # Forecast model name (e.g. "gfs")
        self.model = model
        # List of dicts: each has "param_key", "level", "typeOfLevel", "stepType"
        self.param_keys = param_keys
        # InterpolatorCachingService from ModelService (debounced Redis writes)
        self.cache = model_service.interpolator_cache
        # Time-to-live for each layer in seconds (passed through to cache writes)
        self.ttl = ttl_s
        # How often to refresh all layers in the background
        self._refresh_interval = refresh_interval_s
        # APScheduler for running the refresh job
        self._scheduler = AsyncIOScheduler()

    def _make_layer_key(
        self,
        param_key: str,
        hour_offset: int,
        level: Optional[int],
        type_of_level: Optional[str],
        step_type: Optional[str],
    ) -> str:
        # Build the exact cache key used by the InterpolatorCachingService
        return self.model_service.get_interpolator_cache_key(
            self.model,
            param_key,
            hour_offset,
            level,
            type_of_level,
            step_type,
        )

    def get_layer(
        self,
        param_key: str,
        hour_offset: int,
        level: Optional[int],
        type_of_level: Optional[str],
        step_type: Optional[str],
    ) -> Optional[Any]:
        """
        Ensures the interpolator for this layer is built and cached.
        Returns the Interpolator instance (or None if unavailable).
        """
        return self.model_service.get_or_build_interpolator(
            model=self.model,
            param_key=param_key,
            hour_offset=hour_offset,
            level=level,
            level_type=type_of_level,
            step_type=step_type,
        )

    def _prewarm_all_layers(self) -> None:
        """
        Loops through the configured param_keys and pre-builds each layer.
        """
        for entry in self.param_keys:
            pk    = entry["param_key"]
            lvl   = entry.get("level")
            tof   = entry.get("typeOfLevel")
            stp   = entry.get("stepType")
            # If your JSON includes a start offset per layer, use it; else default to 0
            h_off = entry.get("start_hour_offset", 0)
            print("HERE WE ARE", pk, h_off , lvl, tof, stp)
            # Trigger build + cache-write via get_layer()
            self.get_layer(pk, h_off, lvl, tof, stp)

    def start_refresh_scheduler(self) -> None:
        """
        Starts the background job that refreshes all layers at regular intervals.
        Call this once (e.g. in @app.on_event("startup")).
        """
        self._scheduler.add_job(
            self._prewarm_all_layers,
            trigger="interval",
            seconds=self._refresh_interval,
            id="layer_refresh",
            replace_existing=True,
        )
        self._scheduler.start()
