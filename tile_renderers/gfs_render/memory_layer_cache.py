import os
import threading
import bisect
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Any, Optional
import logging
import math
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
        self._loading_tile = False
        self._init_run = True
        self._cacheStore = memory
        self._localStorage = LocalStorage()
        self._ttl = CACHE_TTL
        self._ttl_3 = self._ttl * 3
        self._ttl_local = self._ttl + (11 + 60)
        self.round_value = 4

    def get_worker_count(self):
        # max_cores = 24
        system_cpu = os.cpu_count() or 4
        cpu_count = math.ceil( system_cpu / 3)
        return system_cpu if self._init_run else cpu_count
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
            return

        self.offsets = range1;
        step_subtract = self.step_hours - 1
        step_range = step_subtract
        for i in range(step_range):
            range_values = list(range(i + 1, self.total_hours + (i + 2), self.step_hours))
            self.offsets = [*self.offsets, *range_values]

        # self._append_midnight_indices(self.offsets)
        print(f"I have these layered offsets {self.offsets}")

    def is_loading(self):
       return self._loading and self._init_run

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
            self._cacheStore.set(key, pickle.dumps(value, protocol=4), expire=self._ttl)
        except Exception as e:
            logger.error(f"Error setting cache key {key}: {e}")

    def _get_cache_key(self, param_key: str, offset: int) -> str:
        hour_key = self.model_service.todays_hour_with_date(offset)
        return f"layer_cache:{param_key}@{hour_key}"

    def _get_expire_key(self, param_key: str, offset: int) -> str:
        hour_key = self.model_service.todays_hour_with_date(offset)
        return f"layer_expire:{param_key}@{hour_key}"

    def _preload_offset(self, off: int) -> None:
        # hour_key = self.model_service.todays_hour_with_date(off)
        print(f"[Preload] offset={off} -> hour_key={off}")
        for entry in self.param_keys:
            pk = entry["param_key"]
            lvl = entry.get("level")
            tof = entry.get("typeOfLevel")
            stp = entry.get("stepType")
            key = self._get_cache_key(pk, off)
            expire_key = self._get_expire_key(pk, off)
            try:
                available = self._cacheStore.available(key)
                has_expire = self._localStorage.available(expire_key)
                print(f"[Preload] Building interpolator for {key} {available}")
                if available and has_expire:
                    self._cacheStore.extend(key, self._ttl)
                    continue

                ip = self.model_service.generate_interpolator(
                    self.model, pk, off, lvl, tof, stp
                )
                if not ip:
                    print(f"[Preload] Skipped {pk}@{off} (no data)")
                    continue
                print(f"[Preload] Setting cache key {key}")

                self._cache_set(key, ip)
                self._localStorage.set(expire_key, True, self._ttl_3)
            except Exception as e:
                logger.error(f"Error setting cache key {key}: {e}")
        print(f"[Preload] Completed offset {off}")

    def _get_cached_values(self, key_value: dict[str, Any], offset: int, no_process: bool = False ):
        """
        We super-charge caching, using a local memory layer with a redis-backed cache layer
        """
        if key_value is None:
            return None
        key_name = key_value.get("param_key", None)

        if key_name is None:
            return None
        key = self._get_cache_key(key_name, offset)
        # look for it in local storage
        if self._localStorage.available(key):
            self._localStorage.extend(key, self._ttl_local);
            return self._localStorage.get(key)

        if no_process:
            return None

        search  = {}
        search["offset"] = offset
        grbs = self.model_service._get_raw_grib(self.model, offset, search)
        result = self.model_service._set_cached_grib_values(
            grbs,
            key_name,
            self.model,
            offset,
            key_value.get("level"),
            key_value.get("typeOfLevel"),
            key_value.get("stepType"),
            self._apply_search_tems(key_value, search)
        )
        # self._cacheStore.set(key, result, self._ttl_3)
        grbs.close() # type: ignore
        self._localStorage.set(key, result, self._ttl_local)
        return result

    def _get_base_time(self):
        now = datetime.utcnow()
        if now.minute >= 30:
            now += timedelta(hours=1)
        base_time = now.replace(minute=0, second=0, microsecond=0)
        return base_time

    def find_current_slice(self, lat: float, lon: float, hour_offset: int):
        values = []
        offset = self._find_closest_offset(hour_offset)

        def _compute_for_offset(key_value: Any):
            sendResults = {}
            try:
                result = self._get_cached_values(key_value, offset)
                if not result:
                    return None

                data_array, lat_array, lon_array, meta_dict, off = result
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
        return self.append_extras_to_current_offset(values, offset, lat, lon)

    def append_extras_to_current_offset(
        self,
        results: List[Dict[str, Any]],
        offset: int,
        lat: float,
        lon: float
    ):
        self.weather_utils.apply_wind_direction_to_single(results)
        self.normalize_precipitation_24h_slice(results, offset, lat, lon)
        for r in results:
            r["value"] = round(r["value"], self.round_value)
        return sorted(
            results,
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
        try:
            meta: dict[str, Any] = sendResults.get("metadata", {"forecastStart": 9999, "forecastEnd": -9999 })
            if meta["forecastStart"] > forecast_time:
                sendResults["metadata"]["forecastStart"] = forecast_time

            if meta["forecastEnd"] < forecast_time:
                sendResults["metadata"]["forecastEnd"] = forecast_time
        except Exception as e:
            print('Offset append error', e)

    def find_slice(self, lat: float, lon: float, start_hour_offset = 0):
        values = []
        offset = self._find_closest_offset(start_hour_offset)
        offsets = [off for off in self.offsets_primary if off >= 0]
        offsets = offsets[offsets.index(offset):]
        def _compute_for_offset(key_value: Any):
            sendResults: dict[str, Any] = {"values" : []}
            try:
                for off in offsets:
                    result = self._get_cached_values(key_value, off)
                    if not result:
                        continue
                    data_array, lat_array, lon_array, meta_dict, _off = result
                    forecast_time = meta_dict.get("forecastTime", _off)
                    # if not 'units' in sendResults:
                    #     sendResults["unit"] = meta_dict.get("parameterUnits", "unknown")
                    if not 'metadata' in sendResults:
                        sendResults["metadata"] = meta_dict.copy()
                        del sendResults["metadata"]["forecastTime"]
                        sendResults["metadata"]["forecastStart"] = forecast_time
                        sendResults["metadata"]["forecastEnd"] = forecast_time
                    val = self.model_service.interpolate_value(data_array, lat_array, lon_array, lat, lon)
                    date_time = self.model_service._build_valid_datetime_from_metadata(meta_dict, off)
                    self._append_start_end(sendResults, forecast_time)
                    sendResults["values"].append({
                        "value": val,
                        "datetime": date_time.isoformat(),
                        "offset": off,
                        "day": date_time.day,
                        "hour": date_time.hour
                    })
                    # self._append_start_end(sendResults, forecast_time)

                return sendResults
            except Exception as e:
                print(f"ERROR {e}")

        with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as exe:
            cfg = SystemConfig().get_default_forecast_json()
            param_keys = cfg["param_keys"]
            futures = {exe.submit(_compute_for_offset, key): key for key in param_keys}
            for fut in as_completed(futures):
                result = fut.result()
                if not result:
                    continue
                values.append(result)
        # sort each parameter‐block by datetime
        for block in values:
            block["values"].sort(key=lambda x: x["datetime"])

        return self.append_special_slices_values(values, offset, lat, lon)

    def append_special_slices_values(
        self,
        slice_results: List[Dict[str, Any]],
        offset: int,
        lat: float,
        lon: float) -> List[Dict[str, Any]]:
        self.normalize_precipitation_24h(slice_results, lat, lon)
        self.weather_utils.apply_extras_details(slice_results, lat, lon)
        for result in slice_results:
            for r in result.get("values", []):
                r["value"] = round(r["value"], self.round_value)
        return sorted(
            slice_results,
            key=lambda item: item["metadata"]["key"]
        )

    def interplate_values_simple(self,  key_value: dict[str, Any], offset: int, lat: float, lon: float):
        result = self._get_cached_values(key_value, offset, True)
        if not result:
            return (None,None,offset)
        data_array, lat_array, lon_array, meta_dict, off = result
        value = self.model_service.interpolate_value(data_array, lat_array, lon_array, lat, lon)
        return (value,meta_dict,off)

    def find_prev_midnight(self, offset: int) -> Optional[int]:
        """
        midnights: a sorted list of integers like [-25, -1, 23, 47, ...]
        offset:    an integer like 1 or 24 or any forecast‐hour
        Returns the greatest m in midnights with m <= offset, or None if none.
        """
        mid_indices = self.generate_midnight_indices(offset, True)
        # Ensure it’s sorted ascending

        # Find insertion point to keep sorted order
        idx = bisect.bisect_right(mid_indices, offset) - 1
        if idx >= 0:
            return mid_indices[idx]
        return None

    def normalize_precipitation_24h_slice(self, records: List[Dict[str, Any]], offset: int, lat:float, lon:float):
       total_precipitation = self.pull_specific_records("total-precipitation", records)
       if total_precipitation is None:
           return

       tp_def = self.get_param_key_layer("total-precipitation")
       if tp_def is None:
            return

       current_val = total_precipitation["value"]
       midnight_offset = self.find_prev_midnight(offset)
       if midnight_offset is None:
           return
       value,meta_dict,off = self.interplate_values_simple(tp_def, offset, lat, lon)
       if meta_dict is None:
           return

       precip_24 = {
           "unit": total_precipitation["unit"],
           "metadata": self.build_precipitation_meta(total_precipitation),
           "value": current_val - value,
           "datetime": total_precipitation["datetime"],
       }
       records.append(precip_24)




    def get_midnight_values(self, values: List[Dict[str, Any]], tp_def: Dict[str, Any], lat:float, lon:float):
        max_hour = max(item["offset"] for item in values)
        # min_hour = min(item["offset"] for item in normalize_precip)
        mid_indices = self.generate_midnight_indices(max_hour, True)
        # filtered_indeces = [i for i in mid_indices]
        # current_midnight = [r for r in normalize_precip if r.get("hour", -1) != 0]
        # offset_map = dict(map( lambda r: (r["offset"], r), for r in normalize_precip ))
        offset_map: dict[int, Any] = {
            p["offset"]: p
            for p in values
        }
        #last_midnight_offset = 0
        midnight_values: List[Dict[str, Any]] = []
        for offset in mid_indices:
            current_offset = offset_map.get(offset, None)
            if current_offset is not None:
               midnight_values.append(current_offset)
               continue

            value,meta_dict,off = self.interplate_values_simple(tp_def, offset, lat, lon)
            if meta_dict is None:
               continue

            datetime = self.model_service._build_valid_datetime_from_metadata(meta_dict, off)
            midnight_values.append({
                "value": value,
                "datetime": datetime.isoformat(),
                "offset": off,
                "day": datetime.day,
                "hour": datetime.hour
            })
        return [r for r in midnight_values if r.get("hour", -1) == 0]

    def apply_records_as_date_map(self, values:  List[Dict[str, Any]]):
        map: Dict[int, Any] = {}
        for value in values:
            date_time = datetime.fromisoformat(value["datetime"])
            map[date_time.day] = value
        return map

    def apply_24_values(self, midnight_values: List[Dict[str, Any]], values:  List[Dict[str, Any]]):
        offset_map = self.apply_records_as_date_map(midnight_values)
        date_values: List[dict[str, Any]] = []
        for value in values:
            date_time = datetime.fromisoformat(value["datetime"])
            day = date_time.day
            midnight = offset_map.get(day, None)
            if midnight is None:
                continue
            updated_value = value.copy()
            updated_value["value"] = updated_value["value"] - midnight.get("value", 0)
            date_values.append(updated_value)
        return date_values

    def get_param_key_layer(self, key:str):
        cfg = SystemConfig().get_default_forecast_json()
        param_keys = cfg["param_keys"]
        # 1) Find “total-precipitation” in param_keys (fallback if not found)
        tp_def = next(
            (p for p in param_keys if p.get("param_key") == key),
            None
        )
        return tp_def.copy() if tp_def is not None else None

    def pull_specific_records(self, name: str, records: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        return next(
            (block for block in records.copy() if  block["metadata"].get("key") == name),
            None
        )

    def build_precipitation_meta(self, total_precipitation: Dict[str, Any]):
        backup_meta = {"key": "total-precipitation",
        "parameterName": "Total Precipitation",}
        if total_precipitation is None:
            total_precipitation = dict({
                "metadata": backup_meta
            })

        meta = total_precipitation.get("metadata" , backup_meta)
        return {
            **meta,
            "key": f"daily-{meta.get("key", "total-precipitation")}",
            "shortName": "dtp",
            "parameterName": f"Daily {meta.get("parameterName", "Total Precipitation")}",
            "name": f"Daily Derived {meta.get("parameterName", "Total Precipitation")}"
        }

    def daily_precipitation_meta(self, records: List[Dict[str, Any]]):
        total_precipitation = self.pull_specific_records("total-precipitation", records)
        if total_precipitation is None:
            return None
        return self.build_precipitation_meta(total_precipitation)

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
            # 1) Find “total-precipitation” in param_keys (fallback if not found)
            tp_def = self.get_param_key_layer("total-precipitation")
            if tp_def is None:
                return

            try:
                total_precipitation = self.pull_specific_records("total-precipitation", slice_results)
                if not total_precipitation:
                    return
                precitation_values = total_precipitation["values"].copy()
                midnight_values = self.get_midnight_values(precitation_values, tp_def, lat, lon)
                values = self.apply_24_values(midnight_values, precitation_values)
                # meta = total_precipitation["metadata"].copy()
                precip_24 = {
                    "values": values,
                    "metadata": self.daily_precipitation_meta(slice_results)
                }
                slice_results.append(precip_24)
            except Exception as e:
                print("Failed to apply daily precipitation", e)

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

    def generate_midnight_indices(self, max_offset: int, strict = False) -> list[int]:
        """
        Return a list of offsets corresponding to midnight boundaries (every 24 hours),
        from 0 up to the largest multiple of 24 ≤ max_offset.
        """
        now = datetime.now(timezone.utc)
        if now.minute >= 30:
            now += timedelta(hours=1)
        now = now.replace(minute=0,  second=0, microsecond=0)
        hours_sice_midnight = now.hour
        first_midnight = -hours_sice_midnight
        mids = []
        off = first_midnight
        while off <= max_offset:
            mids.append(off)
            if not strict:
                mids.append(off + 1) # we do this so our items are cached for the next hour
            off += 24
        return mids


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
        self._append_midnight_indices(offsets)
        length = len(offsets)
        total_length = 0
        cfg = SystemConfig().get_default_forecast_json()
        param_keys = cfg["param_keys"]
        print("RUNNING THESE OFFSETS", offsets)
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

                grbs.close() # type: ignore
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

    def _preload_param(self, entry: Dict[str, Any]) -> None:
        pk = entry["param_key"]
        logger.info(f"[Preload] Param={pk}")
        for off in range(7):
            ip = self.model_service.get_or_build_interpolator(
                self.model, pk, off,
                entry.get("level"), entry.get("typeOfLevel"), entry.get("stepType")
            )
            if not ip:
                continue
        logger.info(f"[Preload] Completed param {pk}")

    def preload_tiles(self) -> None:
        """
        Precompute and store every (param_key, offset) interpolator in its Redis hash.
        """
        if self._loading_tile:
            return
        self._loading_tile = True

        workers = self.get_worker_count()
        logger.info(f"[RedisLayerCache] Preloading {len(self.offsets)} offsets × {len(self.param_keys)} params using {workers} workers")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            executor.map(self._preload_param, self.param_keys.copy())

        self._loading_tile = False
