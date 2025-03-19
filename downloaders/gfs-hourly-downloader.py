#!/usr/bin/env python3
"""
Example NOAA GFS Downloader Script (Flattened Subdirectories)
------------------------------------------------------------
Key Changes:
- We pull a full 00-hour run (f000..f384).
- For 06, 12, 18 runs, we only pull f000..f023.
- Cleanup of older days occurs ONLY if the current day's 00-hour run
  is complete for all file patterns (i.e., we have up to f384).
- Older days now keep only the 00 folder, with forecast hours pruned to f000..f023.
- We remove partial-coverage pruning logic for the current day;
  the 06/12/18 folders are always just 24 hours.
"""
import argparse
import boto3
import os
import re
import shutil
from datetime import datetime, timedelta, UTC

# ---------------------------------------------------------------------------
# CONSTANTS / CONFIG
# ---------------------------------------------------------------------------
BUCKET_NAME = "noaa-gfs-bdp-pds"
S3_BASE_PATH = "gfs"
RESOLUTION = "0p25"

FILE_NAME_PATTERNS = [
    r"gfswave\.t\d{2}z\.global",
    r"gfs\.t\d{2}z\.pgrb2",
    # add more patterns as needed
]

#LOCAL_BASE_PATH = "/Volumes/ModelBackup/HyphenForecaster/gfs_slim"
LOCAL_BASE_PATH = os.getenv("GRIB_FILES_PATH", "/Users/guernica0131/Sites/foreshadow-api/grib")
MAX_DAYS = 5

# These will be used *per hour* depending on whether it's 00 or not:
#  - 00 => 0..384
#  - 06,12,18 => 0..23
FULL_RANGE = list(range(0, 385))   # up to f384
SHORT_RANGE = list(range(0, 24))   # up to f023
STEP_RANGE = list(range(123, 387, 3))   # up to f023
# Boto3 S3 client
s3 = boto3.client("s3")

# ---------------------------------------------------------------------------
# HELPER FUNCTIONS
# ---------------------------------------------------------------------------
def listFileHour(value: int) -> str:
    """Convert integer hour into zero-padded string: 6 -> '006'."""
    return f"{value:03d}"

def list_s3_files(bucket: str, prefix: str):
    """
    Recursively list all S3 keys under a prefix.
    For example, prefix='gfs.20250101/00/' might yield keys in wave/, atmos/, etc.
    """
    files = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        if "Contents" in page:
            files.extend([obj["Key"] for obj in page["Contents"]])
    return files

def download_s3_file(bucket: str, key: str, destination: str):
    """
    Download a single file from S3 to 'destination'.
    """
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    s3.download_file(bucket, key, destination)

def find_local_fvalues(folder_path: str):
    """
    Return a set of forecast-hour integers for files
    found in 'folder_path' that match .fXXX.
    """
    fvalues = set()
    if not os.path.isdir(folder_path):
        return fvalues

    for fname in os.listdir(folder_path):
        full_path = os.path.join(folder_path, fname)
        if not os.path.isfile(full_path):
            continue
        match = re.search(r"\.f(\d{3})", fname)
        if match:
            fhr = int(match.group(1))
            fvalues.add(fhr)
    return fvalues


