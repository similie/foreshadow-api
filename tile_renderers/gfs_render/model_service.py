import threading
from threading import Timer, Lock
import pygrib
from pyproj import Transformer
import logging
import os
import re
import json
import math
import numpy as np
import pickle
from datetime import datetime, timezone, timedelta
from typing import Any, Tuple, Dict, Optional, Union, List, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from scipy.spatial import Delaunay
# Replace these imports with your own local modules:
from .parameter_meta import apply_parameter_meta
from .caching.cache import ICacheBackend, CACHE_TTL
from .caching.local_cache import LocalStorage
from .threads import ConcurrencyService
from .map_colors import MapColors
from .time_logger import TimeLogger
from .interpolator import Interpolator
from .system_config import SystemConfig
from .fast_interpolation import fast_interpolate
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
# Global dictionary for preloaded GRIB file data.
# Keys could be (model, date_str, run_str) or (model, hour_offset) depending on your needs.
# PRELOADED_GRIB_DATA: Dict[str, Dict[Tuple[str, Optional[int], Optional[str]], Tuple[np.ndarray, np.ndarray, np.ndarray, Dict[str, Any]]]] = {}



DEBOUNCE_INTERVAL = 0.3  # debounce period in seconds

class InterpolatorCachingService:
    def __init__(self, cache_backend: ICacheBackend, local_cache: ICacheBackend, no_mem = False):
        self.cache = cache_backend
        self.local_cache = local_cache
        self.debounce_lock = Lock()
        # Maps key -> (latest_interpolator, timer)
        self._no_mem = no_mem
        self.debounce_map = {}

    def _untangle_pickle(self, cached: Any) -> Optional[Interpolator]:
        if cached is not None:
            try:
                return pickle.loads(cached)
            except Exception as e:
                logger.error(f"Error unpickling cache key : {e}")
        return None

    def get_interpolator(self, key: str) -> Optional[Interpolator]:
        inter = self.local_cache.get(key)
        if inter:
            return inter
        inter = self.cache.get(key) or None
        if inter:
            inter = self._untangle_pickle(inter)
            self.local_cache.set(key, inter, CACHE_TTL)
            return inter
        return  None

    def set_global_cache_val(self, key: str, inter: Interpolator, expire = CACHE_TTL) -> None:
        # Write the value to the global cache (e.g., Redis)
        if self._no_mem:
            return
        self.cache.set(key, pickle.dumps(inter, protocol=4), expire=expire)

    def _debounce_callback(self, key: str) -> None:
        """Called when the debounce timer expires for a key."""
        with self.debounce_lock:
            # Retrieve the latest value for the key and remove its timer.
            value, _ = self.debounce_map.pop(key, (None, None))
        if value is not None:
            self.set_global_cache_val(key, value)

    def set_interpolator(self, key: str, inter: Interpolator) -> None:
        # Always update the local (level 2) cache immediately.
        self.local_cache.set(key, inter, CACHE_TTL)
        # Debounce the global cache update.
        with self.debounce_lock:
            # If a timer already exists for this key, cancel it.
            if key in self.debounce_map:
                _, timer = self.debounce_map[key]
                timer.cancel()
            # Create a new timer that will call _debounce_callback after the interval.
            timer = Timer(DEBOUNCE_INTERVAL, self._debounce_callback, args=(key,))
            self.debounce_map[key] = (inter, timer)
            timer.start()


