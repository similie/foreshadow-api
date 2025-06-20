#!/usr/bin/env python3
"""
fastapi_server.py

A FastAPI server that:
  1) Uses a single 'get_or_build_interpolator' method for thread‐safe caching and building the interpolator.
  2) Implements LRU/time‐based pruning for managing memory.
  3) Provides routes for:
     - /tiles/{model}/{param_key}/{hour_offset}/{z}/{x}/{y}.png
     - /list_grib_parameters/{model}/{hour_offset}
     - /parameters
     - /prewarm/{model}/{hour_offset}
     - /point (POST) for point forecasts (optionally with a path hour_offset).
  4) Handles missing values (using np.isclose) and dynamic lat_flip (via jScansPositively).

Run with multiple worker processes (via Uvicorn) to help with CPU‐bound work.
"""
import json
import os
import io
import logging
from typing import  List, Optional, Union
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse
import asyncio
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv, find_dotenv
from multiprocessing.managers import BaseManager
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from gfs_render import ModelService, RedisCacheBackend, TileRendering, MemoryLayerCache, LocalStorage

env_file = find_dotenv()                     # returns path or ''
print("Loading .env from:", env_file)
load_dotenv(env_file, verbose=True)


class CacheManager(BaseManager):
    def LocalStorage(self) -> LocalStorage:  # noqa: F821
        ...
# NOTE: here we do *not* register the implementation class,
# we only declare that `LocalStorage` will exist on the server side.
CacheManager.register("LocalStorage")

# Point to the same address/authkey you used above:
_mgr = CacheManager(address=('127.0.0.1', 50000), authkey=b'secret')
_mgr.connect()  # join the remote manager, do not start a new server

# Grab the shared LocalStorage proxy:
_shared_local_storage = _mgr.LocalStorage()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Initialize the cache backend and ModelService.
backend_cache = RedisCacheBackend()
# Set preload_layers=True if you want to prewarm interpolators on startup.
model_service = ModelService(backend_cache, _shared_local_storage)
tile_renderer = TileRendering(model_service)

layer_cache = MemoryLayerCache(
    model_service,
    backend_cache,
    _shared_local_storage
)



app = FastAPI(title="Global Norm Map Server", version="1.0")
# (Optional) Add CORS middleware if needed.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
###############################################################################
# Pydantic models for POST requests
###############################################################################
class PointForecastRequest(BaseModel):
    model: str
    param_keys: Union[str, List[Union[str, dict]]]
    lat: float
    lon: float
    start_hour_offset: Optional[int] = 0
    total_days: Optional[int] = 10
    step_hours: Optional[int] = 3
    level: Optional[int] = None
    typeOfLevel: Optional[str] = None

###############################################################################
# Routes
###############################################################################
##############################################################################
# Routes
##############################################################################

@app.get("/")
def index():
    return {
        "message": (
            "Map Server with single 'get_or_build_interpolator' method for thread-safe caching, "
            "LRU/time pruning, missingValue handling via np.isclose(), and dynamic lat_flip via jScansPositively=0."
        )
    }

@app.get("/tiles/{model}/{param_key}/{hour_offset}/{z}/{x}/{y}.png")
def serve_tile_route(
    model: str,
    param_key: str,
    hour_offset: int,
    z: int,
    x: int,
    y: int,
    typeOfLevel: str|None = None,
    level: int|None = None,
    stepType: str|None = None
):
    # timer = TimeLogger()
    # timer.log("I STARTED MY RENDER 1")
    if not model_service.valid_model(model):
        raise HTTPException(status_code=404, detail="Unknown model.")
    if not tile_renderer.valid_zxy(z, x, y):
        raise HTTPException(status_code=404, detail="Invalid tile coordinates.")

    cache_key = model_service.create_tile_cache_key(model, param_key, typeOfLevel, hour_offset, z, x, y, level, stepType)
    try:
        cached_tile = backend_cache.get(cache_key)
        if cached_tile:
            return StreamingResponse(io.BytesIO(cached_tile), media_type="image/png")
    except Exception as e:
        logger.error(f"Error reading cache: {e}")

    data = tile_renderer.render_tile(model, param_key, hour_offset, z, x, y, level, typeOfLevel, stepType)
    if data is None:
        raise HTTPException(status_code=404, detail="No tile data found")
    try:
        backend_cache.set(cache_key, data, 15 * 60)
    except Exception as e:
        logger.error(f"Error writing cache: {e}")
    # timer.log("I ENDED MY RENDER 2")
    return StreamingResponse(io.BytesIO(data), media_type="image/png")