# ---------------------------------------------------------------------------
# DOWNLOAD LOGIC
# ---------------------------------------------------------------------------
def download_sparse_files(
    local_dir: str,
    prefix: str,
    date_str: str,
    hour_str: str,
    required_hours: list[int],
    downloaded_map: dict
) -> bool:
    """
    1) Recursively list all S3 keys under 'prefix'.
    2) For each object, check if it matches FILE_NAME_PATTERNS + forecast hour.
    3) Place all matching files in local_dir = <date>/<hour> (no subfolders).
    4) Update coverage in downloaded_map, skip existing files.
    5) Return True if at least one new file was downloaded.
    """
    print(f"Checking S3 path: {prefix}")
    downloaded_any = False

    try:
        s3_files = list_s3_files(BUCKET_NAME, prefix)
        if not s3_files:
            print(f"No files found under {prefix}")
            return False

        existing_fvals = find_local_fvalues(local_dir)
        newly_fetched_fvals = set()

        matched_keys = []
        for s3_key in s3_files:
            # Check each forecast hour that we actually care about
            for hr_val in required_hours:
                hr_str = listFileHour(hr_val)
                for pattern in FILE_NAME_PATTERNS:
                    # e.g. gfs.tXXz.pgrb2.0p25.fHHH
                    regex = rf"{pattern}\.{RESOLUTION}\.f{hr_str}"
                    if re.search(regex, s3_key):
                        matched_keys.append((s3_key, hr_val))
                        break
                else:
                    continue
                break  # matched at least one pattern for this hour, no need to keep scanning
        matched_keys = sorted(set(matched_keys), key=lambda x: x[0])
        for obj_key, hr_val in matched_keys:
            filename_only = os.path.basename(obj_key)
            local_path = os.path.join(local_dir, filename_only)

            # If we already have this forecast hour or file exists, skip
            if hr_val in existing_fvals or os.path.exists(local_path):
                downloaded_any = True
                continue

            print(f"Downloading {obj_key} -> {local_path}")
            try:
                download_s3_file(BUCKET_NAME, obj_key, local_path)
                newly_fetched_fvals.add(hr_val)
                downloaded_any = True
            except Exception as e:
                print(f"Failed to download {obj_key}: {e}")

        # Update coverage in downloaded_map
        if date_str not in downloaded_map:
            downloaded_map[date_str] = {}
        if hour_str not in downloaded_map[date_str]:
            downloaded_map[date_str][hour_str] = set()

        downloaded_map[date_str][hour_str].update(existing_fvals)
        downloaded_map[date_str][hour_str].update(newly_fetched_fvals)

    except Exception as e:
        print(f"Error accessing S3 for {prefix}: {e}")

    return downloaded_any

def fetch_sparse_data(downloaded_map: dict, today_str: str) -> bool:
    """
    - For hour=00, fetch the full range (f000..f384).
    - For hour in [06, 12, 18], fetch only f000..f023.
    - Return True if any file was downloaded at all.
    """
    # now = datetime.now(UTC)
    # today_str = now.strftime("%Y%m%d")
    hours = ["00", "06", "12", "18"]
    new_data_found = False

    for hour in hours:
        if hour == "00":
            required_hours = FULL_RANGE
        else:
            required_hours = SHORT_RANGE

        prefix = f"{S3_BASE_PATH}.{today_str}/{hour}/"
        local_dir = os.path.join(LOCAL_BASE_PATH, today_str, hour)

        if download_sparse_files(local_dir, prefix, today_str, hour, required_hours, downloaded_map):
            new_data_found = True

    return new_data_found

# ---------------------------------------------------------------------------
# COVERAGE CHECK
# ---------------------------------------------------------------------------
def is_00_coverage_complete(downloaded_map: dict, date_str: str) -> bool:
    """
    Returns True if for the given date_str we have *all* required hours
    (0..384) for each file pattern in hour=00.
    """
    # If we never downloaded anything for that date or hour=00, fail immediately
    if date_str not in downloaded_map or "00" not in downloaded_map[date_str]:
        return False
    # Get forecast hours actually downloaded
    fvals = downloaded_map[date_str]["00"]
    # Check if all first 24 hours are present
    first_120 = set(SHORT_RANGE)
    if not first_120.issubset(fvals):
        return False

    upto_384 = set(STEP_RANGE)
    # Check if the last required hour (384) is present
    print(upto_384)
    if not upto_384.issubset(fvals):
        return False

    return True
    # Check if we have *all* 0..384
    # needed = set(FULL_RANGE)
    # print(f"Checking coverage for {date_str} hour=00: {sorted(fvals)} vs. {sorted(needed)}")
    # return needed.issubset(fvals)

