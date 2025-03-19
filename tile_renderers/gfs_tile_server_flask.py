#!/usr/bin/env python3

"""
global_norm_map_server_lru.py

A streamlined Flask server that:
  1) Uses a single 'get_or_build_interpolator' function for thread-safe caching and
     building the interpolator (only one thread loads at a time).
  2) Implements LRU + time-based pruning to manage memory usage.
  3) Provides a '/prewarm/<model>/<hour_offset>' route to load entire parameter sets
     in advance, avoiding slow first-tiles.
  4) Detects sentinel 'missingValue' with floating tolerance via np.isclose
     so e.g. 9998.999 is recognized as missing.
  5) Dynamically determines whether to flip latitudes by checking "jScansPositively"
     in each GRIB message (if jScansPositively=0, lat_flip=True).

Routes:
  - /tiles/<model>/<param_key>/<hour_offset>/<z>/<x>/<y>.png
  - /list_grib_parameters/<model>/<hour_offset>
  - /parameters
  - /prewarm/<model>/<hour_offset>
"""

import io
import logging
from flask import Flask, send_file, abort, jsonify, request
from flask_caching import Cache

# optional parameter metadata merges

from gfs_render import TileRendering, ModelService, RedisCacheBackend
##############################################################################
# Flask Setup
##############################################################################
backend_cache = RedisCacheBackend()
model_service = ModelService(backend_cache)
from werkzeug.routing import BaseConverter
from typing import Any
app = Flask(__name__)

cache_config = {
    "CACHE_TYPE": "RedisCache",
    "CACHE_REDIS_HOST": "localhost",
    "CACHE_REDIS_PORT": 6379,
    "CACHE_DEFAULT_TIMEOUT": 900,  # 15min for tile PNG caching
}
app.config.from_mapping(cache_config)
cache = Cache(app)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Custom converter for signed integers
class SignedIntConverter(BaseConverter):
    regex = r"-?\d+"
    def to_python(self, value: str) -> int:
        """ Convert the matched value to an integer. """
        return int(value)

    def to_url(self, value: Any) -> str:
        """ Convert the integer back to a string when building URLs. """
        return str(value)
app.url_map.converters['signed_int'] = SignedIntConverter
##############################################################################
# Flask Routes
##############################################################################
@app.route("/")
def index():
    return ("Map Server with single 'get_or_build_interpolator' method for thread-safe caching, "
            "LRU/time pruning, missingValue handling via np.isclose(), "
            "and dynamic lat_flip via jScansPositively=0")



@app.route("/tiles/<string:model>/<string:param_key>/<signed_int:hour_offset>/<int:z>/<int:x>/<int:y>.png")
def serve_tile_route(model, param_key, hour_offset, z, x, y):
    user_tof = request.args.get("typeOfLevel", default=None)
    level_arg = request.args.get("level", type=int, default=None)
    step_type = request.args.get("stepType", type=str, default=None)
    renderer = TileRendering(model_service)
    if not model_service.valid_model(model):
        return abort(404, "Unknown model.")
    if not renderer.valid_zxy(z, x, y):
        return abort(404, "Invalid tile coords.")

    cache_key = model_service.create_tile_cache_key(model, param_key, user_tof, hour_offset, z, x, y, level_arg, step_type)
    print('MY CACHE KEY', cache_key)
    try:
        cached_tile = backend_cache.get(cache_key)
        if cached_tile:
            return send_file(io.BytesIO(cached_tile), mimetype="image/png")
    except Exception as e:
        logger.error(f"Error reading cache: {e}")

    data = renderer.render_tile(model, param_key, hour_offset, z, x, y, level_arg, user_tof, step_type)
    if data is None:
        return abort(404, "No tile data found")

    try:
        backend_cache.set(cache_key, data, 15 * 60 )
    except Exception as e:
        logger.error(f"Error writing cache: {e}")

    response = send_file(io.BytesIO(data), mimetype="image/png");
    # response.headers["Cache-Control"] = "public, max-age=900"
    return response

@app.route("/list_grib_parameters/<string:model>/<int:hour_offset>", methods=["GET"])
def list_params(model, hour_offset):
    try:
        params = model_service.build_paramter_name_list(model, hour_offset)
        return jsonify({"parameters": sorted(params.values())})
    except Exception as e:
        logger.error(f"Error listing for {model},{hour_offset}: {e}")
        return abort(500, "GRIB reading error")

