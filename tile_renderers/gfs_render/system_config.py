import numpy as np
from typing import Dict
import os
class SystemConfig:
    def __init__(self):
        self.WEB_MERCATOR_CONSTANT = 20037508.342789244
        self.TILE_SIZE = 256
        self.decimation = 2
        self.GRIB_FILES_PATH =  os.getenv("GRIB_FILES_PATH", "/Users/guernica0131/Sites/foreshadow-api/grib")

        self.MODEL_MAP = {
            "gfs": {
                "FILE_PREFIX": "gfs",
                "FILE_CATEGORY": "pgrb2",
                "RESOLUTION": "0p25",
                "FILE_APPENDIX": ""
            },
            "gfswave": {
                "FILE_PREFIX": "gfswave",
                "FILE_CATEGORY": "global",
                "RESOLUTION": "0p25",
                "FILE_APPENDIX": ".grib2"
            }
        }
    def file_path(self):
        return self.GRIB_FILES_PATH
    def get_model_map(self) -> Dict[str, Dict[str, str]]:
        return self.MODEL_MAP
    def get_decimation(self):
        return self.decimation
    def get_web_mercator_constant(self):
        return self.WEB_MERCATOR_CONSTANT
    def get_web_mercator_x_min(self):
        return -self.WEB_MERCATOR_CONSTANT
    def get_web_mercator_x_max(self):
        return self.WEB_MERCATOR_CONSTANT
    def get_web_mercator_y_min(self):
        return -self.WEB_MERCATOR_CONSTANT
    def get_web_mercator_y_max(self):
        return self.WEB_MERCATOR_CONSTANT
    def get_oversize(self):
        return self.TILE_SIZE + 1
    def apply_oversize(self, oversize: int = 265):
        return oversize + 1
    def get_tile_pts_boundaries(self, z: int, x: int, y: int, oversize = 256):
        tile_count = 2 ** z
        tile_width = (self.get_web_mercator_x_max() - self.get_web_mercator_x_min()) / tile_count
        tile_min_x = self.get_web_mercator_x_min() + x * tile_width
        tile_max_x = tile_min_x + tile_width
        tile_min_y = self.get_web_mercator_y_min() + y * tile_width
        tile_max_y = tile_min_y + tile_width
        gx, gy = np.meshgrid(
            np.linspace(tile_min_x, tile_max_x, oversize),
            np.linspace(tile_min_y, tile_max_y, oversize)
        )
        return np.column_stack((gx.ravel(), gy.ravel()))
    def get_global_pts_boundaries(self, oversize: int = 256) -> np.ndarray:
        """
        Returns a global grid of points covering the entire Web Mercator domain.
        """
        gx, gy = np.meshgrid(
            np.linspace(self.get_web_mercator_x_min(), self.get_web_mercator_x_max(), self.apply_oversize(oversize)),
            np.linspace(self.get_web_mercator_y_min(), self.get_web_mercator_y_max(), self.apply_oversize(oversize))
        )
        return np.column_stack((gx.ravel(), gy.ravel()))
