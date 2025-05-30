import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Any, Optional
import logging
import math
from datetime import datetime, timedelta
# from gfs_render import ModelService, SystemConfig, LocalStorage
from .model_service import ModelService
from .system_config import SystemConfig
from .caching.local_cache import LocalStorage
from .weather import WeatherUtils
import pickle
logger = logging.getLogger(__name__)
from .caching.cache import ICacheBackend, CACHE_TTL

class MemoryLayerCache:
    """
    In-memory cache of interpolator layers for each (param_key, hour_offset).
    Preloads using ModelService.get_or_build_interpolator and serves full 5-day forecasts in memory.
    """
    def __init__(self, model_service: ModelService, memory: ICacheBackend , preloader = False):
        self.model_service = model_service
        cfg = SystemConfig().get_default_forecast_json()
        self.model = cfg["model"]
        self.param_keys = cfg["param_keys"]        # List[dict]
        self.preload_state = preloader
        self.load_offsets()
        self.weather_utils = WeatherUtils()
        print('LOADING OFFSET VALUES', self.offsets)
        self._cache: Dict[str, Any] = {}
        self._lock = threading.Lock()
        self._loading = False
        self._init_run = True
        self._cacheStore = memory
        self._localStorage = LocalStorage()
        self._ttl = CACHE_TTL
        self._ttl_3 = self._ttl * 3


    def get_worker_count(self):
        # max_cores = 24
        cpu_count = os.cpu_count() or 4
        return cpu_count
        #return cpu_count if cpu_count < max_cores else max_cores if cpu_count > max_cores else 4
    def load_offsets(self):
        cfg = SystemConfig().get_default_forecast_json()
        self.total_days = cfg.get("total_days", 5)
        self.step_hours = cfg.get("step_hours", 3)
        self.total_hours = self.total_days * 24
        range1 = list(range(0, self.total_hours + 1, self.step_hours))
        self.offsets_primary = sorted(range1)

        if not self.preload_state:
            range2 = list(range(1, self.total_hours + 2, self.step_hours))
            self.offsets = [*range1, *range2]  # sorted(set(range1 + range2))
            return;

        self.offsets = range1;
        step_subtract = self.step_hours - 1
        step_range = math.floor((self.step_hours / 2) - 1 if self.step_hours > 3 else step_subtract)
        for i in range(step_range):
            range_values = list(range(i + 1, self.total_hours + (i + 2), self.step_hours))
            self.offsets = [*self.offsets, *range_values]
        print(f"I have these layered offsets {self.offsets}")

    def is_loading(self):
       return self._loading and self._init_run


    # def preload(self):

    #     if self._loading:
    #         return

    #     self._loading = True
    #     workers = self.get_worker_count()
    #     print(f"[MemoryLayerCache] Preloading {len(self.offsets)} offsets × {len(self.param_keys)} params using {workers} workers")
    #     with ThreadPoolExecutor(max_workers=workers) as executor:
    #         executor.map(self._preload_offset, self.offsets)

    #     self._loading = False
    #     self._init_run = False
    #     print("[MemoryLayerCache] Preload complete.")

    def _cache_get(self, key: str) -> Optional[Any]:
        cached = self._cacheStore.get(key)
        if cached is not None:
            try:
                return pickle.loads(cached)
            except Exception as e:
                logger.error(f"Error unpickling cache key {key}: {e}")
        return None

    def _cache_set(self, key: str, value: Any) -> None:
        try:
            self._cacheStore.set(key, pickle.dumps(value, protocol=4), expire=self._ttl_3)
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
                    self._cacheStore.extend(key, self._ttl_3)
                    continue
                ip = self.model_service.generate_interpolator(
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

    def _get_cached_values(self, pk: str, offset: int):
        """
        We super-charge caching, using a local memory layer with a redis-backed cache layer
        """
        key = self._get_cache_key(pk, offset)
        # if key in self._cache:
        #     return self._cache[key]
        if self._localStorage.available(key):
            self._localStorage.extend(key, self._ttl);
            return self._localStorage.get(key)
        # ip = self._cache_get(key)
        # if ip:
        #     # with self._lock:
        #     #     self._cache[key] = ip
        #     self._localStorage.set(key, ip)
            # self._cacheStore.extend(key, self._ttl_3);
        return None

    def _get_base_time(self):
        now = datetime.utcnow()
        if now.minute >= 30:
            now += timedelta(hours=1)
        base_time = now.replace(minute=0, second=0, microsecond=0)
        return base_time

    def _get_ip_with_fallback(self, pk: str, off: int):
        ip = self._get_cached_values(pk, off)
        if not ip:
            print(f"[Fetch] Missing {pk}@{off}")
            self._preload_offset(off)
            ip = self._get_cached_values(pk, off)
            if not ip:
                return None
        return ip

    def apply_extras_details(self, records: list[Any]):
        self.weather_utils.apply_extras_details(records)

    # def get_current_slice(self, lat: float, lon: float, off: int):
    #     dt = self._get_base_time() + timedelta(hours=off)
    #     records: list[Any] = []
    #     print(f"[Fetch] Computing offset={off} hour_key={off}")
    #     for entry in self.param_keys:
    #         pk = entry["param_key"]
    #         ip = self._get_ip_with_fallback(pk, off)
    #         if not ip:
    #             continue
    #         # ip = self._cache_get(key)
    #         print(f"Processing value for {pk} {off}")

    #         try:
    #             val = float(ip(lat, lon))
    #         except Exception as e:
    #             print(f"[Fetch] Error {pk}@{off}: {e}")
    #             continue
    #         records.append((pk, {"datetime": dt.isoformat(), "value": val}))
    #     # self.apply_extras_details(records)
    #     return records

    # def load_slices(self, off: int):
    #     print(f"[Fetch] Computing offset={off} hour_key={off}")
    #     for entry in self.param_keys:
    #         pk = entry["param_key"]
    #         self._get_ip_with_fallback(pk, off)
    #         return (pk,off)
    #
    def find_current_slice(self, lat: float, lon: float, hour_offset: int):
        values = []
        offset = self._find_closest_offset(hour_offset)

        def _compute_for_offset(key_value: Any):
            sendResults = {}
            try:
                key_name: str = key_value.get("param_key")
                # for off in offsets:
                key = self._get_cache_key(key_name, offset)
                # print(f'VERIFYING MY KEY {key} {self._localStorage.available(key)}')
                if not self._localStorage.available(key):
                    return None

                self._localStorage.extend(key, self._ttl)
                result = self._localStorage.get(key)

                if not result:
                    return None

                data_array, lat_array, lon_array, meta_dict = result
                if not 'units' in sendResults:
                    sendResults["unit"] = meta_dict.get("parameterUnits", "unknown")
                if not 'metadata' in sendResults:
                    sendResults["metadata"] = meta_dict
                val = self.model_service.interpolate_value(data_array, lat_array, lon_array, lat, lon)
                sendResults["value"] = val
                sendResults["datetime"] = self.model_service._build_valid_datetime_from_metadata(meta_dict, offset).isoformat()
                return sendResults
            except Exception as e:
                print(f"ERROR {e}")


        # self.get_worker_count()
        with ThreadPoolExecutor(max_workers=self.get_worker_count()) as exe:
            futures = {exe.submit(_compute_for_offset, key): key for key in self.param_keys}
            # items = {}
            for fut in as_completed(futures):
                result = fut.result()
                if not result:
                    continue
                values.append(result)
        return sorted(
            values,
            key=lambda item: item["metadata"]["key"]
        )

    def _find_closest_offset(self, offset: int):
        if offset <= 0:
            return 0
        offsets = [off for off in self.offsets_primary if off >= 0]
        if offset in offsets:
            return offset

        closest_offset = min(offsets, key=lambda x: abs(x - offset))
        return closest_offset

    def find_slice(self, lat: float, lon: float, start_hour_offset = 0):
        values = []
        offset = self._find_closest_offset(start_hour_offset)
        offsets = [off for off in self.offsets_primary if off >= 0]
        offsets = offsets[offsets.index(offset):]
        def _compute_for_offset(key_value: Any):
            sendResults = {"values" : []}
            try:
                key_name: str = key_value.get("param_key")
                for off in offsets:
                    key = self._get_cache_key(key_name, off)
                    # print(f'VERIFYING MY KEY {key} {self._localStorage.available(key)}')
                    if not self._localStorage.available(key):
                        continue

                    self._localStorage.extend(key, self._ttl)
                    result = self._localStorage.get(key)

                    if not result:
                        continue

                    data_array, lat_array, lon_array, meta_dict = result
                    if not 'units' in sendResults:
                        sendResults["unit"] = meta_dict.get("parameterUnits", "unknown")
                    if not 'metadata' in sendResults:
                        sendResults["metadata"] = meta_dict
                    val = self.model_service.interpolate_value(data_array, lat_array, lon_array, lat, lon)
                    sendResults["values"].append({
                        "value": val,
                        "datetime": self.model_service._build_valid_datetime_from_metadata(meta_dict, off).isoformat()
                    })
                return sendResults
            except Exception as e:
                print(f"ERROR {e}")


        # self.get_worker_count()
        with ThreadPoolExecutor(max_workers=self.get_worker_count()) as exe:
            futures = {exe.submit(_compute_for_offset, key): key for key in self.param_keys}
            # items = {}
            for fut in as_completed(futures):
                result = fut.result()
                if not result:
                    continue
                values.append(result)
        return sorted(
            values,
            key=lambda item: item["metadata"]["key"]
        )


    def preload_slices(self):
        if self._loading:
            return

        self._loading = True
        cache_key = self.model_service._get_grib_dict_values_key(self.model, 0)
        values = self.model_service._cache_get(cache_key) or {}
        pm = self.model_service.build_param_map_for_offset(self.model)
        print("PARAM MAP", pm)
        loaded_offsets = self.offsets if  self.preload_state  else self.offsets_primary
        offsets = [off for off in loaded_offsets if off >= 0]
        length = len(offsets)

        total_length = 0
        def _compute_for_offset(off: int):
            grbs = self.model_service._get_raw_grib(self.model, off)
            try:
                for param_key in self.param_keys:
                    key_name = param_key.get("param_key")
                    key = self._get_cache_key(key_name, off)
                    if self._localStorage.available(key):
                        self._localStorage.extend(key, self._ttl)
                        values[param_key.get("key", key_name)] = self._localStorage.get(key)
                        continue
                    param_name = pm.get(key_name)
                    if not param_name:
                        logger.warning(f"No parameter name for {param_name}")
                        continue
                    result = self.model_service._set_cached_grib_values(
                        grbs,
                        param_name,
                        self.model,
                        off,
                        param_key.get("level"),
                        param_key.get("typeOfLevel"),
                        param_key.get("stepType")
                    )
                    values[param_key.get("key", key_name)] = result
                    if not self.preload_state:
                        self._localStorage.set(key, result, self._ttl)
                # return self.load_slices(off)
            except Exception as e:
                print(f"ERROR {e}")

            return values
        # self.get_worker_count()
        workers = self.get_worker_count()
        print(f"WORKERS {workers}")
        with ThreadPoolExecutor(max_workers=workers) as exe:
            futures = {exe.submit(_compute_for_offset, off): off for off in offsets}
            for fut in as_completed(futures):
                total_length += 1
                print(f"PRELOAD EXECUTION COMPLETED {total_length} of {length}")

        self._loading = False
        self._init_run = False
        print("Preload completed")
    # def preload_to_local(self):
    #     if self._loading:
    #         return

    #     self._loading = True
    #     offsets = [off for off in self.offsets_primary if off >= 0]
    #     print(f"LOADING THESE OFFSET {offsets}")
    #     length = len(offsets) * len(self.param_keys)
    #     total_length = 0
    #     def _compute_for_offset(off: int):
    #         try:
    #             return self.load_slices(off)
    #         except Exception as e:
    #             print(f"ERROR {e}")

    #     with ThreadPoolExecutor(max_workers=self.get_worker_count()) as exe:
    #         futures = {exe.submit(_compute_for_offset, off): off for off in offsets}
    #         for fut in as_completed(futures):
    #             total_length += 1
    #             print(f"COMPLETED {fut.result()} {total_length} of {length} ")

    #     self._loading = False
    #     self._init_run = False


    # def get_slices(
    #     self,
    #     lat: float,
    #     lon: float,
    #     start_offset: int = 0
    # ) -> List[Dict[str, Any]]:
    #     """
    #     Returns a full multi-offset forecast timeseries for given geopoint.

    #     Each param_key is returned with an ordered list of (datetime, value) pairs.
    #     """
    #     # Determine base timestamp (rounded up at 30min)
    #     # now = datetime.utcnow()
    #     # if now.minute >= 30:
    #     #     now += timedelta(hours=1)
    #     # base_time = now.replace(minute=0, second=0, microsecond=0)
    #     # Filter offsets at or after start_offset
    #     offsets = [off for off in self.offsets_primary if off >= start_offset]
    #     print(f"[Fetch] Generating timeseries from offsets {offsets}")

    #     # Helper to compute one offset's slice
    #     def _compute_for_offset(off: int):
    #         # hour_key = self.model_service.todays_hour_with_date(off)
    #         # dt = base_time + timedelta(hours=off)
    #         return self.get_current_slice(lat, lon, off)

    #     # Parallel fetch
    #     final: Dict[str, List[Dict[str, Any]]] = {entry["param_key"]: [] for entry in self.param_keys}
    #     with ThreadPoolExecutor(max_workers=self.get_worker_count()) as exe:
    #         futures = {exe.submit(_compute_for_offset, off): off for off in offsets}
    #         for fut in as_completed(futures):
    #             off = futures[fut]
    #             try:
    #                 recs = fut.result()
    #             except Exception as e:
    #                 print(f"[Fetch] Offset {off} failed: {e}")
    #                 continue
    #             # Append in temporal order
    #             for pk, rec in recs:
    #                 final[pk].append(rec)

    #     # Build list of param timeseries, sorted by time
    #     result = []
    #     for pk, values in final.items():
    #         # ensure sorted (should be by offset order)
    #         values.sort(key=lambda x: x["datetime"])
    #         result.append({"param_key": pk, "values": values})
    #     return result




# import pickle
# import threading
# from concurrent.futures import ThreadPoolExecutor, as_completed
# from typing import Dict, List, Any
# from datetime import datetime, timedelta
# import logging
# import os
# from gfs_render import ModelService, SystemConfig
# from tile_renderers.gfs_render.caching.redis_cache import RedisCacheBackend
# from .caching.cache import  CACHE_TTL

# logger = logging.getLogger(__name__)




# logger = logging.getLogger(__name__)

# class MemoryLayerCache:
#     """
#     Redis-backed cache of interpolator layers for each (param_key, hour_offset).
#     Stores per-param Redis hash but fetches only needed offsets per call to minimize memory use.
#     """
#     def __init__(
#         self,
#         model_service: ModelService,
#         cache_backend: RedisCacheBackend
#     ):
#         self.model_service = model_service
#         self.redis = cache_backend

#         cfg = SystemConfig().get_default_forecast_json()
#         self.model = cfg["model"]
#         self.param_keys = cfg["param_keys"]
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
#         logger.info(f"[RedisLayerCache] Preloading params with {workers} workers")
#         with ThreadPoolExecutor(max_workers=workers) as exe:
#             exe.map(self._preload_param, self.param_keys)
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
#         logger.info(f"[Preload] Param={pk}")
#         for off in self.offsets:
#             hour_key = self.model_service.todays_hour_with_date(off)
#             if self.redis.client.hexists(hkey, hour_key):
#                 continue
#             ip = self.model_service.get_or_build_interpolator(
#                 self.model, pk, off,
#                 entry.get("level"), entry.get("typeOfLevel"), entry.get("stepType")
#             )
#             if not ip:
#                 continue
#             blob = pickle.dumps(ip, protocol=4)
#             self.redis.client.hset(hkey, hour_key, blob)
#         logger.info(f"[Preload] Completed param {pk}")

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
#             print(f"Processing param {pk}")
#             hkey = self._hash_key(pk)
#             # fetch only our desired hour_keys via HMGET
#             hour_keys = [self.model_service.todays_hour_with_date(off) for off in offsets]
#             # perform HMGET
#             print(f"Processing hour keys {hour_keys}")
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
#         print(f"Results {results}")
#         return results


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
