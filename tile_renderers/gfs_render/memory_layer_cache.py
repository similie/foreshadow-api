import pickle
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Any
from datetime import datetime, timedelta
import logging
import os
from gfs_render import ModelService, SystemConfig
from tile_renderers.gfs_render.caching.redis_cache import RedisCacheBackend
from .caching.cache import  CACHE_TTL

logger = logging.getLogger(__name__)




logger = logging.getLogger(__name__)

class MemoryLayerCache:
    """
    Redis-backed cache of interpolator layers for each (param_key, hour_offset).
    Stores per-param Redis hash but fetches only needed offsets per call to minimize memory use.
    """
    def __init__(
        self,
        model_service: ModelService,
        cache_backend: RedisCacheBackend
    ):
        self.model_service = model_service
        self.redis = cache_backend

        cfg = SystemConfig().get_default_forecast_json()
        self.model = cfg["model"]
        self.param_keys = cfg["param_keys"]
        total_days = cfg.get("total_days", 5)
        step_hours = cfg.get("step_hours", 3)
        self.offsets = list(range(0, total_days * 24 + 1, step_hours))

        self._lock = threading.Lock()
        self._loading = False
        self._init_run = True

    def is_loading(self) -> bool:
        return self._loading and self._init_run

    def _hash_key(self, param_key: str) -> str:
        return f"layer_map:{self.model}:{param_key}"

    def preload(self) -> None:
        """
        Precompute and store every (param_key, offset) interpolator in its Redis hash.
        """
        if self._loading:
            return
        self._loading = True
        workers = os.cpu_count() or 4
        logger.info(f"[RedisLayerCache] Preloading params with {workers} workers")
        with ThreadPoolExecutor(max_workers=workers) as exe:
            exe.map(self._preload_param, self.param_keys)
        # Set TTL on each hash
        for entry in self.param_keys:
            hkey = self._hash_key(entry["param_key"])
            try:
                self.redis.client.expire(hkey, CACHE_TTL)
            except Exception:
                pass
        self._loading = False
        self._init_run = False
        logger.info("[RedisLayerCache] Preload complete.")

    def _preload_param(self, entry: Dict[str, Any]) -> None:
        pk = entry["param_key"]
        hkey = self._hash_key(pk)
        logger.info(f"[Preload] Param={pk}")
        for off in self.offsets:
            hour_key = self.model_service.todays_hour_with_date(off)
            if self.redis.client.hexists(hkey, hour_key):
                continue
            ip = self.model_service.get_or_build_interpolator(
                self.model, pk, off,
                entry.get("level"), entry.get("typeOfLevel"), entry.get("stepType")
            )
            if not ip:
                continue
            blob = pickle.dumps(ip, protocol=4)
            self.redis.client.hset(hkey, hour_key, blob)
        logger.info(f"[Preload] Completed param {pk}")

    def get_slices(
        self,
        lat: float,
        lon: float,
        start_offset: int = 0
    ) -> List[Dict[str, Any]]:
        """
        Returns multi-offset timeseries for each param by fetching only needed offsets per param.
        """
        now = datetime.utcnow()
        if now.minute >= 30:
            now += timedelta(hours=1)
        base_time = now.replace(minute=0, second=0, microsecond=0)
        offsets = [off for off in self.offsets if off >= start_offset]
        logger.info(f"[Fetch] offsets {offsets}")

        def process_param(entry: Dict[str, Any]) -> Dict[str, Any]:
            pk = entry["param_key"]
            print(f"Processing param {pk}")
            hkey = self._hash_key(pk)
            # fetch only our desired hour_keys via HMGET
            hour_keys = [self.model_service.todays_hour_with_date(off) for off in offsets]
            # perform HMGET
            print(f"Processing hour keys {hour_keys}")
            raw_list = self.redis.client.hmget(hkey, hour_keys)
            values = []
            for off, raw in zip(offsets, raw_list):
                if raw is None:
                    continue
                try:
                    ip = pickle.loads(raw)
                    val = float(ip(lat, lon))
                    dt = base_time + timedelta(hours=off)
                    values.append({"datetime": dt.isoformat(), "value": val})
                except Exception as e:
                    logger.error(f"Error for {pk}@{off}: {e}")
                    continue
            # sort by datetime just in case
            values.sort(key=lambda x: x["datetime"])
            return {"param_key": pk, "values": values}

        # parallelize per-param to utilize threads
        results = []
        workers = min(len(self.param_keys), os.cpu_count() or 4)
        with ThreadPoolExecutor(max_workers=workers) as exe:
            futures = {exe.submit(process_param, entry): entry for entry in self.param_keys}
            for fut in as_completed(futures):
                try:
                    results.append(fut.result())
                except Exception as e:
                    logger.error(f"Error processing param: {e}")
        print(f"Results {results}")
        return results


