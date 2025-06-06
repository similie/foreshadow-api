import math
from typing import Dict, List, Any, Optional
class WeatherUtils:

    def wind_direction(self, u: float, v: float) -> float:
        """
        Calculate meteorological wind direction from u (zonal) and v (meridional) components.
        Returns direction in degrees from which the wind is blowing (0° = north).
        """
        # atan2 returns radians; convert to degrees and rotate by 180°
        dir_deg = math.degrees(math.atan2(u, v)) + 180.0
        # ensure within [0,360)
        return dir_deg % 360.0

    def apply_extras_details(self, records: List[Dict[str, Any]], lat: float,
    lon: float) -> None:
            """
            Given a list of parameter timeseries records, extract the
            10-metre-u and 10-metre-v wind components, compute wind direction,
            and append a new record with param_key "10-metre-wind-direction".
            Modifies records in place.
            """
            # Locate u- and v- component series
            try:
                u_series_details =  next(r for r in records if r["metadata"]["key"] == "10-metre-u-wind-component")
                v_series_details = next(r for r in records if r["metadata"]["key"] == "10-metre-v-wind-component")
                meta = v_series_details["metadata"]
                u_series = u_series_details["values"]
                v_series = v_series_details["values"]
            except StopIteration:
                # If either component is missing, nothing to do
                return
            unit = "degrees"
            metadata = {
                "parameterName": "10 Meter Wind Direction",
                "parameterUnits": unit,
                "shortName": "wd",
                "typeOfLevel": meta["typeOfLevel"],
                "level": meta["level"],
                "min": 0,
                "max": 360,
                "name": "10 metre derrived wind direction",
                "stepType": meta["stepType"],
                "key": "10-metre-wind-direction"
            }
            # Build quick lookup by datetime


            # Compute direction for each timestamp present in both
            try:
                u_map = {item["datetime"]: item["value"] for item in u_series}
                v_map = {item["datetime"]: item["value"] for item in v_series}

                direction_values = []
                for dt in sorted(set(u_map).intersection(v_map)):
                    u = u_map[dt]
                    v = v_map[dt]
                    # use your existing utility to get meteorological wind direction
                    wd = self.wind_direction(u, v)
                    direction_values.append({
                        "datetime": dt,
                        "value": round(wd, 3)
                    })

                # Append the new timeseries
                records.append({
                    "values": direction_values,
                    "metadata": metadata
                })
            except Exception as e:
                print("ERROR: Weather.apply_extras_details", e)
