from typing import Any, List, Dict, Union, Tuple
import numpy as np
from matplotlib.colors import LinearSegmentedColormap, Normalize
import matplotlib.pyplot as plt

class MapColors:

    # def __init__(self):
    #     self.precip_cmap = self._create_precip_cmap()

    # def _create_precip_cmap(self) -> LinearSegmentedColormap:
    #     """
    #     Create a custom precipitation colormap.
    #     """
    #     colors = [
    #         (0.0, 0.0, 0.0, 0.0),
    #         (0.0, 0.0, 1.0, 0.6),
    #         (0.0, 0.2, 1.0, 1.0),
    #         (0.0, 0.5, 1.0, 1.0),
    #         (0.0, 1.0, 0.5, 1.0),
    #         (1.0, 1.0, 0.0, 1.0),
    #         (1.0, 0.3, 0.0, 1.0),
    #         (1.0, 0.0, 1.0, 1.0),
    #     ]
    #     positions = [0.0, 0.01, 0.15, 0.25, 0.35, 0.55, 0.7, 1.0]
    #     return LinearSegmentedColormap.from_list("expanded_precip", list(zip(positions, colors)))

    def assign_color_map(self, model: str, param_name: str) -> Union[str, LinearSegmentedColormap]:
        """
        Assign a suitable colormap based on the parameter name.
        """
        lower_name = param_name.lower()

        if any(k in lower_name for k in ["precipitation", "rain", "snow", "graupel", "mixing", "reflectivity"]):
            return "rainbow" # self.precip_cmap
        if "temperature" in lower_name:
            return "jet"
        if "direction" in lower_name:
            return "hsv"
        if "wind" in lower_name:
            return "viridis"
        if "humidity" in lower_name:
            return "YlGnBu"
        if any(k in lower_name for k in ["pressure", "height", "vorticity"]):
            return "plasma"
        if "cloud" in lower_name:
            return "twilight"

        return "viridis"

    def _assign_color_map_with_colors(
        self,
        model: str,
        param_name: str,
        num_colors: int = 256
    ) -> Tuple[Union[str, LinearSegmentedColormap], np.ndarray]:
        """
        Assign a suitable colormap based on the parameter name and return its color array (8-bit RGBA).

        Returns:
            (cmap_name_or_obj, color_array_8bit):
              - cmap_name_or_obj:  str or LinearSegmentedColormap
              - color_array_8bit:  np.ndarray with shape (num_colors, 4), dtype=uint8
        """
        # 1) Determine the base colormap (name or object)
        cmap_name_or_obj = self.assign_color_map(model, param_name)
        cmap_obj = (
            cmap_name_or_obj if isinstance(cmap_name_or_obj, LinearSegmentedColormap)
            else plt.get_cmap(cmap_name_or_obj)
        )

        # 2) Generate floating-point RGBA in [0..1]
        float_array = cmap_obj(np.linspace(0, 1, num_colors))  # shape => (num_colors,4)

        # 3) Convert floats => 8-bit RGBA in [0..255]
        color_array_8bit = (float_array * 255).astype(np.uint8)

        # 4) If param_name includes "cloud", do alpha adjustments
        if self.zero_clip(param_name):
            # Example (A): set alpha=0 for the first color => 0% coverage fully transparent
            # color_array_8bit[0, 3] = 0

            # Example (B): fade alpha from 0..10% coverage.
            #   i.e., first ~10% of color_array => alpha=0..255 linearly
            fade_stop = int(num_colors * 0.1)  # ~10%
            for i in range(fade_stop):
                alpha_val = int(255 * (i / (fade_stop - 1 if fade_stop>1 else 1)))
                color_array_8bit[i, 3] = alpha_val

        return cmap_name_or_obj, color_array_8bit

    def zero_clip(self, param_name: str) -> bool:
        """
        Returns True if the value is 0, False otherwise.
        """
        lower_name = param_name.lower()
        return any(k in lower_name for k in ["cloud", "precipitation", "rain", "snow", "graupel", "mixing", "reflectivity"])

    def colorize_grid_ALTERED(
        self,
        model: str,
        param_name: str,
        data_2d: np.ndarray,
        gmin: float,
        gmax: float,
        missing_mask: np.ndarray | None = None
    ) -> np.ndarray:
        """
        Convert a 2D data array into an RGBA uint8 image of shape (H,W,4).
        Minimizes halo by forcing alpha=0 below a threshold, alpha=255 above.
        Also sets interpolation=nearest at the end if you are using matplotlib.
        """
        if missing_mask is None:
            missing_mask = np.isnan(data_2d)

        tmp_data = data_2d.copy()
        tmp_data[missing_mask] = gmin

        norm = Normalize(vmin=gmin, vmax=gmax)

        # 1) We pick a colormap and ensure its first portion is the same color.
        #    We'll build a 'constant' section from 0..0.02 => the same color
        #    so no hue shift near zero.
        base_cmap_name = self.assign_color_map(model, param_name)
        base_cmap = (base_cmap_name if isinstance(base_cmap_name, LinearSegmentedColormap)
                    else plt.get_cmap(base_cmap_name))

        # Example: we morph the colormap so that 0..0.02 is constant:
        # We build a new colormap array, adjusting the first 2% to be the color at 2%.
        new_n = 256
        base_vals = base_cmap(np.linspace(0, 1, new_n))  # shape=(256,4) float
        # copy color at i=5 => ~2% into the colormap
        boundary_idx = int(new_n * 0.02)
        constant_color = base_vals[boundary_idx, :].copy()
        for i in range(boundary_idx):
            base_vals[i, :] = constant_color

        # Now make a new colormap from these modified values
        new_cmap = LinearSegmentedColormap.from_list("modified_cmap", base_vals)

        # 2) Map our data => [0..1]
        normed_data = norm(tmp_data)

        # 3) Convert to RGBA in [0..1] by calling the new colormap
        rgba_float = new_cmap(normed_data)

        # 4) Convert to 8-bit => shape (H,W,4) in [0..255]
        rgba_8u = (rgba_float * 255).astype(np.uint8)

        # 5) Hard alpha cutoff: normed_data < threshold => alpha=0, else alpha=255
        if self.zero_clip(param_name):
            threshold = 0.2  # same as your constant region
            is_below = (normed_data < threshold)
            rgba_8u[is_below, 3] = 0
            rgba_8u[~is_below, 3] = 255

        # 6) Missing data => alpha=0
        rgba_8u[missing_mask, 3] = 0

        return rgba_8u

    def colorize_grid_ORIG(
        self,
        model: str,
        param_name: str,
        data_2d: np.ndarray,
        gmin: float,
        gmax: float,
        missing_mask: np.ndarray | None = None
    ) -> np.ndarray:
        """
        Convert a 2D data array into an RGBA uint8 image of shape (H,W,4).
        """

        # 1) If no explicit missing_mask, define it from nan
        if missing_mask is None:
            missing_mask = np.isnan(data_2d)

        # 2) Temporary fill missing data with gmin for color-scaling
        tmp_data = data_2d.copy()
        tmp_data[missing_mask] = gmin

        # 3) Build normalizer
        norm = Normalize(vmin=gmin, vmax=gmax)

        # 4) Pick the colormap
        cmap_name_or_obj = self.assign_color_map(model, param_name)
        if isinstance(cmap_name_or_obj, LinearSegmentedColormap):
            cmap_obj = cmap_name_or_obj
        else:
            cmap_obj = plt.get_cmap(cmap_name_or_obj)

        # 5) Apply normalization => [0..1]
        normed_data = norm(tmp_data)  # shape => (H,W) in [0..1]

        # 6) Convert to RGBA => shape (H,W,4) in float [0..1]
        rgba_float = cmap_obj(normed_data)

        # 7) Convert to 8-bit => shape (H,W,4) in [0..255]
        rgba_8u = (rgba_float * 255).astype(np.uint8)

        # 8) If we need to clip near zero => alpha=0
        #    Because partial alpha near 0 can cause a "halo" or "border" effect,
        #    we do a binary threshold. For example, any data < fade_threshold => alpha=0
        if self.zero_clip(param_name):
            fade_threshold = 0.02  # in normalized space (0..1). Adjust as needed
            fade_region = (normed_data < fade_threshold)
            rgba_8u[fade_region, 3] = 0  # fully transparent for near-zero values
            # optionally, for data above fade_threshold => alpha=255 (already is by default)
            # but if you want to ensure no partial alpha, do:
            rgba_8u[~fade_region, 3] = 255

        # 9) Missing data => alpha=0
        rgba_8u[missing_mask, 3] = 0

        return rgba_8u

    def colorize_grid_third(
        self,
        model: str,
        param_name: str,
        data_2d: np.ndarray,
        gmin: float,
        gmax: float,
        missing_mask: np.ndarray | None = None
    ) -> np.ndarray:
        """
        Returns (H,W,4) RGBA in uint8, with alpha=0 for near-zero if 'zeroClip' is True,
        alpha=255 otherwise, to avoid halo borders.
        """
        from matplotlib.colors import LinearSegmentedColormap, Normalize
        import matplotlib.pyplot as plt
        import numpy as np

        if missing_mask is None:
            missing_mask = np.isnan(data_2d)

        tmp_data = data_2d.copy()
        tmp_data[missing_mask] = gmin

        norm = Normalize(vmin=gmin, vmax=gmax)

        cmap_name_or_obj = self.assign_color_map(model, param_name)
        if isinstance(cmap_name_or_obj, LinearSegmentedColormap):
            cmap_obj = cmap_name_or_obj
        else:
            cmap_obj = plt.get_cmap(cmap_name_or_obj)

        # => Convert data => normalized => then colormap => float RGBA => 8-bit
        normed_data = norm(tmp_data)
        rgba_float = cmap_obj(normed_data)
        rgba_8u = (rgba_float * 255).astype(np.uint8)

        if self.zero_clip(param_name):
            # Example binary alpha cutoff at normed_data < 0.02 => alpha=0, else=255
            threshold = 0.02
            is_below = (normed_data < threshold)
            rgba_8u[is_below, 3] = 0
            rgba_8u[~is_below, 3] = 255

        # Missing => alpha=0
        rgba_8u[missing_mask, 3] = 0

        return rgba_8u


    def colorize_grid(
        self,
        model: str,
        param_name: str,
        data_2d: np.ndarray,
        gmin: float,
        gmax: float,
        missing_mask: np.ndarray | None = None
    ) -> np.ndarray:
        """
        Returns an (H,W,4) RGBA uint8 array.
        We'll do a binary alpha cutoff for near-zero data => alpha=0.
        """
        if missing_mask is None:
            missing_mask = np.isnan(data_2d)

        tmp_data = data_2d.copy()
        tmp_data[missing_mask] = gmin

        norm = Normalize(vmin=gmin, vmax=gmax)

        cmap_name_or_obj = self.assign_color_map(model, param_name)
        if isinstance(cmap_name_or_obj, LinearSegmentedColormap):
            cmap_obj = cmap_name_or_obj
        else:
            cmap_obj = plt.get_cmap(cmap_name_or_obj)

        # 1) Convert data => normalized => colormap => float RGBA => then 8-bit
        normed_data = norm(tmp_data)            # shape (H,W), in [0..1]
        rgba_float = cmap_obj(normed_data)      # shape (H,W,4), float in [0..1]
        rgba_8u = (rgba_float * 255).astype(np.uint8)

        # 2) Hard alpha cutoff near zero => no partial alpha
        #    For example, below 0.02 => alpha=0, else alpha=255
        if self.zero_clip(param_name):
            threshold = 0.02
            is_below = (normed_data < threshold)
            rgba_8u[is_below, 3] = 0
            rgba_8u[~is_below, 3] = 255

        # 3) Missing data => alpha=0
        rgba_8u[missing_mask, 3] = 0

        return rgba_8u

    def select_first_last_every_nth(self, arr: List[Any], n: int) -> List[Any]:
        """
        Returns the first, last, and every nth element from arr.
        """
        if not arr:
            return []
        if len(arr) <= 2:
            return arr
        return [arr[0]] + arr[1:-1:n] + [arr[-1]]

    def _rgba_to_hex(self, rgba: List[int], keep_alpha: bool = False) -> str:
        """
        Convert [R,G,B,A] in 8-bit ints to #RRGGBB or #RRGGBBAA
        """
        r, g, b, a = rgba
        if keep_alpha:
            return f"#{r:02X}{g:02X}{b:02X}{a:02X}"
        else:
            return f"#{r:02X}{g:02X}{b:02X}"

    def get_color_profile(
        self,
        model: str,
        param_name: str,
        num_colors: int = 256,
        condensed: int = 1,
        keep_alpha: bool = False
    ) -> Dict[str, Any]:
        """
        Get a color profile for param_name in hex.

        Returns:
            {
              "colormap": "twilight",
              "colors": [ "#RRGGBB", "#RRGGBB", ... ] or with alpha => "#RRGGBBAA"
            }
        """
        # 1) Build the full color array
        cmap_name_or_obj, color_array_8bit = self._assign_color_map_with_colors(
            model, param_name, num_colors
        )

        # 2) Convert each row => hex
        color_rows = color_array_8bit.tolist()
        hex_colors = [self._rgba_to_hex(row, keep_alpha=keep_alpha) for row in color_rows]

        # 3) Possibly condense
        condensed_list = self.select_first_last_every_nth(hex_colors, condensed)

        # 4) Label the colormap
        if isinstance(cmap_name_or_obj, LinearSegmentedColormap):
            cmap_label = cmap_name_or_obj.name
        else:
            cmap_label = cmap_name_or_obj

        return {
            "colormap": cmap_label,
            "colors": condensed_list
        }