# class MemoryLayerCache:
#     """
#     Redis-backed cache of interpolator layers for each (param_key, hour_offset).
#     Stores each param's full offset map in a Redis hash to minimize round-trips.
#     """
#     def __init__(
#         self,
#         model_service: ModelService,
#         cache_backend: RedisCacheBackend
#     ):
#         self.model_service = model_service
#         self.redis = cache_backend  # must expose .client for hash ops

#         cfg = SystemConfig().get_default_forecast_json()
#         self.model = cfg["model"]
#         self.param_keys = cfg["param_keys"]   # List[dict]
#         total_days = cfg.get("total_days", 5)
#         step_hours = cfg.get("step_hours", 3)
#         self.offsets = list(range(0, total_days * 24 + 1, step_hours))

#         self._lock = threading.Lock()
#         self._loading = False
#         self._init_run = True

#     def is_loading(self) -> bool:
#         return self._loading and self._init_run

#     def _hash_key(self, param_key: str) -> str:
#         return f"layer_map:{self.model}:{param_key}"

#     def preload(self) -> None:
#         """
#         Precompute and store every (param_key, offset) interpolator in its Redis hash.
#         """
#         if self._loading:
#             return
#         self._loading = True

#         workers = os.cpu_count() or 4
#         logger.info(f"[RedisLayerCache] Preloading {len(self.offsets)} offsets × {len(self.param_keys)} params using {workers} workers")
#         with ThreadPoolExecutor(max_workers=workers) as executor:
#             executor.map(self._preload_param, self.param_keys)

#         # Set TTL on each hash
#         for entry in self.param_keys:
#             hkey = self._hash_key(entry["param_key"])
#             try:
#                 self.redis.client.expire(hkey, CACHE_TTL)
#             except Exception:
#                 pass

#         self._loading = False
#         self._init_run = False
#         logger.info("[RedisLayerCache] Preload complete.")

#     def _preload_param(self, entry: Dict[str, Any]) -> None:
#         pk = entry["param_key"]
#         hkey = self._hash_key(pk)
#         logger.info(f"[Preload] Param={pk} into hash {hkey}")
#         for off in self.offsets:
#             hour_key = self.model_service.todays_hour_with_date(off)
#             # skip if already exists
#             if self.redis.client.hexists(hkey, hour_key):
#                 continue
#             logger.debug(f"[Preload] Building interpolator for {pk}@{hour_key}")
#             ip = self.model_service.get_or_build_interpolator(
#                 self.model, pk, off,
#                 entry.get("level"), entry.get("typeOfLevel"), entry.get("stepType")
#             )
#             if not ip:
#                 logger.warning(f"[Preload] No data for {pk}@{hour_key}")
#                 continue
#             blob = pickle.dumps(ip, protocol=4)
#             self.redis.client.hset(hkey, hour_key, blob)
#         logger.info(f"[Preload] Completed hash {hkey}")

#     def get_slices(
#         self,
#         lat: float,
#         lon: float,
#         start_offset: int = 0
#     ) -> List[Dict[str, Any]]:
#         """
#         Returns multi-offset timeseries for each param by fetching only needed offsets per param.
#         """
#         now = datetime.utcnow()
#         if now.minute >= 30:
#             now += timedelta(hours=1)
#         base_time = now.replace(minute=0, second=0, microsecond=0)
#         offsets = [off for off in self.offsets if off >= start_offset]
#         logger.info(f"[Fetch] offsets {offsets}")

#         def process_param(entry: Dict[str, Any]) -> Dict[str, Any]:
#             pk = entry["param_key"]
#             hkey = self._hash_key(pk)
#             # fetch only our desired hour_keys via HMGET
#             hour_keys = [self.model_service.todays_hour_with_date(off) for off in offsets]
#             # perform HMGET
#             raw_list = self.redis.client.hmget(hkey, hour_keys)
#             values = []
#             for off, raw in zip(offsets, raw_list):
#                 if raw is None:
#                     continue
#                 try:
#                     ip = pickle.loads(raw)
#                     val = float(ip(lat, lon))
#                     dt = base_time + timedelta(hours=off)
#                     values.append({"datetime": dt.isoformat(), "value": val})
#                 except Exception as e:
#                     logger.error(f"Error for {pk}@{off}: {e}")
#                     continue
#             # sort by datetime just in case
#             values.sort(key=lambda x: x["datetime"])
#             return {"param_key": pk, "values": values}

#         # parallelize per-param to utilize threads
#         results = []
#         workers = min(len(self.param_keys), os.cpu_count() or 4)
#         with ThreadPoolExecutor(max_workers=workers) as exe:
#             futures = {exe.submit(process_param, entry): entry for entry in self.param_keys}
#             for fut in as_completed(futures):
#                 try:
#                     results.append(fut.result())
#                 except Exception as e:
#                     logger.error(f"Error processing param: {e}")
#         return results