@app.route("/parameters", methods=["GET"])
def parameters_route():
    """
    Enumerate param info for each model, using hour_offset=0 by default.
    Merges optional metadata from parameter_meta.py via apply_parameter_meta.
    """
    offset = 0
    results = model_service.parameter_definitions(offset);
    return jsonify({"models": results})

@app.route("/point", defaults={'hour_offset': 0}, methods=["POST"])
@app.route("/point/<signed_int:hour_offset>", methods=["POST"])
def point_forecast_params_route(hour_offset):
    """
    We can call the api in a few ways:
        1) with a list of string search parameters,
        2) with a search object that takes the level details and the parameter name in the form of:
        { param_key: "temperature", level: 0, typeOfLevel: "surface", "model": "gfs"  }
    """
    data = request.get_json()  # Expect a JSON body with lat/lon, level, type_ofLevel
    if not data:
        return abort(400, "No JSON body provided.")

    lat = data.get("lat")
    lon = data.get("lon")
    user_tof = data.get("typeOfLevel", None)
    level_arg = data.get("level", None)
    model = data.get("model", None)
    search_parameters = data.get("param_keys", None) # { param_key: "temperature", level: 0, typeOfLevel: "surface", "model": "gfs"  }
    step_type = data.get("stepType", None)
    if search_parameters is None:
        return abort(400, "Missing 'search' in JSON body.")

    if lat is None or lon is None:
        return abort(400, "Missing 'lat' or 'lon' in JSON body.")

    try:
        values = model_service.iterate_multiple_keys_against_geo_point(
            model,
            search_parameters,
            lat,
            lon,
            hour_offset,
            level_arg,
            user_tof,
            step_type
        )

        if len(values) == 0:
            return abort(404, "No forecast value found at this point.")
        else:
            return jsonify(values)
    except Exception as e:
        return abort(404, e)

@app.route("/point/<string:model>/<string:param_key>/<signed_int:hour_offset>", methods=["POST"])
def point_forecast_route(model, param_key, hour_offset):
    data = request.get_json()  # Expect a JSON body with lat/lon, level, type_ofLevel
    if not data:
        return abort(400, "No JSON body provided.")

    lat = data.get("lat")
    lon = data.get("lon")
    user_tof = data.get("typeOfLevel", None)
    level_arg = data.get("level", None)
    total_days = data.get("total_days", 10)
    step_hours = data.get("step_hours", 3)
    step_type = data.get("stepType", None)
    if lat is None or lon is None:
        return abort(400, "Missing 'lat' or 'lon' in JSON body.")
    val = model_service.get_point_forecast_timeseries(
        model=model,
        param_keys=param_key,
        lat=lat,
        lon=lon,
        start_hour_offset=hour_offset,
        level=level_arg,
        type_of_level=user_tof,
        total_days=total_days,
        step_hours=step_hours,
        step_type=step_type
    )

    if val is None:
        return abort(404, "No forecast value found at this point.")
    else:
        return jsonify(val)

@app.route("/forecast", methods=["POST"])
def forecast_route():
    """
    POST body JSON example:
    {
      "model": "gfs",
      "param_keys": ["temperature", "wind-speed"],
      "lat": 38.5,
      "lon": -90.2,
      "start_hour_offset": 0,
      "total_days": 10,
      "step_hours": 3,
      "level": 0,
      "typeOfLevel": "surface"
    }
    """

    data = request.get_json()
    if not data:
        return abort(400, "No JSON body provided.")

    # Extract required fields with defaults or validation
    model = data.get("model")
    param_keys = data.get("param_keys")
    lat = data.get("lat")
    lon = data.get("lon")

    if model is None or not param_keys or lat is None or lon is None:
        return abort(400, "Missing required fields: model, param_keys, lat, lon.")

    # Optionals
    start_hour_offset = data.get("start_hour_offset", 0)
    total_days = data.get("total_days", 10)
    step_hours = data.get("step_hours", 3)
    level_arg = data.get("level", None)
    user_tof = data.get("typeOfLevel", None)
    # Build the forecast
    timeseries = model_service.get_point_forecast_timeseries(
        model=model,
        param_keys=param_keys,
        lat=lat,
        lon=lon,
        start_hour_offset=start_hour_offset,
        total_days=total_days,
        step_hours=step_hours,
        level=level_arg,
        type_of_level=user_tof
    )

    # If we got no results at all, you can decide to 404 or return []
    if not timeseries:
        return jsonify([]), 200
    return jsonify(timeseries), 200
#
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5001)
