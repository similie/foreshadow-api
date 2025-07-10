
import logging
import asyncio
import os
# from gfs_render import ModelService, RedisCacheBackend, MemoryLayerCache
from .model_service import ModelService
from .caching.redis_cache import RedisCacheBackend
from .caching.local_cache import LocalStorage
from .memory_layer_cache import MemoryLayerCache
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv, find_dotenv
env_file = find_dotenv()                     # returns path or ''
print("Loading .env from:", env_file)
load_dotenv(env_file, verbose=True)
# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
prewarm_executor = ThreadPoolExecutor(max_workers=os.cpu_count() or 1)
no_mem = True
# Initialize the cache backend and ModelService.
local_storage = LocalStorage()
backend_cache = RedisCacheBackend()
# Set preload_layers=True if you want to prewarm interpolators on startup.
model_service = ModelService(backend_cache, local_storage, no_mem)

async def _prewarm_loop(
    interval_s: float = 60.0,
):
    try:
        layer_cache = MemoryLayerCache(
            model_service,
            no_mem
        )
        # loop = asyncio.get_event_loop()
        while True:
            # pick random lat/lon in valid ranges
            print("Running preloader...")
            try:
                # we do this to preload into redis
               layer_cache.preloader()
            except Exception as exc:
                logger.error(f"Pre-warm failed {exc}")
            # wait before next one
            #
            # print("PRELOAD EXECUTION COMPLETE")
            await asyncio.sleep(interval_s)
    except Exception as e:
        print(f"Prelader failure {e}")

if __name__ == "__main__":
    print("Starting Preloader")
    asyncio.run(_prewarm_loop(600.0))
