import logging
import io
import numpy as np
from PIL import Image
from .model_service import ModelService
from .map_colors import MapColors
# from .time_logger import TimeLogger
from .system_config import SystemConfig
logger = logging.getLogger(__name__)

class TileRendering:
    def __init__(self, model_service: ModelService) -> None:
        self.model_service = model_service
        self.colors = MapColors()
        self.config = SystemConfig()

    def render_tile(
        self,
        model: str,
        param_key: str,
        hour_offset: int,
        z: int,
        x: int,
        y: int,
        level: int|None = None,
        type_of_level: str|None = None,
        step_type: str|None = None
    ) -> bytes | None:
        # timer = TimeLogger()
        # timer.log("Start render_tile")
        oversize = self.config.get_oversize()
        pts = self.config.get_tile_pts_boundaries(z, x, y, oversize)
        # timer.log("Got the pts")
        iterp_key = self.model_service.get_interpolator_cache_key(
            model, param_key, hour_offset, level, type_of_level, step_type
        )
        ip = self.model_service.get_or_build_interpolator(
             model, param_key, hour_offset, level, type_of_level, step_type
        )
        # timer.log("I have the IP")
        if not ip:
            logger.warning("No interpolator found => returning None.")
            return None

        gmin = float(getattr(ip, "gmin", 0.0))
        gmax = float(getattr(ip, "gmax", 1.0))
        missing_val = float(getattr(ip, "missing_val", 9999.0))
        try:
            grid_z = self.model_service.get_or_build_tile_grid(ip, pts, iterp_key, oversize)
            # timer.log("Built GridZ")
            mask_sentinel = np.isclose(grid_z, missing_val, atol=1.0)
            grid_z[mask_sentinel] = np.nan
            if np.isnan(grid_z).all():
                logger.info("All cells missing => blank tile.")
                return self._blank_tile()
            # 3) colorize => RGBA (oversize,oversize,4)
            rgba = self.colors.colorize_grid(
                model=model,
                param_name=param_key,
                data_2d=grid_z,
                gmin=gmin,
                gmax=gmax,
                missing_mask=np.isnan(grid_z)
            )
            # timer.log("RGB Done")
            # 4) Crop => (256,256)
            tile_img = Image.fromarray(rgba, "RGBA").crop((0, 0, 256, 256))
            # 5) Convert to PNG
            out_buf = io.BytesIO()
            tile_img.save(out_buf, format="PNG")
            # timer.log("SENDING")
            return out_buf.getvalue()
        except Exception as e:
            logger.error(f"Interpolation error => {e}")
            return None

    def _blank_tile(self) -> bytes:
        arr = np.zeros((256,256,4), dtype=np.uint8)
        buf = io.BytesIO()
        Image.fromarray(arr, "RGBA").save(buf, format="PNG")
        return buf.getvalue()

    def valid_zxy(self, z:int, x:int, y:int):
        return not(z < 0 or not (0 <= x < 2**z and 0 <= y < 2**z))