@app.get("/list_parameters/{model}/{hour_offset}")
def list_params(model: str, hour_offset: int):
    try:
        params = model_service.build_paramter_name_list(model, hour_offset)
        return JSONResponse(content={"parameters": sorted(list(params.values()))})
    except Exception as e:
        logger.error(f"Error listing for {model},{hour_offset}: {e}")
        raise HTTPException(status_code=500, detail="GRIB reading error")


@app.get("/parameters")
def parameters_route():
    offset = 0
    results = model_service.parameter_definitions(offset)
    return JSONResponse(content={"models": results})


@app.post("/point", response_model=dict)
@app.post("/point/{hour_offset}", response_model=dict)
async def point_forecast_params_route(request: Request, hour_offset: int = 0):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="No JSON body provided.")

    lat = data.get("lat")
    lon = data.get("lon")
    user_tof = data.get("typeOfLevel")
    level_arg = data.get("level")
    model = data.get("model")
    search_parameters = data.get("param_keys")
    step_type = data.get("stepType")
    start_hour_offset = data.get("start_hour_offset", hour_offset)
    if search_parameters is None or lat is None or lon is None:
        raise HTTPException(status_code=400, detail="Missing required fields in JSON body.")

    try:
        values = model_service.iterate_multiple_keys_against_geo_point(
            model,
            search_parameters,
            lat,
            lon,
            start_hour_offset,
            level_arg,
            user_tof,
            step_type
        )
        if not values:
            raise HTTPException(status_code=404, detail="No forecast value found at this point.")
        return JSONResponse(content=values)
    except Exception as e:
        logger.error(f"Error in point forecast: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/forecast-stream")
async def forecast_streaming_route(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="No JSON body provided.")

    model = data.get("model")
    param_keys = data.get("param_keys")
    lat = data.get("lat")
    lon = data.get("lon")
    if model is None or not param_keys or lat is None or lon is None:
        raise HTTPException(status_code=400, detail="Missing required fields: model, param_keys, lat, lon.")

    start_hour_offset = data.get("start_hour_offset", 0)
    total_days = data.get("total_days", 10)
    step_hours = data.get("step_hours", 3)
    level_arg = data.get("level")
    user_tof = data.get("typeOfLevel")
    step_type = data.get("stepType")

    progress_queue = asyncio.Queue()

    # Define the callback that will be called from the thread.
    def progress_callback(completed, total):
        message = {"progress": f"{completed} of {total}"}
        # Use asyncio.run_coroutine_threadsafe to put the message in the queue.
        asyncio.run_coroutine_threadsafe(progress_queue.put(message), loop)

    loop = asyncio.get_event_loop()

    # Run the forecast computation in an executor so it doesn't block the event loop.
    timeseries_future = loop.run_in_executor(
        None,
        lambda: model_service.get_point_forecast_timeseries(
            model=model,
            param_keys=param_keys,
            lat=lat,
            lon=lon,
            start_hour_offset=start_hour_offset,
            total_days=total_days,
            step_hours=step_hours,
            level=level_arg,
            type_of_level=user_tof,
            step_type=step_type,
            callback=progress_callback
        )
    )

    async def stream_forecast():
        # While the forecast computation is running, yield progress messages.
        while not timeseries_future.done() or not progress_queue.empty():
            try:
                msg = await asyncio.wait_for(progress_queue.get(), timeout=1)
                yield f"{json.dumps(msg)}\n\n"
            except asyncio.TimeoutError:
                # If no progress message, yield a keep-alive (optional)
                yield "{}\n\n"
        # Once done, yield the final timeseries.
        timeseries = await timeseries_future
        yield f"{json.dumps({'timeseries': timeseries})}\n\n"

    return StreamingResponse(stream_forecast(), media_type="text/event-stream")

