import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Tuple, List, Any, Optional
import logging
from datetime import datetime, timedelta
from gfs_render import ModelService, SystemConfig, RedisCacheBackend
import pickle
logger = logging.getLogger(__name__)
from .caching.cache import CACHE_TTL

class MemoryLayerCache:
    """
    In-memory cache of interpolator layers for each (param_key, hour_offset).
    Preloads using ModelService.get_or_build_interpolator and serves full 5-day forecasts in memory.
    """
    def __init__(self, model_service: ModelService, memory: RedisCacheBackend):
        self.model_service = model_service
        cfg = SystemConfig().get_default_forecast_json()
        self.model = cfg["model"]
        self.param_keys = cfg["param_keys"]        # List[dict]
        total_days = cfg.get("total_days", 5)
        step_hours = cfg.get("step_hours", 3)
        # Offsets: 0, step_hours, 2*step_hours, ..., total_days*24
        self.offsets = list(range(0, total_days * 24 + 1, step_hours))
        print('DOING THESE OFFSETS', self.offsets)
        # Key: (param_key, hour_key) -> interpolator instance
        self._cache: Dict[Tuple[str, str], Any] = {}
        self._lock = threading.Lock()
        self._loading = False
        self._init_run = True
        self._cacheStore = memory

        # Preload all layers in parallel
        # workers = os.cpu_count() or 4
        # print(f"[MemoryLayerCache] Preloading {len(self.offsets)} offsets × {len(self.param_keys)} params using {workers} workers")
        # with ThreadPoolExecutor(max_workers=workers) as executor:
        #     executor.map(self._preload_offset, self.offsets)
        # print("[MemoryLayerCache] Preload complete.")
        #

    def isLoading(self):
       return self._loading and self._init_run

    def loadOffset(self):

        if self._loading:
            return

        self._loading = True
        workers = os.cpu_count() or 4
        print(f"[MemoryLayerCache] Preloading {len(self.offsets)} offsets × {len(self.param_keys)} params using {workers} workers")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            executor.map(self._preload_offset, self.offsets)

        self._loading = False
        self._init_run = False
        print("[MemoryLayerCache] Preload complete.")

    def _cache_get(self, key: str) -> Optional[Any]:
        cached = self._cacheStore.get(key)
        if cached is not None:
            try:
                return pickle.loads(cached)
            except Exception as e:
                logger.error(f"Error unpickling cache key {key}: {e}")
        return None

    def _cache_set(self, key: str, value: Any, expire: int = CACHE_TTL) -> None:
        try:
            self._cacheStore.set(key, pickle.dumps(value, protocol=4), expire=expire)
        except Exception as e:
            logger.error(f"Error setting cache key {key}: {e}")

    def _get_cache_key(self, param_key: str, offset: int) -> str:
        hour_key = self.model_service.todays_hour_with_date(offset)
        return f"layer_cache:{param_key}@{hour_key}"

    def _preload_offset(self, off: int) -> None:
        # hour_key = self.model_service.todays_hour_with_date(off)
        print(f"[Preload] offset={off} -> hour_key={off}")
        for entry in self.param_keys:
            pk = entry["param_key"]
            lvl = entry.get("level")
            tof = entry.get("typeOfLevel")
            stp = entry.get("stepType")
            key = self._get_cache_key(pk, off)
            try:
                available = self._cacheStore.available(key)
                print(f"[Preload] Building interpolator for {key} {available}")
                if available:
                    continue
                ip = self.model_service.get_or_build_interpolator(
                    self.model, pk, off, lvl, tof, stp
                )
                if not ip:
                    print(f"[Preload] Skipped {pk}@{off} (no data)")
                    continue
                print(f"[Preload] Setting cache key {key}")
                self._cache_set(key, ip)
            except Exception as e:
                logger.error(f"Error setting cache key {key}: {e}")
            # with self._lock:
            #     self._cache[(pk, hour_key)] = ip
        print(f"[Preload] Completed offset {off}")

    def get_slices(
        self,
        lat: float,
        lon: float,
        start_offset: int = 0
    ) -> List[Dict[str, Any]]:
        """
        Returns a full multi-offset forecast timeseries for given geopoint.

        Each param_key is returned with an ordered list of (datetime, value) pairs.
        """
        # Determine base timestamp (rounded up at 30min)
        now = datetime.utcnow()
        if now.minute >= 30:
            now += timedelta(hours=1)
        base_time = now.replace(minute=0, second=0, microsecond=0)
        # Filter offsets at or after start_offset
        offsets = [off for off in self.offsets if off >= start_offset]
        print(f"[Fetch] Generating timeseries from offsets {offsets}")

        # Helper to compute one offset's slice
        def _compute_for_offset(off: int):
            # hour_key = self.model_service.todays_hour_with_date(off)
            dt = base_time + timedelta(hours=off)
            records = []
            print(f"[Fetch] Computing offset={off} hour_key={off}")
            for entry in self.param_keys:
                pk = entry["param_key"]
                key = self._get_cache_key(pk, off)
                ip = self._cache_get(key)
                if not ip:
                    print(f"[Fetch] Missing {pk}@{off}")
                    continue
                try:
                    val = float(ip(lat, lon))
                except Exception as e:
                    print(f"[Fetch] Error {pk}@{off}: {e}")
                    continue
                records.append((pk, {"datetime": dt.isoformat(), "value": val}))
            return records

        # Parallel fetch
        max_workers = os.cpu_count() or 4
        final: Dict[str, List[Dict[str, Any]]] = {entry["param_key"]: [] for entry in self.param_keys}
        with ThreadPoolExecutor(max_workers=max_workers) as exe:
            futures = {exe.submit(_compute_for_offset, off): off for off in offsets}
            for fut in as_completed(futures):
                off = futures[fut]
                try:
                    recs = fut.result()
                except Exception as e:
                    print(f"[Fetch] Offset {off} failed: {e}")
                    continue
                # Append in temporal order
                for pk, rec in recs:
                    final[pk].append(rec)

        # Build list of param timeseries, sorted by time
        result = []
        for pk, values in final.items():
            # ensure sorted (should be by offset order)
            values.sort(key=lambda x: x["datetime"])
            result.append({"param_key": pk, "values": values})
        return result
