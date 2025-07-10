# @todo:: leverage a better caching mechanism. As of right now this code is not functional
import time
from multiprocessing.managers import BaseManager
from gfs_render.caching.local_cache import LocalStorage
from gfs_render import ModelService, RedisCacheBackend
from gfs_render.memory_layer_cache import MemoryLayerCache

# 1) Define & start the Manager
class CacheManager(BaseManager):
    def LocalStorage(self) -> LocalStorage:  # noqa: F821
            ...

CacheManager.register("LocalStorage", LocalStorage)

def main():
    mgr = CacheManager(address=('127.0.0.1', 50000), authkey=b'secret')
    mgr.start()  # spins up the manager server in its own process

    # 2) Grab one shared LocalStorage proxy
    shared_store = mgr.LocalStorage()

    # 3) Build your model+cache exactly once
    backend = RedisCacheBackend()
    service = ModelService(backend)
    memory = MemoryLayerCache(
        model_service=service,
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