@app.get("/point", response_model=dict)
def forecast_point(lat: float, lon: float, hour_offset: int = 0):
    if layer_cache.is_loading():
        raise HTTPException(status_code=404, detail="Data is not ready for output.")
    try:
        result = layer_cache.find_current_slice(lat, lon, hour_offset)
        if not result:
            raise HTTPException(status_code=404, detail="No data for that offset.")
        return JSONResponse(content=result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/forecast")
def forecast(lat: float, lon: float, hour_offset: int = 0):
    if layer_cache.is_loading():
        raise HTTPException(status_code=404, detail="Data is not ready for output.")
    try:
        result = layer_cache.find_slice(lat, lon, hour_offset)
        if not result:
            raise HTTPException(status_code=404, detail="No data for that offset.")
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"Error in preconfigured forecast: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/forecast")
async def forecast_route(request: Request):
    """
    POST JSON example:
    {
      "model": "gfs",
      "param_keys": ["temperature", "wind-speed", {"param_key": "precipitation", "level": 0, "typeOfLevel": "surface", ""}],
      "lat": 38.5,
      "lon": -90.2,
      "start_hour_offset": 0,
      "total_days": 10,
      "step_hours": 3,
      "level": 0,
      "typeOfLevel": "surface"
    }
    """
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="No JSON body provided.")
    model = data.get("model")
    param_keys = data.get("param_keys")
    lat = data.get("lat")
    lon = data.get("lon")
    if model is None or not param_keys or lat is None or lon is None:
        raise HTTPException(status_code=400, detail="Missing required fields: model, param_keys, lat, lon.")
    start_hour_offset = data.get("start_hour_offset", 0)
    total_days = data.get("total_days", 10)
    step_hours = data.get("step_hours", 3)
    level_arg = data.get("level")
    user_tof = data.get("typeOfLevel")
    step_type = data.get("stepType")

    timeseries = model_service.get_point_forecast_timeseries(
        model=model,
        param_keys=param_keys,
        lat=lat,
        lon=lon,
        start_hour_offset=start_hour_offset,
        total_days=total_days,
        step_hours=step_hours,
        level=level_arg,
        type_of_level=user_tof,
        step_type=step_type
    )
    print("MY TIME SERIES", timeseries)
    if not timeseries:
        return JSONResponse(content=[], status_code=200)
    return JSONResponse(content=timeseries)
#———————————————————————————————
# 1) the “pre-warm” worker
#———————————————————————————————

def run_workers():
    try:
        layer_cache.preload_slices()
        layer_cache.preload_tiles()
    except Exception as e:
        print(f"Error in run_workers: {e}")

async def _prewarm_loop(
    interval_s: float = 60.0,
):
    try:
        # loop = asyncio.get_running_loop()
        # last_future = None
        while True:
            # pick random lat/lon in valid ranges
            print("PRELOAD EXECUTION STARTED")
            try:
               await asyncio.to_thread(run_workers)
                # if last_future is None or last_future.done():
                #     logger.info("Submitting new prewarm task")
                #     last_future = prewarm_process_executor.submit(run_workers)
                # else:
                #     logger.info("Previous prewarm still running, skipping this cycle")

                # await asyncio.sleep(interval_s)
                # layer_cache.loadOffset();
                # await asyncio.to_thread(run_workers)
                # await loop.run_in_executor(
                #    prewarm_executor,
                #    run_workers #layer_cache.preload_to_local()
                # )
                # prewarm_process_executor.submit(do_prewarm)
                # await loop.run_in_executor(
                #    None,
                #    lambda: run_workers() #layer_cache.preload_to_local()
                # )
            except Exception as exc:
                logger.error(f"Pre-warm failed {exc}")
            # wait before next one
            #
            print("PRELOAD EXECUTION COMPLETE")
            await asyncio.sleep(interval_s)
    except Exception as outer_exc:
        logger.critical(f"_prewarm_loop has died with: {outer_exc}", exc_info=True)
        start_prewarm()

def start_prewarm():
    print("GETTING STARTING WITH PREWARMING")
    loop = asyncio.get_running_loop()
    loop.create_task(_prewarm_loop(600.0))
#———————————————————————————————
# 2) start it on app startup
#———————————————————————————————
@app.on_event("startup")
async def kick_off_prewarm():
    print("GETTING STARTING WITH PREWARMING")
    # start_prewarm()
###############################################################################
# Main entry point
###############################################################################
if __name__ == "__main__":
    # Run with multiple worker processes to distribute CPU-bound tasks.
    uvicorn.run("main:app", host="0.0.0.0", port=5001, reload=True, workers=os.cpu_count() or 4)