class ModelService:
    """
    ModelService manages:
      - GRIB file retrieval,
      - Param map building,
      - Interpolator building/caching for tiles,
      - LRU/time-based pruning,
      - Single-point forecasts,
      - Multi-day bounding-box forecasts,
      - Parallel tasks like prewarming.
    """

    def __init__(self, cache_backend: ICacheBackend, local_cache: ICacheBackend = LocalStorage(), no_mem = False) -> None:
        self.config = SystemConfig()
        self.cache = cache_backend
        self.local_cache = local_cache
        self.concurrency = ConcurrencyService()
        self.key_locks: Dict[str, threading.Lock] = {}
        self.metadata_cache: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
        self.decimation = self.config.get_decimation()
        self.MODEL_MAP = self.config.get_model_map()
        self.GRIB_FILES_PATH = self.config.file_path()
        self.RUN_HOURS = [0, 6, 12, 18]
        self.transformer = Transformer.from_crs("epsg:4326", "epsg:3857", always_xy=True)
        self.colors = MapColors()
        self.interpolator = Interpolator(self.transformer)
        self.interpolator_cache = InterpolatorCachingService(self.cache, self.local_cache, no_mem)

        # self._preload_all_grib_data()
    def get_or_build_tile_grid(self, ip, pts: np.ndarray, tile_key: str, oversize: int = 257) -> Optional[np.ndarray]:
        """
        If the tile grid for tile_key is not yet computed, one thread computes it
        (by calling ip(pts) in one go) while other threads wait.
        Once computed, the grid is cached and returned.
        """
        try:
            timer = TimeLogger()
            timer.log("Start 1")
            # # return None
            tri: Delaunay = ip.tri  # ip.tri is the Delaunay object used by LinearNDInterpolator
            # Compute the simplex indices for each query point.
            simplex_indices = tri.find_simplex(pts)
            # Get the transformation matrix and the simplices.
            transform = tri.transform  # shape: (nsimplex, ndim+1, ndim)
            simplices = tri.simplices   # shape: (nsimplex, ndim+1)
            vertex_values = ip.values.ravel()  # Data values as a 1D array
            f_grid = fast_interpolate(transform, simplices, vertex_values, pts, simplex_indices)
            grid = f_grid.reshape((oversize, oversize))
            # self._cache_set(tile_key, ip)
            self.interpolator_cache.set_interpolator(tile_key, ip)
            timer.log("END 1")
            return grid
        except Exception as e:
            logger.error(f"Faile to interpolate key file {tile_key}: {e}", exc_info=True)
            return None
    # ---------------------------------------------------------
    # Unified Interpolation Methods (unchanged)
    # ---------------------------------------------------------
    def interpolate_value(
        self,
        data: np.ndarray,
        lats: np.ndarray,
        lons: np.ndarray,
        target_lat: float,
        target_lon: float
    ) -> float:
        wrapped_lon = self._wrap_lon_0_360(target_lon)
        rc_list = self.find_nearest_grid_indices(lats, lons, target_lat, wrapped_lon, k=4)
        return self.bilinear_from_indices(lats, lons, data, rc_list, target_lat, wrapped_lon)

    def _wrap_lon_0_360(self, lon: float) -> float:
        return lon + 360.0 if lon < 0 else lon

    def find_nearest_grid_indices(
        self,
        lat_array: np.ndarray,
        lon_array: np.ndarray,
        target_lat: float,
        target_lon: float,
        k: int = 4
    ) -> List[Tuple[int, int]]:
        rows, cols = lat_array.shape
        flat_lats = lat_array.ravel()
        flat_lons = lon_array.ravel()
        d2 = (flat_lats - target_lat) ** 2 + (flat_lons - target_lon) ** 2
        idx_sorted = np.argsort(d2)[:k]
        return [(idx // cols, idx % cols) for idx in idx_sorted]

    def bilinear_from_indices(
        self,
        lat_array: np.ndarray,
        lon_array: np.ndarray,
        data_array: np.ndarray,
        rc_list: List[Tuple[int, int]],
        target_lat: float,
        target_lon: float
    ) -> float:
        weights, vals = [], []
        for r, c in rc_list:
            latv, lonv, datv = lat_array[r, c], lon_array[r, c], data_array[r, c]
            dist = math.sqrt((latv - target_lat) ** 2 + (lonv - target_lon) ** 2)
            w = 1.0 / (dist + 1e-9)
            weights.append(w)
            vals.append(datv)
        total_w = sum(weights)
        if total_w < 1e-14:
            return float(vals[0])
        return float(sum(v * w for v, w in zip(vals, weights)) / total_w)

    def fallback_time(self, fallback_offset: int) -> datetime:
        # fallback: use current UTC hour, rounded to the top of the hour
        now = datetime.now(timezone.utc)
        base = now.replace(minute=0, second=0, microsecond=0)
        return base + timedelta(hours=fallback_offset)

    def _build_valid_datetime_from_metadata(self, meta: Dict[str, Any], fallback_offset: int) -> datetime:
        data_date = meta.get("dataDate")
        data_time = meta.get("dataTime", 0)
        fcst_time = meta.get("forecastTime", fallback_offset)
        if not data_date:
            # fallback: use current UTC hour, rounded to the top of the hour
            return self.fallback_time(fallback_offset)
        try:
            yyyymmdd = str(data_date)
            year = int(yyyymmdd[:4])
            month = int(yyyymmdd[4:6])
            day = int(yyyymmdd[6:8])
            init_dt = datetime(year, month, day, data_time, 0, 0, tzinfo=timezone.utc)
            return init_dt + timedelta(hours=fcst_time)
        except Exception:
            return self.fallback_time(fallback_offset)

    # -------------------------------------------------------------------------
    # Basic GRIB Utilities
    # -------------------------------------------------------------------------
    def build_run_init_times(self, max_lookback=5, max_forward=1) -> List[datetime]:
        now_utc = datetime.now(timezone.utc)
        runs: List[datetime] = []
        for d in range(-max_lookback, max_forward + 1):
            dt_candidate = now_utc + timedelta(days=d)
            date_part = dt_candidate.date()
            for rh in self.RUN_HOURS:
                run_dt = datetime(date_part.year, date_part.month, date_part.day, rh, tzinfo=timezone.utc)
                runs.append(run_dt)
        runs.sort(reverse=True)
        return runs

    def process_hour_offset(self, hour_offset: int) -> int:
        if hour_offset <= 120:
            return hour_offset
        delta = hour_offset - 120
        change = delta % 3
        if change == 0:
            return hour_offset
        elif change == 1:
            return hour_offset - 1
        return hour_offset + 1

    def find_date_run_fhr(self, model: str, hour_offset: int,
                          max_lookback=5, max_forward=1) -> Tuple[Optional[str], Optional[str], Optional[int]]:
        if model not in self.MODEL_MAP:
            logger.error(f"Unknown model '{model}'.")
            return None, None, None

        target_dt = datetime.now(timezone.utc) + timedelta(hours=hour_offset)
        runs = self.build_run_init_times(max_lookback, max_forward)

        cfg = self.MODEL_MAP[model]
        prefix = cfg["FILE_PREFIX"]
        category = cfg["FILE_CATEGORY"]
        resolution = cfg["RESOLUTION"]
        appendix = cfg["FILE_APPENDIX"]

        for rd in runs:
            diff_hrs = (target_dt - rd).total_seconds() / 3600.0
            offset = int(round(diff_hrs))
            fhr = self.process_hour_offset(offset)
            if 0 <= fhr <= 384:
                date_str = rd.strftime("%Y%m%d")
                run_str = f"{rd.hour:02d}"
                f_str = f"f{fhr:03d}"
                folder = os.path.join(self.GRIB_FILES_PATH, date_str, run_str)
                fname = f"{prefix}.t{run_str}z.{category}.{resolution}.{f_str}{appendix}"
                path = os.path.join(folder, fname)
                if os.path.exists(path):
                    return date_str, run_str, fhr
        return None, None, None

    def get_grib_file(self, model: str, hour_offset: int, grbSearch: Optional[Dict[str, Any]] = None) -> Optional[str]:
        try:
            d, r, fhr = self.find_date_run_fhr(model, hour_offset)
            if not d:
                return None
            cfg = self.MODEL_MAP[model]
            folder = os.path.join(self.GRIB_FILES_PATH, d, r)  # type: ignore
            fname = f"{cfg['FILE_PREFIX']}.t{r}z.{cfg['FILE_CATEGORY']}.{cfg['RESOLUTION']}.f{fhr:03d}{cfg['FILE_APPENDIX']}"
            fullpath = os.path.join(folder, fname)
            if os.path.exists(fullpath):
                return fullpath
        except Exception as e:
            logger.error(f"GRB Path Error {e}")
        return None
    # -------------------------------------------------------------------------
    # Building Param Map
    # -------------------------------------------------------------------------
    def build_param_map_for_offset(self, model: str, hour_offset: int = 0) -> Dict[str, str]:
        cache_key = f"param_map:{model}:{hour_offset}"
        def compute():
            fp = self.get_grib_file(model, 0)
            if not fp:
                return {}
            try:
                with pygrib.open(fp) as grbs: # type: ignore
                    plist = {grb.name for grb in grbs}
                pm = {re.sub(r"[^\w\s_-]", "", p.replace("/", "_")).lower().replace(" ", "-"): p for p in plist}
                return pm
            except Exception as e:
                logger.error(f"Error building param map for {model}, {hour_offset}: {e}")
                return {}
        return self._get_or_compute(cache_key, compute)

    def flip_latitudes(self, ckey: Tuple[Any, ...], g: Any) -> bool:
        """
        Detect if the GRIB message requires flipping the latitude array.
        """
        j_scans_pos = getattr(g, "jScansPositively", None)
        if j_scans_pos == 0:
            logger.info(f"{ckey}: Detected jScansPositively=0 => lat_flip=True")
            return True
        return False

    def build_interpolator_key(
        self,
        model: str,
        param_key: str,
        hour_offset: int,
        level: Optional[int] = None,
        level_type: Optional[str] = None,
        step_type: Optional[str] = None
    ) -> Tuple[Any, ...]:
        time_key = self.todays_hour_with_date(hour_offset)
        return (model, param_key, time_key, level, level_type, step_type)

    def get_interpolator_cache_key(
        self,
        model: str,
        param_key: str,
        hour_offset: int,
        level: Optional[int] = None,
        level_type: Optional[str] = None,
        step_type:  Optional[str] = None):
        return f"interp:{model}:{param_key}:{self.todays_hour_with_date(hour_offset)}:{level}:{level_type}:{step_type}"

    def generate_interpolator(self, model: str, param_key: str, hour_offset: int, level:  Optional[int] = None, level_type:  Optional[str] = None, step_type: Optional[str] = None):

        file_path = self.get_grib_file(model, hour_offset)
        pm = self.build_param_map_for_offset(model)
        param_name = pm.get(param_key)
        if not param_name:
            return None

        try:
            with pygrib.open(file_path) as grbs: # type: ignore
                g = self._select_grib_message(grbs, param_name, level, level_type, step_type)
                if not g:
                    return None
                data = g.values
                lats, lons = g.latlons()
                lat_flip = self.flip_latitudes(self.build_interpolator_key(model, param_key, hour_offset, level, level_type, step_type), g)
                ip = self.interpolator.build_interpolator(data, lats, lons, lat_flip=lat_flip, decimation=self.decimation)
                if ip is None:
                    return None
                # meta = self._extract_grib_metadata(g)
                gmin = float(getattr(g, "minimum", 0.0))
                gmax = float(getattr(g, "maximum", 1.0))
                ip.gmin, ip.gmax = self.update_and_get_min_max(model, param_key, level if level is not None else 0,
                                                               level_type if level_type is not None else 'surface',
                                                               step_type if step_type is not None else 'instant',
                                                               gmin, gmax)
                ip.missing_val = float(getattr(g, "missingValue", 9999.0))
                # Cache both the interpolator and its metadata together.
                ip(self.config.get_global_pts_boundaries())
                return ip
        except Exception as e:
            logger.error(f"Error building interpolator: {e}", exc_info=True)
            return None

    def get_or_build_interpolator(
        self,
        model: str,
        param_key: str,
        hour_offset: int,
        level: Optional[int] = None,
        level_type: Optional[str] = None,
        step_type: Optional[str] = None,
    ) -> Optional[Any]:
        """
        Build or retrieve from cache a global Interpolator for tile rendering.
        """
        cache_key = self.get_interpolator_cache_key(model, param_key, hour_offset, level, level_type, step_type)
        def compute():
            return self.generate_interpolator(model, param_key, hour_offset, level, level_type, step_type)
        interpolator_cache = self.interpolator_cache.get_interpolator(cache_key)
        if interpolator_cache:
            return interpolator_cache
        return self._get_or_compute(cache_key, compute)

    def interpolate_str(self, template: Any, mapping: dict[str, str]) -> str | None:
        """
        Replace placeholders in `template` with corresponding values from `mapping`.

        Example:
            template = "I am {cool}"
            mapping = {"cool": "not cool"}
            returns "I am not cool"
        """
        if not isinstance(template, str):
            return None

        try:
            return template.format(**mapping)
        except KeyError as e:
            logger.error(f"Missing key '{e.args[0]}' for string interpolation")

        return None
            # If a placeholder is missing in `mapping`, re-raise with a clear message.
            # missing = e.args[0]
            # raise KeyError(f"Missing key '{missing}' for string interpolation") from None

    def _apply_search_conditions(self, key: str, search: Dict[str, Any], conditions: Optional[Dict[str, str]]):
        if conditions is None or conditions.get(key) is None or search.get(key) is None:
            return

        def case_int(x):
            return int(x)
        def case_float(x):
            return float(x)
        def case_bool(x):
            return bool(x)
        if conditions[key] == 'int':
            search[key] = case_int(search[key])
        elif conditions[key] == 'float':
            search[key] = case_float(search[key])
        elif conditions[key] == 'bool':
            search[key] = case_bool(search[key])

    def _append_search(self, search: Dict[str, Any], grbSearch: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if grbSearch is None:
            return search
        template = grbSearch.get("template", None)
        if template is None:
            return search
        terms = grbSearch.get("terms", {})
        conditions = grbSearch.get("conditions", None)
        appendedSearch = search.copy()
        try:
            for key, value in template.items():
                val = self.interpolate_str(value, terms)
                appendedSearch[key] = val or value
                self._apply_search_conditions(key, appendedSearch, conditions)
        except KeyError as e:
            # If a placeholder is missing in `template`, re-raise with a clear message.
            logger.error(f"Key/value interpolation error '{e}'")
            return search
        return appendedSearch;

    def _select_grib_message(
        self,
        grbs: Any,
        param_name: str,
        level: Optional[int],
        type_of_level: Optional[str],
        step_type: Optional[str],
        grbSearch: Optional[Dict[str, Any]] = None
    ) -> Optional[Any]:
        search: Dict[str, str|int] = { "name": param_name }
        if level is not None:
            search["level"] = level
        if type_of_level is not None:
            search["typeOfLevel"] = type_of_level
        if step_type is not None:
            search["stepType"] = step_type
        # print("GRIB SEARCH CRITERIA", search)
        # if level is not None and type_of_level is not None:
        try:
            grbSelect = self._append_search(search, grbSearch)
            sel = grbs.select(**grbSelect)
            if len(sel) == 1:
                logger.debug(f"[Exact match] param={param_name}, level={level}, typeOfLevel={type_of_level}")
                return sel[0]
            # if we faile an exact serach we should fail
            if len(sel) == 0 and level is not None and type_of_level is not None and step_type is not None:
                return None
            # for partial seraches we get close to the surface
            if len(sel) > 1:
                priority = self.pull_priority_layers(sel, grbSearch)
                if priority:
                    return priority
            # we then try to find a fallback
            return self.fetch_with_fallbacks(grbs, param_name, type_of_level, level, step_type)
        except (IndexError, ValueError):
            logger.warning(f"No direct match => fallback for param={param_name}")
            return self.fetch_with_fallbacks(grbs, param_name, type_of_level, level, step_type)
        return None

    def _extract_grib_metadata(self, grb_msg: Any) -> Dict[str, Any]:
        relevant_keys = [
            "parameterName", "parameterUnits", "shortName", "typeOfLevel", "level",
            "minimum", "maximum", "dataDate", "dataTime", "forecastTime", "name", "stepType", "startStep", "endStep"
        ]
        try:
            meta = {k: getattr(grb_msg, k, None) for k in relevant_keys}
            name_details = meta["name"]
            if name_details:
                meta['key'] = self.make_param_key(name_details)

            forcast_time = meta.get("forecastTime", 0)
            start_step = meta.get("startStep", None)
            end_step = meta.get("endStep", None)
            if start_step is None or end_step is None:
                return meta

            if start_step == forcast_time and start_step < end_step:
                meta["forecastTime"] = end_step

            return meta
        except Exception as e:
            logger.error(f"Meta execption error {e}")
        return {}


    # def _find_midnight_for_offset(self, offset: int) -> int:
    #     """
    #     Given a forecast‐hour offset `offset`, return the nearest
    #     “midnight” offset at or before it (i.e. largest multiple of 24 ≤ offset).
    #     """
    #     return (offset // 24) * 24

    # def _get_24h_accum_message(
    #     self,
    #     grbs: Any,
    #     param_name: str,
    #     level: Optional[int],
    #     type_of_level: Optional[str],
    #     step_type: str,
    #     target_offset: int,
    #     grbSearch: Optional[Dict[str, Any]] = None
    # ) -> Optional[Any]:
    #     """
    #     If step_type == "accum", fetch two messages:
    #       - msg_N: accumulation from hour 0 → target_offset
    #       - msg_M: accumulation from hour 0 → midnight_offset (M)
    #     Then compute data_delta = msg_N.values - msg_M.values, and
    #     attach adjusted metadata (e.g. forecastTime = target_offset - M).
    #     Return a dummy object that holds (data_delta, lat, lon, metadata_delta).

    #     If either msg_N or msg_M is missing, return None.
    #     """
    #     # 1) Build basic search dict (name/level/type/stepType)
    #     base_search: Dict[str, Union[str,int]] = {"name": param_name}
    #     if level is not None:
    #         base_search["level"] = level
    #     if type_of_level is not None:
    #         base_search["typeOfLevel"] = type_of_level
    #     base_search["stepType"] = "accum"

    #     # 2) Merge with any extra grbSearch terms (e.g. forecastTime ranges)
    #     if grbSearch:
    #         merged = {**base_search, **grbSearch.get("terms", {})}
    #     else:
    #         merged = base_search.copy()

    #     # 3) First fetch full‐accum up to target_offset
    #     #    We want startStep=0, endStep=target_offset
    #     midnight_off = self._find_midnight_for_offset(target_offset)
    #     # To guarantee we pick exactly the “0 → target_offset” message,
    #     # merge forecastTime constraint if present:
    #     merged_N = merged.copy()
    #     merged_N["startStep"] = 0
    #     merged_N["endStep"] = target_offset

    #     try:
    #         candidates_N = grbs.select(**merged_N)
    #     except Exception:
    #         candidates_N = []

    #     if not candidates_N:
    #         # No “0→N” accumulation available
    #         return None
    #     # If multiple, pick first. (In most GFS files there should be exactly one.)
    #     msg_N = candidates_N[0]

    #     # 4) Next fetch full‐accum up to midnight_off (the previous 24h boundary).
    #     #    If midnight_off == target_offset, then msg_M is effectively “zeroed” (no subtraction).
    #     if midnight_off == target_offset:
    #         # If we’re at a perfect 24h boundary, there's no “previous accumulation”
    #         # we can treat msg_M as all zeros.
    #         data_M = np.zeros_like(msg_N.values)
    #         lat_M, lon_M = msg_N.latlons()
    #         # Build metadata: forecastTime=M;
    #         meta_M = self._extract_grib_metadata(msg_N)
    #         meta_M["forecastTime"] = midnight_off
    #     else:
    #         merged_M = merged.copy()
    #         merged_M["startStep"] = 0
    #         merged_M["endStep"] = midnight_off

    #         try:
    #             candidates_M = grbs.select(**merged_M)
    #         except Exception:
    #             candidates_M = []

    #         if not candidates_M:
    #             # Can't find the pre‐midnight accumulation; give up.
    #             return None
    #         msg_M = candidates_M[0]
    #         data_M = msg_M.values
    #         lat_M, lon_M = msg_M.latlons()
    #         meta_M = self._extract_grib_metadata(msg_M)

    #     # 5) Subtract arrays to get 24h delta
    #     data_delta = msg_N.values - data_M
    #     lat_delta, lon_delta = msg_N.latlons()  # same grid as msg_N
    #     meta_N = self._extract_grib_metadata(msg_N)

    #     # 6) Build a synthetic “GRIB‐like” object for the 24h delta:
    #     class _SyntheticGRIB:
    #         pass

    #     synth = _SyntheticGRIB()
    #     # Attach just enough attributes so downstream code (that does `.values` and `.latlons()`) will work:
    #     synth.values = data_delta
    #     synth.latlons = lambda: (lat_delta, lon_delta)

    #     # Build merged metadata: carry over everything from msg_N,
    #     # but override "forecastTime" to be the 24h window length (target_offset-midnight_off).
    #     merged_meta = meta_N.copy()
    #     merged_meta["forecastTime"] = target_offset - midnight_off
    #     merged_meta["accumulated_from"] = midnight_off
    #     merged_meta["accumulated_to"] = target_offset
    #     synth._metadata = merged_meta

    #     return synth

    def get_interpolator_metadata(
        self,
        model: str,
        param_key: str,
        hour_offset: int,
        level: Optional[int] = None,
        type_of_level: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        time_key = self.todays_hour_with_date(hour_offset)
        ckey = (model, param_key, time_key, level, type_of_level)
        return self.metadata_cache.get(ckey)

    # -------------------------------------------------------------------------
    # Fallback logic (if no exact level/typeOfLevel is specified)
    # -------------------------------------------------------------------------
    def fetch_instant(self, grb: List[Any]):
        if len(grb) == 1:
            return grb[0]
        filtered = [g for g in grb if getattr(g, "stepType", None) == "instant"]
        if len(filtered):
            return filtered[0]
        return None

    def pull_priority_layers(self, matches: List[Any], grbSearch: Optional[Dict[str, Any]] = None):
        def get_priority(item):
            if item.typeOfLevel == "surface":
                return 0
            elif item.typeOfLevel == "orderedSequenceData" and item.level in [0, 1]:
                return 1
            elif item.typeOfLevel == "heightAboveGround":
                return 2
            elif item.typeOfLevel == "atmosphere" and item.level in [0]:
                return 3
            return 99  # fallback for unexpected types
        matches.sort(key=get_priority)
        return self.fetch_instant(matches)

    def fetch_near_surface_fallback(self, grbs: List[Any]) -> Optional[Any]:
        # Collect all matching items
        matches = []
        for f in grbs:
            if f.typeOfLevel == "surface":
                matches.append(f)
            elif f.typeOfLevel == "orderedSequenceData" and f.level in [0, 1]:
                matches.append(f)
            elif f.typeOfLevel == "heightAboveGround":
                matches.append(f)
            elif f.typeOfLevel == "atmosphere" and f.level in [0]:
                matches.append(f)

        # Define a function to assign priority (lower number is higher priority)
        self.pull_priority_layers(matches)

    def search_for_provided_level(
        self,
        grbs: Any,
        param_name: str,
        type_of_level: Optional[str] = "surface",
        level: Optional[int] = 0,
        step_type: Optional[str] = None
    ):
        select = {"name": param_name, "typeOfLevel": type_of_level, "level": level}
        if step_type is not None:
            select = {**select, "stepType": step_type}

        for i in range(2):
            try:
                select["level"] = i
                found = grbs.select(**select)
                if found:
                    logger.info(f"Found surface data (level={i}) for param={param_name}: layer length: {len(found)}")
                    if len(found) > 0:
                        return found[0]
                    else:
                        return None
                continue
            except:
                break
        return None

    def search_height_above_ground(self, grbs: Any, param_name: str, type_of_level: Optional[str] = "surface", step_type: Optional[str] = "instant"):
        if type_of_level == "heightAboveGround":
            return None
        try:
            # two meter above ground
            found = grbs.select(name=param_name, typeOfLevel="heightAboveGround", level=2, stepType=step_type)
            if found:
                logger.info("Found data (level=2) for param=heightAboveGround" )
                return found[0]
        except:
            pass
        return None

    def search_iso_surface(self, grbs: Any, param_name: str):
        near_surface_levels = [1000, 975, 950, 925, 900, 850]
        for lvl in near_surface_levels:
            try:
                found = grbs.select(name=param_name, typeOfLevel="isobaricInhPa", level=lvl)
                if found:
                    logger.info(f"Found isobaric near-surface data (lvl={lvl}) for param={param_name}")
                    return self.fetch_instant(found)
            except:
                pass
        return None

    def search_param_only_and_find_surface(self, grbs: Any, param_name: str):
        try:
            found = grbs.select(name=param_name)
            if found:
                f = self.fetch_near_surface_fallback(found)
                send = f if f else found[0]
                logger.warning(f"No surface/near-surface match for param={param_name}; falling back to first message.")
                return send
        except:
            pass
        return None

    def fetch_with_fallbacks(
        self,
        grbs: Any,
        param_name: str,
        type_of_level: Optional[str] = None,
        level: Optional[int] = None,
        step_type: Optional[str] = None
    ) -> Optional[Any]:
        # print('GETTING THIS DATA ',param_name, type_of_level, level, step_type)
        try:
            layer = self.search_for_provided_level(grbs, param_name, type_of_level, level, step_type)
            if layer:
                return layer
            layer = self.search_height_above_ground(grbs, param_name, type_of_level, step_type)
            if layer:
                return layer
            layer = self.search_iso_surface(grbs, param_name)
            if layer:
                return layer
            layer = self.search_param_only_and_find_surface(grbs, param_name)
            if layer:
                return layer
            logger.warning(f"No matching messages at all for param={param_name}")
            return None
        except Exception as e:
            logger.error(f"Exception while targeting suitable layer {e}")
            return None

    def valid_model(self, model: str) -> bool:
        return model in self.MODEL_MAP

    def build_parameter_name_list(self, model: str, hour_offset: int) -> Dict[str, str]:
        if not self.valid_model(model):
            raise ValueError(f"Unknown model: {model}")
        fp = self.get_grib_file(model, hour_offset)
        if not fp or not os.path.exists(fp):
            raise ValueError(f"No file found for {model}, offset={hour_offset}.")
        return self.build_param_map_for_offset(model)

    def build_local_levels(self, fp: str) -> Dict[str, Dict[str, Dict[ str, List[int|str]]]]:
        local_levels: Dict[str, Dict[str, Dict[ str, List[int|str]]]] = {}
        with pygrib.open(fp) as grbs:  # type: ignore
            for grb in grbs:
                nm = grb.name
                tof = grb.typeOfLevel
                lv = grb.level
                st = grb.stepType
                if nm not in local_levels:
                    local_levels[nm] = {}
                if tof not in local_levels[nm]:
                    local_levels[nm][tof] = {"level": [], "stepType": []}
                local_levels[nm][tof]["level"].append(lv)
                if st not in local_levels[nm][tof]["stepType"]:
                    local_levels[nm][tof]["stepType"].append(st)
        return local_levels

    def parameter_definitions(self, offset: int = 0) -> List[Dict[str, Any]]:
        param_def_key = f"param_key_definitions:{self.todays_hour_with_date(offset)}"
        def compute():
            results: List[Dict[str, Any]] = []
            for model_key in self.MODEL_MAP:
                fp = self.get_grib_file(model_key, offset)
                if not fp or not os.path.exists(fp):
                    results.append({"model": model_key, "params": []})
                    continue
                try:
                    param_list = self.parse_grib_parameters(fp, model_key)
                    results.append({"model": model_key, "params": param_list})
                except Exception as ex:
                    logger.error(f"Error reading {fp}: {ex}")
                    results.append({"model": model_key, "params": []})
            return results
        return self._get_or_compute(param_def_key, compute)

    def parse_grib_parameters(self, fp: str, model_key: str) -> List[Dict[str, Any]]:
        local_levels = self.build_local_levels(fp)
        local_info: Dict[str, Any] = {}
        with pygrib.open(fp) as grbs:  # type: ignore
            for grb in grbs:
                nm = grb.name
                if nm in local_info:
                    continue

                pk = self.make_param_key(nm)
                color_map = self.colors.get_color_profile(model_key, nm, 256, 20)
                p_info = {
                    "parameter_key": pk,
                    "parameter_name": nm,
                    "units": getattr(grb, "units", "N/A"),
                    "param_units": getattr(grb, "parameterUnits", "N/A"),
                    "short_name": getattr(grb, "shortName", "N/A"),
                    "type_of_level": getattr(grb, "typeOfLevel", "unknown"),
                    "color_map": color_map,
                    "data_range": {
                        "min": getattr(grb, "minimum", "N/A"),
                        "max": getattr(grb, "maximum", "N/A"),
                    },
                    "levels": local_levels.get(nm, {})
                }
                local_info[nm] = self.apply_parameter_meta(pk, p_info, model_key)
        return list(local_info.values())

    def make_param_key(self, raw_name: str) -> str:
        pk = raw_name.replace("/", "_")
        pk = re.sub(r"[^\w\s_-]", "", pk)
        return pk.lower().replace(" ", "-")

    def todays_hour(self, offset: int = 0) -> int:
        now = datetime.now()
        return (now + timedelta(hours=offset)).hour

    def todays_hour_with_date(self, offset: int = 0) -> str:
        now = datetime.utcnow()
        if now.minute >= 30:
            now += timedelta(hours=1)
        adjusted_time = now + timedelta(hours=offset)
        key = f"{adjusted_time.day:02}:{adjusted_time.hour:02}"
        return key

    def create_tile_cache_key(
        self,
        model: str,
        param_key: str,
        user_tof: Optional[str],
        hour_offset: int,
        z: int, x: int, y: int,
        level_arg: Optional[int],
        step_type: Optional[str] = None
    ) -> str:
        return (f"tile:{model}:{param_key}:{self.todays_hour_with_date(hour_offset)}:"
                f"{z}:{x}:{y}:{level_arg if user_tof is not None else 0}:"
                f"{user_tof if user_tof else 'surface'}:"
                f"{step_type if step_type else 'instant'}")


    def get_key_string(self, param_val: str|Dict[str, Any]) -> str:
        if isinstance(param_val, str):
            return param_val
        return param_val.get("param_key", "")

    def get_all_key_strings(self, search_parameters: List[Any]):
        return [self.get_key_string(sp) for sp in search_parameters]

    def build_level_params(
        self,
        search_parameter: str,
        level:  Optional[int] = None,
        type_of_level:  Optional[str] = None,
        step_type: Optional[str] = None
    ):
        return f"{search_parameter}:{level}:{type_of_level}:{step_type}"

    def get_all_missing_key_strings(
        self,
        search_parameters: List[Any],
        dict_values: Dict[str, Any],
        level: Optional[int] = None,
        type_of_level: Optional[str] = None,
        step_type: Optional[str] = None):
        missing_params = []
        for sp in search_parameters:
            local_level = level
            local_type_of_level = type_of_level
            local_step_type = step_type
            if not isinstance(sp, str):
                local_level = sp.get("level", level)
                local_type_of_level = sp.get("typeOfLevel", type_of_level)
                local_step_type = sp.get("stepType", step_type)
            param_name = self.get_key_string(sp)
            key = self.build_level_params(param_name, local_level, local_type_of_level, local_step_type)
            if key not in dict_values:
                missing_params.append({
                    "param_key": param_name,
                    "level": local_level,
                    "typeOfLevel": local_type_of_level,
                    "stepType": local_step_type,
                    "key": key
                })
        return missing_params

    def pull_worker_map_values(
        self,
        model: str,
        lat: float,
        lon: float,
        hour_offset: int,
        level: Optional[int] = None,
        type_of_level: Optional[str] = None,
        step_type: Optional[str] = None):
        def map_values(search_item: Any, values_dict: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            try:
                local_level = level
                local_type_of_level = type_of_level
                local_step_type = step_type
                if not isinstance(search_item, str):
                    local_level = search_item.get("level", level)
                    local_type_of_level = search_item.get("typeOfLevel", type_of_level)
                    local_step_type = search_item.get("stepType", step_type)
                param_name = self.get_key_string(search_item)
                param_key = self.build_level_params(param_name, local_level, local_type_of_level, local_step_type)
                result = values_dict.get(param_key)
                if result is None:
                    raise ValueError(f"No cached values for {param_key}")
                data_array, lat_array, lon_array, meta_dict, off = result
                val = self.interpolate_value(data_array, lat_array, lon_array, lat, lon)
                return {"value": float(val), "units": meta_dict.get("parameterUnits", "unknown"), "metadata": meta_dict}

            except Exception as exc:
                logger.warning(f"Error getting forecast for {search_item}: {exc}")
                return None
        return map_values


    def iterate_multiple_keys_against_geo_point(
        self,
        model: str,
        search_parameters: List[Any],
        lat: float,
        lon: float,
        hour_offset: int,
        level: Optional[int] = None,
        type_of_level: Optional[str] = None,
        step_type: Optional[str] = None,
    ) -> List[Optional[Dict[str, Any]]]:
        values_dict = self._get_or_build_value_cache_dictionary(
            model,
            search_parameters,
            lat,
            lon,
            hour_offset,
            level,
            type_of_level,
            step_type
        )
        map_values = self.pull_worker_map_values(model, lat, lon, hour_offset, level, type_of_level,step_type)
        def worker(search_item: Any) -> Optional[Dict[str, Any]]:
            return map_values(search_item, values_dict)

        results = []
        max_workers = os.cpu_count() or 4
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {executor.submit(worker, sp): sp for sp in search_parameters}
            for future in as_completed(future_map):
                try:
                    results.append(future.result())
                except Exception as exc:
                    logger.warning(f"Thread error for search item: {exc}")
                    results.append(None)
        return results

# from concurrent.futures import ThreadPoolExecutor, as_completed
# from datetime import datetime
# import os
# from typing import Any, Callable, Dict, List, Optional, Union

# Assume logger is defined somewhere
#
    def build_date_content(self, metadata: Dict[str, Any], offset: int) -> Dict[str, Any]:
        date_time = self._build_valid_datetime_from_metadata(metadata, offset)
        return {
            "datetime": date_time.isoformat(),
            "offset": offset,
            "day": date_time.day,
            "hour": date_time.hour
        }

    def get_point_forecast_timeseries(
        self,
        model: str,
        param_keys: Union[str, List[Union[str, Dict[str, Any]]]],
        lat: float,
        lon: float,
        start_hour_offset: int = 0,
        total_days: int = 10,
        step_hours: int = 3,
        level: Optional[int] = None,
        type_of_level: Optional[str] = None,
        step_type: Optional[str] = None,
        callback: Optional[Callable[[int, int], None]] = None
    ) -> List[Dict[str, Any]]:
        """
        Build a forecast timeseries and call the callback each time all futures for an offset are resolved.
        The callback is called with (completed_offsets, total_offsets).
        """
        # Normalize param_keys to a list
        if isinstance(param_keys, (str, dict)):
            param_keys = [param_keys]

        offsets = list(range(start_hour_offset, start_hour_offset + total_days * 24 + 1, step_hours))
        total_offsets = len(offsets)
        # For each offset, we expect one future per param_key.
        outstanding = {off: len(param_keys) for off in offsets}
        completed_offsets = 0

        results = []
        with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as executor:
            future_map = {}
            for off in offsets:
                values_dict = self._get_or_build_value_cache_dictionary(
                    model, param_keys, lat, lon, off, level, type_of_level, step_type
                )
                map_values = self.pull_worker_map_values(model, lat, lon, off, level, type_of_level, step_type)
                for pk in param_keys:
                    fut = executor.submit(map_values, pk, values_dict)
                    future_map[fut] = (off, self.get_key_string(pk))
            for fut in as_completed(future_map):
                off, local_param_key = future_map[fut]
                try:
                    res = fut.result()
                    if res:
                        results.append({
                            # "offset": off,
                            "param_key": local_param_key,
                            "value": res["value"],
                            "units": res["units"],
                            "metadata": res["metadata"],
                            **self.build_date_content(res["metadata"], off)
                            # "datetime": self._build_valid_datetime_from_metadata(res["metadata"], off).isoformat()
                        })
                except Exception as e:
                    logger.error(f"Error processing offset {off} for param {local_param_key}: {e}")
                # Decrement the count for this offset.
                outstanding[off] -= 1
                if outstanding[off] == 0:
                    completed_offsets += 1
                    if callback:
                        callback(completed_offsets, total_offsets)
        # Group results by parameter key.
        final_results = {}
        for r in results:
            pk = r["param_key"]
            if pk not in final_results:
                final_results[pk] = {"values": [], "metadata": r.get("metadata", {})}
            final_results[pk]["values"].append({
                "datetime": r["datetime"],
                "value": r["value"],
                "offset": r["offset"],
                "day": r["day"],
                "hour": r["hour"]
            })
        for res in final_results.values():
            res["values"] = sorted(
                res["values"],
                key=lambda x: datetime.fromisoformat(x["datetime"])
            )
        final_list = list(final_results.values())
        return final_list

    def _get_grib_array_values_key(
        self,
        param_name: str,
        model: str,
        hour_offset: int,
        level: Optional[int] = None,
        type_of_level: Optional[str] = None,
        step_type: Optional[str] = None,
    ):
        hour_key = self.todays_hour_with_date(hour_offset)
        p_key = self.make_param_key(param_name)
        return f"grib_array:{model}:{hour_key}:{p_key}:{level}:{type_of_level}:{step_type}"

    def _get_grib_dict_values_key(
        self,
        model: str,
        hour_offset: int,
        level: Optional[int] = None,
        type_of_level: Optional[str] = None
    ):
        hour_key = self.todays_hour_with_date(hour_offset)
        return f"grib_dictionary_array:{model}:{hour_key}"

    def _get_or_build_value_cache_dictionary(
        self,
        model: str,
        search_parameters: List[Any],
        lat: float,
        lon: float,
        hour_offset: int,
        level: Optional[int] = None,
        type_of_level: Optional[str] = None,
        step_type: Optional[str] = None,
    ):
        cache_key = self._get_grib_dict_values_key(model, hour_offset)
        values = self._cache_get(cache_key) or {}
        param_keys = self.get_all_missing_key_strings(search_parameters, values, level, type_of_level, step_type)
        if len(param_keys) == 0:
            return values
        pm = self.build_param_map_for_offset(model)
        grbs = self._get_raw_grib(model, hour_offset)
        if not grbs:
            logger.warning(f"No GRIB file for {model} offset {hour_offset}")
            return values
        for param_key in param_keys:
            key_name = param_key.get("param_key")
            param_name = pm.get(key_name)
            if not param_name:
                logger.warning(f"No parameter name for {param_name}")
                continue
            try:
                result = self._set_cached_grib_values(
                    grbs,
                    param_name,
                    model,
                    hour_offset,
                    param_key.get("level", level),
                    param_key.get("typeOfLevel", type_of_level),
                    param_key.get("stepType", step_type)
                )
                values[param_key.get("key", key_name)] = result
            except Exception as exc:
                logger.error(f"Error building interpolator: {exc}", exc_info=True)
        grbs.close() # type: ignore
        self._cache_set(cache_key, values)
        return values

    def _set_cached_grib_values(
        self,
        grbs: Any,
        param_name: str,
        model: str,
        hour_offset: int,
        level: Optional[int] = None,
        type_of_level: Optional[str] = None,
        step_type: Optional[str] = None,
        grbSearch: Optional[Dict[str, Any]] = None
    ):
        cache_key = self._get_grib_array_values_key(param_name, model, hour_offset, level, type_of_level, step_type)
        def compute():
            try:
                g = self._select_grib_message(grbs, param_name , level, type_of_level, step_type, grbSearch)
                if not g:
                    logger.warning(f"No suitable GRIB message found for {param_name}")
                    return None
                data_array = g.values
                lat_array, lon_array = g.latlons()
                if getattr(g, "jScansPositively", None) == 0:
                    data_array = np.flipud(data_array)
                    lat_array = np.flipud(lat_array)
                    lon_array = np.flipud(lon_array)
                # Apply decimation.
                if self.decimation > 1:
                    data_array = data_array[::self.decimation, ::self.decimation]
                    lat_array = lat_array[::self.decimation, ::self.decimation]
                    lon_array = lon_array[::self.decimation, ::self.decimation]
                meta = self._extract_grib_metadata(g)
                return (data_array, lat_array, lon_array, meta, hour_offset)
            except Exception as e:
                logger.error(f"Error building interpolator: {e}", exc_info=True)
                return None
        return self._get_or_compute(cache_key, compute, CACHE_TTL * 3)

    def _append_file_meta_values(self, fp: str, hour_offset: int, search: Dict[str, Any] = {}) :
        parts = fp.split(os.sep)
        # parts[-3] should be the date directory, parts[-2] the run‐hour, and parts[-1] the filename.
        date_dir = parts[-3]
        run_hour = parts[-2]

        filename = parts[-1]
        # Find the 'fXXX' portion before ".grib2"
        m = re.search(r'\.f(\d+)(?:\.grib2)?$', filename)
        if not m:
            raise ValueError(f"Could not extract forecast hour from '{filename}'")
        forecast_hr = int(m.group(1))
        search["forecast_hr"] = forecast_hr
        search["offset"] = hour_offset
        search["fp"] = fp
        search["run_hour"] = run_hour
        search["date"] = date_dir

    def _get_raw_grib(self, model: str, hour_offset: int, search: Optional[Dict[str, str]] = None) -> Optional[List[bytes]]: # Optional[List[Any]] :
        fp = self.get_grib_file(model, hour_offset)
        if fp is None:
            logger.warning(f"No GRIB file for {model} offset {hour_offset} {fp}")
            return None

        try:
            if search is not None:
               self._append_file_meta_values(fp, hour_offset, search)
            return pygrib.open(fp) # type: ignore
        except Exception as exc:
            logger.error(f"Error reading GRIB file {fp}: {exc}", exc_info=True)
            return None

    # -------------------------------------------------------------------------
    # Multi-Day Forecast Timeseries
    # -------------------------------------------------------------------------
    def build_paramter_name_list(self, model: str, hour_offset: int):
        if not self.valid_model(model):
            raise ValueError(f"Unknown model: {model}")
        fp = self.get_grib_file(model, hour_offset)
        if not fp or not os.path.exists(fp):
            raise ValueError(f"No file found for {model}, offset={hour_offset}.")
        return self.build_param_map_for_offset(model, hour_offset)

    # -------------------------------------------------------------------------
    # Updating min/max with caching (for consistent tile color range)
    # -------------------------------------------------------------------------
    def update_and_get_min_max(
        self,
        model: str,
        param_key: str,
        level: int,
        type_of_level: str,
        step_type: str,
        new_min: float,
        new_max: float
    ) -> Tuple[float, float]:
        cache_key = f"max:min:{model}:{param_key}:{level}:{type_of_level}:{step_type}"
        cached = self.cache.get(cache_key)
        if cached:
            try:
                current_min, current_max = json.loads(cached)
                updated_min = min(current_min, new_min)
                updated_max = max(current_max, new_max)
            except (json.JSONDecodeError, TypeError, ValueError):
                updated_min, updated_max = new_min, new_max
        else:
            updated_min, updated_max = new_min, new_max
        self.cache.set(cache_key, json.dumps((updated_min, updated_max)))
        return updated_min, updated_max

    def apply_parameter_meta(self, param_key: str, p_info: dict, model_key: str) -> dict:
        return apply_parameter_meta(param_key, p_info, model_key)

    # --- Caching Helper Methods ---
    def _cache_get(self, key: str) -> Optional[Any]:
        cached = self.cache.get(key)
        if cached is not None:
            try:
                return pickle.loads(cached)
            except Exception as e:
                logger.error(f"Error unpickling cache key {key}: {e}")
        return None

    def _cache_set(self, key: str, value: Any, expire: int = CACHE_TTL) -> None:
        try:
            self.cache.set(key, pickle.dumps(value, protocol=4), expire=expire)
        except Exception as e:
            logger.error(f"Error setting cache key {key}: {e}")

    def _get_or_compute(self, key: str, compute_fn: Callable[[], Any], expire: int = CACHE_TTL) -> Any:
        """Return value from cache or compute it (with per‑resource locking) and cache it."""
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        lock = self.key_locks.setdefault(key, threading.Lock())
        with lock:
            # Check again in case another thread computed while waiting
            cached = self._cache_get(key)
            if cached is not None:
                if expire > 0:
                    self.cache.extend(key, expire)
                return cached
            result = compute_fn()
            if result is not None:
                self._cache_set(key, result, expire)
            return result
