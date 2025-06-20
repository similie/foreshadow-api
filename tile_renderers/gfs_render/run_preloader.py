
import logging
import asyncio
# from gfs_render import ModelService, RedisCacheBackend, MemoryLayerCache
from .model_service import ModelService
from .caching.redis_cache import RedisCacheBackend
from .memory_layer_cache import MemoryLayerCache
from dotenv import load_dotenv, find_dotenv
env_file = find_dotenv()                     # returns path or ''
print("Loading .env from:", env_file)
load_dotenv(env_file, verbose=True)
# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Initialize the cache backend and ModelService.
backend_cache = RedisCacheBackend()
# Set preload_layers=True if you want to prewarm interpolators on startup.
model_service = ModelService(backend_cache)

layer_cache = MemoryLayerCache(
    model_service,
    backend_cache,
)

async def _prewarm_loop(
    interval_s: float = 60.0,
):
    loop = asyncio.get_event_loop()
    while True:
        # pick random lat/lon in valid ranges
        print("Running preloader...")
        try:
            # layer_cache.loadOffset();
            await loop.run_in_executor(
                None,
                lambda:layer_cache.preload_slices()
            )
        except Exception as exc:
            logger.error(f"Pre-warm failed {exc}")
        # wait before next one
        #
        print("PRELOAD EXECUTION COMPLETE")
        await asyncio.sleep(interval_s)

if __name__ == "__main__":
    print("Starting Preloader")
    asyncio.run(_prewarm_loop(600.0))
