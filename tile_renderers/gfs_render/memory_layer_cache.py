import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Any, Optional
import logging
from datetime import datetime, timedelta, timezone
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
        self._cache: Dict[str, Any] = {}
        self._lock = threading.Lock()
        self._loading = False
        self._init_run = True
        self._cacheStore = memory
        self._localStorage = LocalStorage()
        self._ttl = CACHE_TTL
        self._ttl_3 = self._ttl * 3
        self._ttl_local = self._ttl + (11 + 60)


    def get_worker_count(self):
        # max_cores = 24
        cpu_count = os.cpu_count() or 4
        return cpu_count
        #return cpu_count if cpu_count < max_cores else max_cores if cpu_count > max_cores else 4
    def _append_midnight_indices(self,range_hours: List[int]):
        max_hour = range_hours[-1]
        midnight_indices = self.generate_midnight_indices(max_hour)
        for index in midnight_indices:
            if index not in range_hours:
                range_hours.append(index)
        return midnight_indices

    def load_offsets(self):
        cfg = SystemConfig().get_default_forecast_json()
        self.total_days = cfg.get("total_days", 5)
        self.step_hours = cfg.get("step_hours", 3)
        self.total_hours = self.total_days * 24
        max_hour = self.total_hours + 1
        range1 = list(range(0, max_hour, self.step_hours))

        self.offsets_primary = sorted(range1)

        if not self.preload_state:
            range2 = list(range(1, max_hour + 1, self.step_hours))
            self.offsets = [*range1, *range2]  # sorted(set(range1 + range2))
            return self._append_midnight_indices(self.offsets);

        self.offsets = range1;
        step_subtract = self.step_hours - 1
        step_range = step_subtract
        for i in range(step_range):
            range_values = list(range(i + 1, self.total_hours + (i + 2), self.step_hours))
            self.offsets = [*self.offsets, *range_values]

        self._append_midnight_indices(self.offsets)
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

    def _get_cached_values(self, key_value: dict[str, Any], offset: int ):
        """
        We super-charge caching, using a local memory layer with a redis-backed cache layer
        """
        if key_value is None:
            return None
        key_name = key_value.get("param_key", None)
        # print("I HAVE THIS HERE", key_name)
        if key_name is None:
            return None
        key = self._get_cache_key(key_name, offset)
        # look for it in local storage
        # print("going local", key)
        if self._localStorage.available(key):
            self._localStorage.extend(key, self._ttl_local);
            return self._localStorage.get(key)
        # try to find it in longer-term storage
        # cached_result = self._cacheStore.get(key)
        # if cached_result is not None:
        #     # self._cacheStore.extend(key, self._ttl_3)
        #     self._localStorage.set(key, cached_result, self._ttl_local)
        #     return cached_result
        # otherwise, we pull it
        grbs = self.model_service._get_raw_grib(self.model, offset)
        result = self.model_service._set_cached_grib_values(
            grbs,
            key_name,
            self.model,
            offset,
            key_value.get("level"),
            key_value.get("typeOfLevel"),
            key_value.get("stepType")
        )
        # print("I HAVE a new result here",result, key_name)
        # self._cacheStore.set(key, result, self._ttl_3)
        self._localStorage.set(key, result, self._ttl_local)
        return result

    def _get_base_time(self):
        now = datetime.utcnow()
        if now.minute >= 30:
            now += timedelta(hours=1)
        base_time = now.replace(minute=0, second=0, microsecond=0)
        return base_time

    # def _get_ip_with_fallback(self, pk: str, off: int):
    #     ip = self._get_cached_values(pk, off)
    #     if not ip:
    #         print(f"[Fetch] Missing {pk}@{off}")
    #         self._preload_offset(off)
    #         ip = self._get_cached_values(pk, off)
    #         if not ip:
    #             return None
    #     return ip

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
                # key_name: str = key_value.get("param_key")
                # for off in offsets:
                # key = self._get_cache_key(key_name, offset)
                result = self._get_cached_values(key_value, offset)
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
            cfg = SystemConfig().get_default_forecast_json()
            param_keys = cfg["param_keys"]
            futures = {exe.submit(_compute_for_offset, key): key for key in param_keys}
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

    def _append_start_end(self, sendResults: Dict[str, Any], forecast_time: int) -> None:
        # 1. Tell Pyright that sendResults is Dict[str, Any]
        # sendResults: Dict[str, Any] = {}
        # # Later you populate sendResults["metadata"] = { ... }
        # sendResults["metadata"] = {"forecastStart": 999, "forecastEnd": 0, ...}

        # forecast_time = ...  # some integer or float
        meta = sendResults["metadata"]  # Pyright now knows `meta` is Dict[str, Any]
        # because we've annotated sendResults above.

        if meta["forecastStart"] > forecast_time:
            meta["forecastStart"] = forecast_time

        if meta["forecastEnd"] < forecast_time:
            meta["forecastEnd"] = forecast_time

    def find_slice(self, lat: float, lon: float, start_hour_offset = 0):
        values = []
        offset = self._find_closest_offset(start_hour_offset)
        offsets = [off for off in self.offsets_primary if off >= 0]
        offsets = offsets[offsets.index(offset):]
        def _compute_for_offset(key_value: Any):
            sendResults = {"values" : []}
            try:
                for off in offsets:
                    result = self._get_cached_values(key_value, off)
                    if not result:
                        continue
                    data_array, lat_array, lon_array, meta_dict = result
                    forecast_time = meta_dict.get("forecastTime", off)
                    # if not 'units' in sendResults:
                    #     sendResults["unit"] = meta_dict.get("parameterUnits", "unknown")
                    if not 'metadata' in sendResults:
                        sendResults["metadata"] = meta_dict.copy()
                        del sendResults["metadata"]["forecastTime"]
                        sendResults["metadata"]["forecastStart"] = forecast_time
                        sendResults["metadata"]["forecastEnd"] = forecast_time
                    val = self.model_service.interpolate_value(data_array, lat_array, lon_array, lat, lon)
                    sendResults["values"].append({
                        "value": val,
                        "datetime": self.model_service._build_valid_datetime_from_metadata(meta_dict, off).isoformat()
                    })
                    self._append_start_end(sendResults, forecast_time)

                return sendResults
            except Exception as e:
                print(f"ERROR {e}")


        # self.get_worker_count()
        with ThreadPoolExecutor(max_workers=self.get_worker_count()) as exe:
            cfg = SystemConfig().get_default_forecast_json()
            param_keys = cfg["param_keys"]
            futures = {exe.submit(_compute_for_offset, key): key for key in param_keys}
            # items = {}
            for fut in as_completed(futures):
                result = fut.result()
                if not result:
                    continue
                values.append(result)
        # sort each parameter‐block by datetime
        for block in values:
            block["values"].sort(key=lambda x: x["datetime"])

        return self.append_special_slices_values(sorted(
            values,
            key=lambda item: item["metadata"]["key"]
        ), lat, lon)

    def append_special_slices_values(
        self,
        slice_results: List[Dict[str, Any]],
        lat: float,
        lon: float) -> List[Dict[str, Any]]:
        # self.normalize_precipitation_24h(slice_results, lat, lon)
        # self.weather_utils.apply_extras_details(slice_results, lat, lon)
        return slice_results


    def normalize_precipitation_24h(
            self,
            slice_results: List[Dict[str, Any]],
            lat: float,
            lon: float
        ) -> None:
            """
            In-place, converts each “total-precipitation” block’s cumulative values
            into 24 h deltas that reset at midnight of each calendar day.

            Algorithm:
            1. Find the “total-precipitation” definition in param_keys.
            2. Build run_init = datetime(dataDate, dataTime) from metadata.
            3. For each record rec in block["values"]:
            a. Parse rec_dt = datetime.fromisoformat(rec["datetime"]).
            b. If rec_date not in midnight_cache:
                    i. Compute hours_since_run = (rec_dt - run_init).hours
                ii. midnight_offset = floor(hours_since_run / 24) * 24
                iii. Fetch cumulative at midnight_offset (mid_cum).
                iv. Fetch cumulative at (midnight_offset – 1) if ≥ 0 (prev_cum).
                    v. midnight_cache[rec_date] = mid_cum – prev_cum.
            c. delta = rec["value"] – midnight_cache[rec_date]
            d. Append {"value": round(delta,3), "datetime": rec["datetime"]}.
            4. Overwrite block["values"] with these deltas.
            """
            cfg = SystemConfig().get_default_forecast_json()
            param_keys = cfg["param_keys"]

            # 1) Find “total-precipitation” in param_keys (fallback if not found)
            tp_def = next(
                (p for p in param_keys if p.get("param_key") == "total-precipitation"),
                None
            )
            if tp_def is None:
                tp_def = {
                    "param_key": "total-precipitation",
                    "level": 0,
                    "typeOfLevel": "surface",
                    "stepType": "accum"
                }

            def get_previous_hour_cumulative(prev_offset: int) -> float:
                """
                Return the interpolated “total-precipitation” at prev_offset hours from run,
                or 0.0 if not found.
                """
                prev_val = 0.0
                four_tuple = self._get_cached_values(tp_def, prev_offset)
                if four_tuple is not None:
                    data_arr, lat_arr, lon_arr, _ = four_tuple
                    prev_val = self.model_service.interpolate_value(
                        data_arr, lat_arr, lon_arr, lat, lon
                    )
                return prev_val

            # 2) Loop over each block in slice_results
            for block in slice_results:
                if block["metadata"].get("key") != "total-precipitation":
                    continue

                # a) Build run_init from metadata
                meta = block["metadata"]
                data_date = meta.get("dataDate")    # e.g. 20250531
                data_time = meta.get("dataTime", 0) # e.g. 0, 6, 12, or 18
                if data_date is None:
                    continue

                ds = str(data_date)
                run_init = datetime(
                    int(ds[:4]), int(ds[4:6]), int(ds[6:8]),
                    data_time, 0, 0, tzinfo=timezone.utc
                )

                # b) Keep a cache: { date → midnight_base }
                midnight_cache: Dict[Any, float] = {}
                new_values: List[Dict[str, Any]] = []

                for rec in block["values"]:
                    rec_iso = rec["datetime"]
                    cum_val = rec["value"]

                    # i) Parse rec_dt
                    rec_dt = datetime.fromisoformat(rec_iso)
                    if rec_dt.tzinfo is None:
                        rec_dt = rec_dt.replace(tzinfo=timezone.utc)
                    rec_date = rec_dt.date()

                    # ii) If we haven't computed this date's midnight yet:
                    if rec_date not in midnight_cache:
                        # 1) Compute how many hours from run_init to rec_dt
                        hours_since_run = int((rec_dt - run_init).total_seconds() // 3600)

                        # 2) midnight_offset = largest multiple of 24 ≤ hours_since_run
                        midnight_offset = (hours_since_run // 24) * 24
                        prev_offset = midnight_offset - 1

                        # 3) Fetch cumulative at midnight_offset
                        mid_cum = 0.0
                        midnight_tuple = self._get_cached_values(tp_def, midnight_offset)
                        if midnight_tuple is not None:
                            data_mid, lat_mid, lon_mid, _ = midnight_tuple
                            mid_cum = self.model_service.interpolate_value(
                                data_mid, lat_mid, lon_mid, lat, lon
                            )

                        # 4) Fetch cumulative at the hour before midnight
                        prev_cum = 0.0
                        if prev_offset >= 0:
                            prev_cum = get_previous_hour_cumulative(prev_offset)

                        # 5) Store the true “midnight base” for that date:
                        midnight_cache[rec_date] = mid_cum - prev_cum

                    # iii) Subtract that date’s midnight base
                    base = midnight_cache[rec_date]
                    delta = cum_val - base

                    new_values.append({
                        "value": round(delta, 3),
                        "datetime": rec_iso
                    })

                # 6) Replace its “values” list
                block["values"] = new_values

    # def normalize_precipitation_24h(
    #     self,
    #     slice_results: List[Dict[str, Any]],
    #     lat: float,
    #     lon: float
    # ) -> None:
    #     """
    #     Convert each total-precip block’s cumulative (accum) values into 24 h deltas.
    #     For each record:
    #         delta = cum_val(t) – cum_val(midnight_of_that_day).

    #     If the exact midnight record (“00:00 UTC”) is missing in the block, we
    #     pull it directly from cache + interpolate at (lat, lon).
    #     """
    #     cfg = SystemConfig().get_default_forecast_json()
    #     param_keys = cfg["param_keys"]

    #     # Locate the “total-precipitation” param‐dict in your config:
    #     tp_dict = next(
    #         (d for d in param_keys if d.get("param_key") == "total-precipitation"),
    #         None
    #     )
    #     kv = tp_dict or {
    #         "param_key": "total-precipitation",
    #         "level": 0,
    #         "typeOfLevel": "surface",
    #         "stepType": "accum"
    #     }

    #     def extract_previous_hour_value(prev_offset: int) -> float:
    #         """
    #         Fetch the 4‐tuple at (prev_offset), unpack all four items,
    #         interpolate at (lat, lon), return that scalar.
    #         If caching comes back None, return 0.0.
    #         """
    #         prev_items = self._get_cached_values(kv, prev_offset)
    #         if prev_items is None:
    #             return 0.0

    #         data_arr, lat_arr, lon_arr, _meta = prev_items
    #         return self.model_service.interpolate_value(data_arr, lat_arr, lon_arr, lat, lon)

    #     # Loop over each “block” (i.e. each param_key → { "values": [...], "metadata": {...} })
    #     for block in slice_results:
    #         if block["metadata"].get("key") != "total-precipitation":
    #             continue

    #         meta = block["metadata"]
    #         data_date = meta.get("dataDate")     # e.g. 20250531 (YYYYMMDD)
    #         data_time = meta.get("dataTime", 0)   # e.g. 0, 6, 12, or 18
    #         if data_date is None:
    #             # If there’s no model‐run date in the metadata, skip normalization
    #             continue

    #         # Build the model‐run init datetime (UTC) from metadata
    #         ds = str(data_date)
    #         init_dt = datetime(int(ds[:4]), int(ds[4:6]), int(ds[6:8]),
    #                         data_time, 0, 0, tzinfo=timezone.utc)

    #         midnight_cache: Dict[str, float] = {}
    #         new_values: List[Dict[str, Any]] = []

    #         for rec in block["values"]:
    #             # 1) Parse the record’s own “datetime” field—never use metadata for this
    #             rec_dt = datetime.fromisoformat(rec["datetime"])
    #             print('WE HAVE THIS DT', rec_dt)
    #             if rec_dt.tzinfo is None:
    #                 rec_dt = rec_dt.replace(tzinfo=timezone.utc)

    #             cum_val = rec["value"]

    #             # 2) Compute that record’s “offset hours since init run”:
    #             offset_secs = (rec_dt - init_dt).total_seconds()
    #             offset_hours = int(offset_secs // 3600)   # e.g.  31 hours since run
    #             if offset_hours < 0:
    #                 # A forecast earlier than run‐start?  Skip or treat as zero.
    #                 delta = 0.0
    #                 new_values.append({"value": round(delta, 3), "datetime": rec["datetime"]})
    #                 continue

    #             # 3) Determine that record’s “midnight of the same UTC day”
    #             midnight_dt = rec_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    #             midnight_key = midnight_dt.isoformat()

    #             # 4) Figure out what “forecast-hour” corresponds to that midnight:
    #             #    Since `offset_hours` is hours‐since‐run, the midnight offset is:
    #             midnight_offset = (offset_hours // 24) * 24
    #             prev_offset = midnight_offset - 1

    #             # 5) If we haven’t already looked up “midnight cache” for this day, do so now:
    #             if midnight_key not in midnight_cache:
    #                 # a) Check if this exact midnight appears in block["values"]:
    #                 found_mid = next((r for r in block["values"]
    #                                 if r["datetime"] == midnight_key),
    #                                 None)
    #                 if found_mid:
    #                     # If it’s present, we still need “the hour immediately before midnight”
    #                     # in order to compute the daytime delta (see GFS standard).
    #                     prev_hour_val = extract_previous_hour_value(prev_offset)
    #                     midnight_cache[midnight_key] = found_mid["value"] - prev_hour_val
    #                 else:
    #                     # Otherwise, pull the cached 4‐tuple at midnight_offset, interpolate, etc.
    #                     midnight_items = self._get_cached_values(kv, midnight_offset)
    #                     if midnight_items is None:
    #                         midnight_cache[midnight_key] = 0.0
    #                     else:
    #                         data_m, lat_m, lon_m, _mmeta = midnight_items
    #                         mid_val = self.model_service.interpolate_value(data_m, lat_m, lon_m, lat, lon)
    #                         prev_val = extract_previous_hour_value(prev_offset)
    #                         midnight_cache[midnight_key] = mid_val - prev_val

    #             # 6) Finally compute: delta = current_cum – midnight_cum
    #             delta_24h = cum_val - midnight_cache[midnight_key]
    #             new_values.append({
    #                 "value": round(delta_24h, 3),
    #                 "datetime": rec["datetime"]
    #             })

    #         # Overwrite the block’s “values” with the new 24 h deltas
    #         block["values"] = new_values
    # def normalize_precipitation_24h(
    #     self,
    #     slice_results: List[Dict[str, Any]],
    #     lat: float,
    #     lon: float
    # ) -> None:
    #     """
    #     In-place, converts each “total-precipitation” block’s cumulative values
    #     into 24 h deltas.  For each timestamp:
    #         delta = cum_value(t) – cum_value(midnight_of_that_day).

    #     If a midnight entry is missing, pulls it from cache + interpolates at (lat, lon).
    #     Assumes metadata has “dataDate” and “dataTime” (init run) so we can compute offset.
    #     """
    #     cfg = SystemConfig().get_default_forecast_json()
    #     param_keys = cfg["param_keys"]
    #     # matches = [d for d in param_keys if d.get("param_key") == "total-precipitation"]
    #     tp_dict = next((d for d in param_keys if d.get("param_key") == "total-precipitation"), None)
    #     kv = tp_dict or {
    #         "param_key": "total-precipitation",
    #         "level": 0,
    #         "typeOfLevel": "surface",
    #         "stepType": "accum"
    #     }
    #     def extract_previous_hour_value(previous_offset: int):
    #         previous_hour_value = 0.0
    #         previous_hour_items = self._get_cached_values(kv, previous_offset)
    #         if previous_hour_items is not None:
    #             data_array, lat_array, lon_array, meta_dict = previous_hour_items
    #             previous_hour_value = self.model_service.interpolate_value(
    #                 data_array, lat_array, lon_array, lat, lon
    #             )
    #             print('I AM THE PREVIOUS HOUR VALUE', previous_hour_value)
    #         return previous_hour_value

    #     for block in slice_results:
    #         # Only transform “total-precipitation” blocks:
    #         if block["metadata"].get("key") != "total-precipitation":
    #             continue

    #         # Extract init‐time from metadata:
    #         meta = block["metadata"]
    #         data_date = meta.get("dataDate")    # e.g. 20250531
    #         data_time = meta.get("dataTime", 0) # e.g. hour of run (0, 6, 12, 18)
    #         if data_date is None:
    #             # If no dataDate, skip normalization:
    #             continue

    #         # Build the GRIB‐run initialization datetime:
    #         date_str = str(data_date)
    #         year = int(date_str[:4])
    #         month = int(date_str[4:6])
    #         day = int(date_str[6:8])
    #         init_dt = datetime(year, month, day, data_time, tzinfo=timezone.utc)

    #         # Build a map: date → midnight_cumulative_value
    #         midnight_cache: Dict[str, float] = {}

    #         new_values: List[Dict[str, Any]] = []
    #         for rec in block["values"]:
    #             dt_iso = rec["datetime"]
    #             cum_val = rec["value"]
    #             # Parse record datetime to a timezone-aware UTC datetime:
    #             rec_dt = datetime.fromisoformat(dt_iso)
    #             if rec_dt.tzinfo is None:
    #                 rec_dt = rec_dt.replace(tzinfo=timezone.utc)

    #             # Find midnight of that same calendar date:
    #             midnight_dt = rec_dt.replace(hour=0, minute=0, second=0, microsecond=0)

    #             # Use ISO‐string as the key for midnight:
    #             midnight_key = midnight_dt.isoformat()

    #             # If midnight_value not yet cached, try to get it:
    #             if midnight_key not in midnight_cache:
    #                 # 1) Look through existing ‘values’ for a match:
    #                 found = next(
    #                     (r for r in block["values"]
    #                         if r["datetime"] == midnight_key),
    #                     None
    #                 )
    #                 offset_hours = int((rec_dt - init_dt).total_seconds() // 3600)
    #                 midnight_offset = (offset_hours // 24) * 24
    #                 previous_offset = midnight_offset - 1

    #                 if found:
    #                     previous = extract_previous_hour_value(previous_offset)
    #                     midnight_cache[midnight_key] = found["value"] - previous
    #                 else:
    #                     # Fetch the 4‐tuple from cache (data_array, lat_array, lon_array, meta_dict):
    #                     cached = self._get_cached_values(kv, midnight_offset)
    #                     if cached is None:
    #                         midnight_cache[midnight_key] = 0.0
    #                     else:
    #                         data_array, lat_array, lon_array, meta_at_midnight = cached
    #                         mid_val = self.model_service.interpolate_value(
    #                             data_array, lat_array, lon_array, lat, lon
    #                         )
    #                         previous = extract_previous_hour_value(previous_offset)
    #                         midnight_cache[midnight_key] = mid_val - previous
    #                         print('I AM MIDNIGHT VALUE', midnight_cache[midnight_key])

    #             # Finally, compute delta = cumulative − midnight_cumulative:
    #             delta = cum_val - midnight_cache[midnight_key]
    #             new_values.append({
    #                 "value": round(delta, 3),
    #                 "datetime": dt_iso
    #             })

    #         # Replace the block’s “values” list with the normalized 24 h deltas:
    #         block["values"] = new_values



    def _apply_search_tems(self, param_key: Dict[str, Any],  search: Dict[str, Any]) -> Dict[str, Any]:
        grbSearch = {**param_key.get("grbSearch", {"terms": {}})}
        grbSearch["terms"] = {**grbSearch["terms"], **search}
        grbSearch["terms"]["param_name"] = param_key.get("param_key")
        return grbSearch;

    def _load_offset_for_param_key(self, param_key: Dict[str, Any], offset: int, grbs: List[bytes], pm: Dict[str, str], search: Dict[str, Any]):
        try:
            key_name = param_key.get("param_key")
            if not key_name:
                return None
            key = self._get_cache_key(key_name, offset)
            if self._localStorage.available(key):
                self._localStorage.extend(key, self._ttl_local)
                return self._localStorage.get(key)
            param_name = pm.get(key_name)
            if not param_name:
                return None

            result = self.model_service._set_cached_grib_values(
                grbs,
                param_name,
                self.model,
                offset,
                param_key.get("level"),
                param_key.get("typeOfLevel"),
                param_key.get("stepType"),
                self._apply_search_tems(param_key, search)
            )

            if not self.preload_state:
                self._localStorage.set(key, result, self._ttl_local)
            return result
        except Exception as e:
            print(f"ERROR::_load_offset_for_param_key {e}")

    def generate_midnight_indices(self, max_offset: int) -> list[int]:
        """
        Return a list of offsets corresponding to midnight boundaries (every 24 hours),
        from 0 up to the largest multiple of 24 ≤ max_offset.
        """
        return list([*range(0, max_offset + 1, 24), *range(0, max_offset + 1, 23) ] )


    def preload_slices(self):
        if self._loading:
            return

        self._loading = True
        cache_key = self.model_service._get_grib_dict_values_key(self.model, 0)
        values = self.model_service._cache_get(cache_key) or {}
        pm = self.model_service.build_param_map_for_offset(self.model)
        loaded_offsets = self.offsets # self.offsets_primary # self.offsets if  self.preload_state  else self.offsets_primary
        print(f'RUNNING A PRELOAD WITH THESE OFFSET {loaded_offsets} {len(loaded_offsets)}')
        offsets = [off for off in loaded_offsets if off >= 0]
        length = len(offsets)
        total_length = 0
        cfg = SystemConfig().get_default_forecast_json()
        param_keys = cfg["param_keys"]
        def _compute_for_offset(off: int):
            try:
                search: Dict[str, Any] = {}
                grbs = self.model_service._get_raw_grib(self.model, off, search)
                if not grbs:
                    return values
                for param_key in param_keys:
                    key_name = param_key.get("param_key")
                    result = self._load_offset_for_param_key(param_key, off, grbs, pm, search)
                    if result is None:
                        continue
                    values[param_key.get("key", key_name)] = result
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
