
import logging
import asyncio
from gfs_render import ModelService, RedisCacheBackend, MemoryLayerCache
# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Initialize the cache backend and ModelService.
backend_cache = RedisCacheBackend()
# Set preload_layers=True if you want to prewarm interpolators on startup.
model_service = ModelService(backend_cache)

layer_cache = MemoryLayerCache(
    model_service,
    backend_cache
)

async def _prewarm_loop(
    interval_s: float = 60.0,
):
    loop = asyncio.get_event_loop()
    while True:
        # pick random lat/lon in valid ranges
        try:
            # layer_cache.loadOffset();
            await loop.run_in_executor(
                None,
                lambda:layer_cache.preload()
            )
        except Exception as exc:
            logger.error(f"Pre-warm failed {exc}")
        # wait before next one
        #
        print("PRELOAD EXECUTION COMPLETE")
        await asyncio.sleep(interval_s)

if __name__ == "__main__":
    asyncio.create_task(_prewarm_loop(600.0))
