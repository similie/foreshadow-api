import pickle
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Any
from datetime import datetime, timedelta
import logging
import os
from gfs_render import ModelService, SystemConfig
from tile_renderers.gfs_render.caching.redis_cache import RedisCacheBackend
from .caching.cache import  CACHE_TTL

logger = logging.getLogger(__name__)

class RedisLayerCache:
    """
    Redis-backed cache of interpolator layers for each (param_key, hour_offset).
    Stores each param's full offset map in a Redis hash to minimize round-trips.
    """
    def __init__(
        self,
        model_service: ModelService,
        cache_backend: RedisCacheBackend
    ):
        self.model_service = model_service
        self.redis = cache_backend  # must expose .client for hash ops

        cfg = SystemConfig().get_default_forecast_json()
        self.model = cfg["model"]
        self.param_keys = cfg["param_keys"]   # List[dict]
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
        logger.info(f"[RedisLayerCache] Preloading {len(self.offsets)} offsets × {len(self.param_keys)} params using {workers} workers")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            executor.map(self._preload_param, self.param_keys)

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
        logger.info(f"[Preload] Param={pk} into hash {hkey}")
        for off in self.offsets:
            hour_key = self.model_service.todays_hour_with_date(off)
            # skip if already exists
            if self.redis.client.hexists(hkey, hour_key):
                continue
            logger.debug(f"[Preload] Building interpolator for {pk}@{hour_key}")
            ip = self.model_service.get_or_build_interpolator(
                self.model, pk, off,
                entry.get("level"), entry.get("typeOfLevel"), entry.get("stepType")
            )
            if not ip:
                logger.warning(f"[Preload] No data for {pk}@{hour_key}")
                continue
            blob = pickle.dumps(ip, protocol=4)
            self.redis.client.hset(hkey, hour_key, blob)
        logger.info(f"[Preload] Completed hash {hkey}")

    def get_slices(
        self,
        lat: float,
        lon: float,
        start_offset: int = 0
    ) -> List[Dict[str, Any]]:
        """
        Returns a full multi-offset forecast timeseries for given geopoint.
        Pulls each param's entire hash in one HGETALL, then computes values.
        """
        now = datetime.utcnow()
        if now.minute >= 30:
            now += timedelta(hours=1)
        base_time = now.replace(minute=0, second=0, microsecond=0)

        offsets = [off for off in self.offsets if off >= start_offset]
        logger.info(f"[Fetch] Generating timeseries for offsets {offsets}")

        # Pipeline HGETALL for all params
        pipe = self.redis.client.pipeline()
        for entry in self.param_keys:
            hkey = self._hash_key(entry["param_key"])
            pipe.hgetall(hkey)
        hash_maps = pipe.execute()  # List[Dict[bytes,bytes]]

        final: List[Dict[str, Any]] = []
        for entry, hmap in zip(self.param_keys, hash_maps):
            pk = entry["param_key"]
            values = []
            for off in offsets:
                hour_key = self.model_service.todays_hour_with_date(off)
                raw = hmap.get(hour_key.encode())
                if not raw:
                    logger.debug(f"[Fetch] Missing {pk}@{hour_key}")
                    continue
                ip = pickle.loads(raw)
                try:
                    val = float(ip(lat, lon))
                except Exception as e:
                    logger.error(f"[Fetch] Error computing {pk}@{hour_key}: {e}")
                    continue
                dt = base_time + timedelta(hours=off)
                values.append({"datetime": dt.isoformat(), "value": val})
            # sort and append
            values.sort(key=lambda x: x["datetime"])
            final.append({"param_key": pk, "values": values})

        return final
