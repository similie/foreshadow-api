import math
from datetime import datetime
from typing import Dict, List, Any
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

    def get_wind_direction_values(self, records: List[Dict[str, Any]]):
        u_series_details =  next(r for r in records if r["metadata"]["key"] == "10-metre-u-wind-component")
        v_series_details = next(r for r in records if r["metadata"]["key"] == "10-metre-v-wind-component")
        meta = v_series_details["metadata"]
        return (u_series_details, v_series_details, meta)

    def generate_wind_speed_meta(self, meta: Dict[str, Any]):
        unit = "degrees"
        metadata = {
            "parameterName": "10 Meter Wind Direction",
            "parameterUnits": unit,
            "shortName": "wd",
            "typeOfLevel": meta["typeOfLevel"],
            "level": meta["level"],
            "minimum": 0,
            "maximum": 360,
            "name": "10 metre derrived wind direction",
            "stepType": meta["stepType"],
            "key": "10-metre-wind-direction"
        }
        return metadata

    def apply_wind_direction_to_single(self, records: List[Dict[str, Any]]):
        try:
            u_series_details, v_series_details, meta =  self.get_wind_direction_values(records)
            u_series = u_series_details["value"]
            v_series = v_series_details["value"]
        except StopIteration:
            # If either component is missing, nothing to do
            return

        try:
            metadata = self.generate_wind_speed_meta(meta)
            wd = self.wind_direction(u_series, v_series)
            result = {
                "metadata": metadata,
                "value": wd,
                "datetime": u_series_details["datetime"],
                "unit": metadata["parameterUnits"]
            }
            records.append(result)
        except Exception as e:
            print('Single wind-direction failure', e)


    def apply_wind_direction_to_series(self, records: List[Dict[str, Any]]):
        """
        Given a list of parameter timeseries records, extract the
        10-metre-u and 10-metre-v wind components, compute wind direction,
        and append a new record with param_key "10-metre-wind-direction".
        Modifies records in place.
        """
        # Locate u- and v- component series
        try:
            u_series_details, v_series_details, meta =  self.get_wind_direction_values(records)
            u_series = u_series_details["values"]
            v_series = v_series_details["values"]
        except StopIteration:
            # If either component is missing, nothing to do
            return
        metadata = self.generate_wind_speed_meta(meta)
        # Build quick lookup by datetime
        # Compute direction for each timestamp present in both
        try:
            u_map = {item["datetime"]: (item["value"],item["offset"]) for item in u_series}
            v_map = {item["datetime"]: (item["value"],item["offset"]) for item in v_series}

            direction_values = []
            for dt in sorted(set(u_map).intersection(v_map)):
                u, u_off = u_map[dt]
                v, v_off = v_map[dt]
                # use your existing utility to get meteorological wind direction
                wd = self.wind_direction(u, v)
                date_time = datetime.fromisoformat(dt)
                direction_values.append({
                    "value": round(wd, 3),
                     "datetime": dt,
                     "offset": v_off,
                     "day": date_time.day,
                     "hour": date_time.hour
                })

            # Append the new timeseries
            records.append({
                "values": direction_values,
                "metadata": metadata
            })
        except Exception as e:
            print("ERROR: Weather.apply_extras_details", e)

    def apply_extras_details(self, records: List[Dict[str, Any]], lat: float,
    lon: float) -> None:
        self.apply_wind_direction_to_series(records)
