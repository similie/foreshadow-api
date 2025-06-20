import time
from multiprocessing.managers import BaseManager

from .model_service import ModelService
from .memory_layer_cache import MemoryLayerCache
from .caching.redis_cache import RedisCacheBackend
from .caching.local_cache import LocalStorage
# 1) Define & start the Manager
class CacheManager(BaseManager):
    def LocalStorage(self) -> LocalStorage:
            ...

CacheManager.register("LocalStorage", LocalStorage)

def main():
    mgr = CacheManager(address=('127.0.0.1', 50000), authkey=b'secret')
    mgr.start()  # spins up the manager server in its own process

    # 2) Grab one shared LocalStorage proxy
    shared_store = mgr.LocalStorage()

    # 3) Build your model+cache exactly once
    backend = RedisCacheBackend()
    service = ModelService(backend, shared_store)
    memory = MemoryLayerCache(
        model_service=service,
        memory=backend,  # if you want full layered offsets
        local_storage=shared_store
    )

    # 4) Initial pre-warm
    # print("Initial pre-warm…")
    # memory.preload_slices()
    # memory.preload_tiles()
    # print("Initial pre-warm complete. Entering 10-minute loop.")

    # 5) Loop forever, sleeping 10 minutes between warms
    while True:

        print("Running periodic pre-warm…")
        memory.preload_slices()
        memory.preload_tiles()
        print("Preload Done.")
        time.sleep(10 * 60)

if __name__ == "__main__":
    main()
