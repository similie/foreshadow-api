# interpolator.py
import numpy as np
from scipy.spatial import Delaunay
from scipy.interpolate import LinearNDInterpolator
from pyproj import Transformer, exceptions as proj_exceptions
import logging
# Configure logging for debugging purposes
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

class Interpolator:
    """
    A wrapper class to build and manage the LinearNDInterpolator with multithreading optimizations.
    """
    def __init__(self, transformer: Transformer | None = None):
        """
        Initializes the Interpolator by setting up the transformer.
        """
        try:
            self.transformer = transformer if transformer is not None else Transformer.from_crs("epsg:4326", "epsg:3857", always_xy=True)
            logger.info("Interpolator: Transformer initialized successfully.")
        except proj_exceptions.ProjError as e:
            logger.error(f"Interpolator: Failed to initialize transformer: {e}")
            raise ValueError(f"Interpolator: Transformer initialization failed: {e}")

    def build_interpolator(self, data, lats, lons, lat_flip=False,  decimation: int  = 1):
        """
        Builds a CPU-based LinearNDInterpolator using Delaunay triangulation.
        """
        if lat_flip:
            lats = -lats
        lats = np.clip(lats, -85.05112878, 85.05112878)
        lons = (lons + 180) % 360 - 180
        flt_lat = lats.ravel()[::decimation]
        flt_lon = lons.ravel()[::decimation]
        flt_dat = data.ravel()[::decimation]
        # Duplicate points near the antimeridian (±180°)
        dl_thresh = 1.0
        mask_dl = (np.abs(flt_lon) >= (180 - dl_thresh))
        dup_lon = flt_lon[mask_dl] + 360 * np.where(flt_lon[mask_dl] < 0, 1, -1)
        dup_lat = flt_lat[mask_dl]
        dup_dat = flt_dat[mask_dl]
        flt_lon = np.concatenate([flt_lon, dup_lon])
        flt_lat = np.concatenate([flt_lat, dup_lat])
        flt_dat = np.concatenate([flt_dat, dup_dat])

        fx, fy = self.transformer.transform(flt_lon, flt_lat)

        # Validate data
        valid_mask = ~np.isnan(fx) & ~np.isnan(fy) & ~np.isnan(flt_dat)
        valid_mask &= ~np.isinf(fx) & ~np.isinf(fy) & ~np.isinf(flt_dat)
        fx = fx[valid_mask]
        fy = fy[valid_mask]
        flt_dat = flt_dat[valid_mask]

        tri = Delaunay(np.column_stack((fx, fy)))
        ip = LinearNDInterpolator(tri, flt_dat, fill_value=np.nan)
        return ip