# ---------------------------------------------------------------------------
# CLEANUP LOGIC
# ---------------------------------------------------------------------------
def prune_files_to_24h(hour_folder: str):
    """
    In <date>/<hour> folder, remove forecast files beyond f023.
    E.g. gfswave.t00z.global.0p25.f027.grib2 => remove
    """
    if not os.path.isdir(hour_folder):
        return

    for fname in os.listdir(hour_folder):
        full_path = os.path.join(hour_folder, fname)
        if not os.path.isfile(full_path):
            continue

        match = re.search(r"\.f(\d{3})", fname)
        if match:
            fhr = int(match.group(1))
            if fhr > 23:
                print(f"Removing {full_path} because forecast hour f{fhr:03d} > 023")
                os.remove(full_path)

def cleanup_old_data(latest_date_str: str):
    """
    1) Delete date folders older than MAX_DAYS.
    2) For days < latest_date_str, keep only '00' with f000–f023 (prune above f023).
    3) Remove other hour folders entirely for older days.
    (No partial-coverage logic for the current day is needed now.)
    """

    latest_date_dt = datetime.strptime(latest_date_str, "%Y%m%d")
    cutoff_date = latest_date_dt - timedelta(days=MAX_DAYS)

    if not os.path.exists(LOCAL_BASE_PATH):
        return

    for folder_name in os.listdir(LOCAL_BASE_PATH):
        date_path = os.path.join(LOCAL_BASE_PATH, folder_name)
        if not os.path.isdir(date_path):
            continue

        try:
            folder_date = datetime.strptime(folder_name, "%Y%m%d")
        except ValueError:
            continue

        # 1) Remove older than cutoff
        if folder_date < cutoff_date:
            print(f"Deleting old folder: {date_path}")
            shutil.rmtree(date_path, ignore_errors=True)
            continue

        # 2) If < latest_date, keep only hour=00 with f000–f023
        if folder_date < latest_date_dt:
            print(f"Pruning older day: {date_path} => keep only 00 with f000–f023")
            for hour_sub in os.listdir(date_path):
                hour_path = os.path.join(date_path, hour_sub)
                if not os.path.isdir(hour_path):
                    continue
                if hour_sub != "00":
                    # remove hour folders other than '00'
                    print(f"Removing hour folder: {hour_path}")
                    shutil.rmtree(hour_path, ignore_errors=True)
                else:
                    # keep folder=00 but prune beyond f023
                    prune_files_to_24h(hour_path)
        else:
            # Current day: do nothing. We no longer do partial coverage cleanup here.
            print(f"Skipping cleanup for current day: {date_path}")

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("Starting NOAA GFS data download (flattened) with revised logic...")
    # Dictionary to track coverage for newly downloaded runs
    parser = argparse.ArgumentParser(description='Download NOAA GFS data.')

    # Add optional arguments
    parser.add_argument(
        '--date-offset',
        type=int,
        default=0,
        help='Number of days offset from today (default: 0)'
    )
    parser.add_argument(
        '--no-cleanup',
        action='store_true',
        help='Skip cleanup of old data'
    )

    # Parse the arguments
    args = parser.parse_args()

    downloaded_files_map = {}
    # 1) Fetch today's data
    target_date = datetime.now(UTC) + timedelta(days=args.date_offset)  # Default: Today
    today_str = target_date.strftime("%Y%m%d")
    pulled_new_data = fetch_sparse_data(downloaded_files_map, today_str)
    # 2) Only clean up if:
    #    - we downloaded something new today
    #    - and the 00 coverage is complete up to f384
    print(args.no_cleanup, pulled_new_data)
    if not args.no_cleanup:
        if pulled_new_data and is_00_coverage_complete(downloaded_files_map, today_str):
            cleanup_old_data(today_str)
        else:
            print("No complete 00 run found or no new data. Skipping cleanup.")

    print("Finished download and pruning process.")
