# terrain/dem_sources/copernicus_s3.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

import boto3
from botocore import UNSIGNED
from botocore.client import Config

BBox = Tuple[float, float, float, float]  # (min_lon, min_lat, max_lon, max_lat)


@dataclass(frozen=True)
class CopernicusDEMConfig:
    # Prefer 30m, fall back to 90m if you want global completeness.
    dataset: str = "30m"  # "30m" or "90m"
    out_dir: Path = Path("data/elevation_copernicus")

    # Default: Timor-Leste + buffer
    # Rough TL bbox ≈ lon 123.7–127.3, lat -10.6–-8.1; add buffer.
    default_bbox: BBox = (123.0, -11.4, 128.2, -7.4)


class CopernicusDEMS3Downloader:
    """
    Downloads Copernicus DEM tiles from AWS public S3 buckets.

    - dataset="30m": bucket copernicus-dem-30m (resolution code 10)
    - dataset="90m": bucket copernicus-dem-90m (resolution code 30)
    """

    def __init__(self, cfg: CopernicusDEMConfig = CopernicusDEMConfig()):
        self.cfg = cfg
        self.bucket = (
            "copernicus-dem-30m" if cfg.dataset == "30m" else "copernicus-dem-90m"
        )
        self.res_code = "10" if cfg.dataset == "30m" else "30"

        # unsigned, public access
        self.s3 = boto3.client("s3", config=Config(signature_version=UNSIGNED))

    # ----------------------------
    # Public API
    # ----------------------------

    def _already_downloaded(self, path: Path, min_bytes: int = 10_000) -> bool:
        # min_bytes prevents treating a partial/failed download as valid
        return path.exists() and path.is_file() and path.stat().st_size >= min_bytes

    def download(
        self, bbox: Optional[BBox] = None, *, include_all_if_null: bool = False
    ) -> List[Path]:
        """
        Download tiles intersecting bbox.

        If bbox is None:
          - if include_all_if_null=True, attempts to download *all tiles* (very large).
          - else uses cfg.default_bbox (Timor+buffer).
        """
        if bbox is None:
            if include_all_if_null:
                return self._download_all_tiles()
            bbox = self.cfg.default_bbox

        folders = list(self._folders_for_bbox(bbox))
        downloaded: List[Path] = []
        count = 0
        for folder in folders:
            keys = self._list_keys(prefix=f"{folder}/")
            tif_keys = [k for k in keys if k.lower().endswith(".tif")]

            if not tif_keys:
                # tile may be missing (ocean, or 30m-public coverage gap)
                continue

            for key in tif_keys:
                count += 1
                local_path = self.cfg.out_dir / key
                if self._already_downloaded(local_path):
                    downloaded.append(local_path)
                    print(f"File exists. Skipping {count} {local_path} ")
                    continue

                tmp_path = local_path.with_suffix(local_path.suffix + ".part")
                self._download_key(key, tmp_path)
                tmp_path.replace(local_path)
                downloaded.append(local_path)
                print(f"File downloaded {count} {local_path}")

        return downloaded

    # ----------------------------
    # Internals
    # ----------------------------
    def _download_all_tiles(self) -> List[Path]:
        """
        Downloads everything by walking the bucket.
        Warning: can be huge.
        """
        downloaded: List[Path] = []
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.lower().endswith(".tif"):
                    local_path = self.cfg.out_dir / key
                    tmp_path = local_path.with_suffix(local_path.suffix + ".part")
                    self._download_key(key, tmp_path)
                    tmp_path.replace(local_path)
                    downloaded.append(local_path)
        return downloaded

    def _download_key(self, key: str, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        self.s3.download_file(self.bucket, key, str(dst))

    def _list_keys(self, prefix: str) -> List[str]:
        resp = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=prefix)
        return [c["Key"] for c in resp.get("Contents", [])]

    def _folders_for_bbox(self, bbox: BBox) -> Iterable[str]:
        min_lon, min_lat, max_lon, max_lat = bbox

        # 1x1 degree tiles => integer degree grid
        lon0 = int(min_lon // 1)
        lon1 = int(max_lon // 1)
        lat0 = int(min_lat // 1)
        lat1 = int(max_lat // 1)

        for lat in range(lat0, lat1 + 1):
            for lon in range(lon0, lon1 + 1):
                yield self._tile_folder(lat, lon)

    def _tile_folder(self, lat_deg: int, lon_deg: int) -> str:
        # Northing like N08_00 or S10_00 (always _00)
        ns = "N" if lat_deg >= 0 else "S"
        ew = "E" if lon_deg >= 0 else "W"

        lat_abs = abs(lat_deg)
        lon_abs = abs(lon_deg)

        northing = f"{ns}{lat_abs:02d}_00"
        easting = f"{ew}{lon_abs:03d}_00"

        return f"Copernicus_DSM_COG_{self.res_code}_{northing}_{easting}_DEM"
