#!/usr/bin/env python3
import time
import logging
from multiprocessing.managers import BaseManager

# ← adjust these imports to match your project structure
from gfs_render.caching.local_cache import LocalStorage
from gfs_render.caching.redis_cache import RedisCacheBackend
from gfs_render.model_service       import ModelService
from gfs_render.memory_layer_cache  import MemoryLayerCache

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("preloader")

# 1) CREATE exactly one LocalStorage instance
_shared_store = LocalStorage()

class CacheManager(BaseManager):
    """no extra methods"""
    pass

# 2) REGISTER a “constructor” that always returns our one shared_store
CacheManager.register(
    "LocalStorage",
    callable=lambda: _shared_store,
    exposed=["set", "get", "available", "delete", "extend"]
)

def main():
    mgr = CacheManager(address=("127.0.0.1", 50000), authkey=b"secret")
    mgr.start()   # 🔥 spins up the manager server
    log.info("CacheManager listening on 127.0.0.1:50000")

    # build your service+cache *once*
    backend = RedisCacheBackend()
    svc     = ModelService(backend, _shared_store)
    cache   = MemoryLayerCache(
        model_service=svc,
        memory=backend,
        local_storage=_shared_store
    )

    # initial warm
    log.info("⏫ initial preload…")
    cache.preload_slices()
    cache.preload_tiles()
    log.info("✅ initial preload done")

    # every 10 minutes, top up
    while True:
        time.sleep(10 * 60)
        log.info("⏫ periodic preload…")
        cache.preload_slices()
        cache.preload_tiles()
        log.info("✅ periodic preload done")

if __name__ == "__main__":
    main()
