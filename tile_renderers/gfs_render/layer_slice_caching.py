import pickle
import os
from typing import Any, List, Dict
from concurrent.futures import ThreadPoolExecutor

import pygrib
from pyproj import Transformer

from gfs_render import ModelService, RedisCacheBackend, SystemConfig
from .interpolator import Interpolator
from .caching.cache import CACHE_TTL

class LayerSliceCaching:
    """
    Iterates individual GRIB files (one per time offset) in parallel, builds per-layer
    interpolators, and stores them in Redis under keys "slice:{model}:{param_key}:{hour_offset}".

    On a geopoint query, pulls the relevant slice and runs the cached interpolator.
    """
    def __init__(
        self,
        model_service: ModelService,
        redis_backend: RedisCacheBackend,
        decimation: int = 1,
        max_workers: int|None = None
    ):
        # Core services
        self.model_service = model_service
        self.redis = redis_backend
        self.decimation = decimation
        # Number of threads for parallel preload
        self.max_workers = max_workers or (os.cpu_count() or 1)

        # Forecast config from SystemConfig
        cfg = SystemConfig().get_default_forecast_json()
        self.model = cfg["model"]
        self.param_keys = cfg["param_keys"]        # List[dict]
        total_days = cfg.get("total_days", 5)
        step_hours = cfg.get("step_hours", 3)
        # Hour offsets to process: 0, step_hours, 2*step_hours, ..., total_days*24
        self.offsets = list(range(0, total_days * 24 + 1, step_hours))

        # Interpolator builder (projects to WebMercator internally)
        transformer = Transformer.from_crs("epsg:4326", "epsg:3857", always_xy=True)
        self.interpolator = Interpolator(transformer)

        # Pre-build & cache every layer slice in parallel
        self._preload_layers()

    def _preload_offset(self, off: int) -> None:
        # 1) Find the correct GRIB file for this offset
        grib_path = self.model_service.get_grib_file(self.model, off)
        if not grib_path:
            return

        # 2) Open and parse
        grbs = pygrib.open(grib_path)
        param_map = self.model_service.build_param_map_for_offset(self.model, off)

        # 3) For each desired param_key, build & cache its interpolator
        for entry in self.param_keys:
            pk  = entry["param_key"]
            lvl = entry.get("level")
            tof = entry.get("typeOfLevel")
            stp = entry.get("stepType")

            redis_key = self.redis_key(pk, off)
            print("I AM GETTING THIS GOING", redis_key)
            raw = self.redis.get(redis_key)
            if raw:
                continue

            param_name = param_map.get(pk)
            if not param_name:
                continue
            print("I AM RUNNING THIS HERE", pk, lvl, tof, stp, off)
            msgs = grbs.select(
                name=param_name,
                level=lvl,
                typeOfLevel=tof,
                stepType=stp
            )
            if not msgs:
                continue
            g = msgs[0]

            # 4) Extract 2D arrays
            data2d = g.values
            lat2d, lon2d = g.latlons()

            # 5) Build interpolator and cache it in Redis
            ip = self.interpolator.build_interpolator(
                data2d,
                lat2d,
                lon2d,
                lat_flip=(getattr(g, "jScansPositively", 1) == 0),
                decimation=self.decimation
            )
            redis_key = self.redis_key(pk, off)
            print('STORING THIS KEY', redis_key)
            self.redis.set(redis_key, pickle.dumps(ip), expire=CACHE_TTL)

        grbs.close()

    def _preload_layers(self) -> None:
        """
        Parallelize interpolation builds across all configured offsets.
        """
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            executor.map(self._preload_offset, self.offsets)

    def redis_key(self, pk: str, off: int) -> str:
        # Use todays_hour_with_date to ensure consistent time-based caching
        time_key = self.model_service.todays_hour_with_date(off)
        return f"slice:{self.model}:{pk}:{time_key}"

    def get_slices(
        self,
        lat: float,
        lon: float,
        hour_offset: int
    ) -> List[Dict[str, Any]]:
        """
        For a single geopoint + offset, fetch each cached slice-Interpolator from Redis,
        run ip(lat, lon), and return a list of {param_key, value}.
        """
        results: List[Dict[str, Any]] = []
        for entry in self.param_keys:
            pk = entry["param_key"]
            redis_key = self.redis_key(pk, hour_offset)
            print("I AM RUNNING THIS TO CHANGE MY LIFE", redis_key)
            raw = self.redis.get(redis_key)
            print("I AM RAW")
            if not raw:
                continue
            ip = pickle.loads(raw)
            print("I AM LOADED")
            results.append({
                "param_key": pk,
                "value": float(ip(lat, lon))
            })
            print("I am a result")
        return results

    def refresh(self) -> None:
        """
        Clears all cached slices and re-runs the preload over current offsets.
        Call after new GRIB files are available.
        """
        # for off in self.offsets:
        #     for entry in self.param_keys:
        #         key = self.redis_key(entry['param_key'], off)
        #         self.redis.delete(key)
        self._preload_layers()
