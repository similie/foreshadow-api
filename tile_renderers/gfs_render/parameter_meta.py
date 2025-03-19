# parameter_meta.py

"""
Manual metadata for each parameter_key.
Data is keyed by parameter_key, which must match the keys in your main code.
"""

PARAMETER_META = {
    "gfs": {
        "pressure-reduced-to-msl": {
            #"data_range": {
            #    "min": 80000,
            #    "max": 110000
            #},
            "description": "Mean sea level pressure commonly used for weather analysis and forecasting.",
            "notes": "Typically ranges from ~800 hPa to ~1100 hPa."
        },
        "cloud-mixing-ratio": {
            #"data_range": {
            #    "min": 0,
            #    "max": 0.02
            #},
            "description": "Mass of cloud water per unit mass of air.",
            "notes": "Ranges typically from 0 to ~0.02 in standard atmospheric conditions."
        },
        "ice-water-mixing-ratio": {
            #"data_range": {
            #    "min": 0,
            #    "max": 0.02
            #},
            "description": "Mass of ice water per unit mass of air.",
            "notes": "Ranges typically from 0 to ~0.02 in cold cloud regions."
        },
        "rain-mixing-ratio": {
            #"data_range": {
            #    "min": 0,
            #    "max": 0.03
            #},
            "description": "Mass of rain water per unit mass of air.",
            "notes": "Ranges up to ~0.03 in heavy precipitation conditions."
        },
        "snow-mixing-ratio": {
            #"data_range": {
            #    "min": 0,
            #    "max": 0.02
            #},
            "description": "Mass of snow crystals per unit mass of air.",
            "notes": "Ranges typically up to ~0.02 in strong winter storms."
        },
        "graupel-snow-pellets": {
            #"data_range": {
            #    "min": 0,
            #    "max": 0.01
            #},
            "description": "Mass of graupel (snow pellets) per unit mass of air.",
            "notes": "Typically near 0 unless convective storms produce graupel."
        },
        "derived-radar-reflectivity": {
            #"data_range": {
            #    "min": -30,
            #    "max": 75
            #},
            "description": "Reflectivity factor derived from microphysical states.",
            "notes": "Ranges from negative values in clear air to 75+ dBZ in extreme storms."
        },
        "maximumcomposite-radar-reflectivity": {
            #"data_range": {
            #    "min": -30,
            #    "max": 75
            #},
            "description": "Maximum or composite reflectivity across multiple altitudes or a vertical column.",
            "notes": "Ranges from negative values in clear air to 75+ dBZ in extreme storms."
        },
        "visibility": {
            #"data_range": {
            #    "min": 0,
            #    "max": 100000
            #},
            "description": "Horizontal visibility at the surface in meters.",
            "notes": "Typically up to 10–20 km in clear air; 100000 m ~ 100 km in some models."
        },
        "u-component-of-wind": {
            #"data_range": {
            #    "min": -100,
            #    "max": 100
            #},
            "description": "Zonal (east-west) wind component in m/s.",
            "notes": "Negative = westward, positive = eastward."
        },
        "v-component-of-wind": {
            #"data_range": {
            #    "min": -100,
            #    "max": 100
            #},
            "description": "Meridional (north-south) wind component in m/s.",
            "notes": "Negative = southward, positive = northward."
        },
        "ventilation-rate": {
            #"data_range": {
            #    "min": 0,
            #    "max": 1000
            #},
            "description": "Measurement of atmospheric ventilation (wind speed × mixing height).",
            "notes": "Units in m**2 s**-1; ranges vary widely."
        },
        "wind-speed-gust": {
            #"data_range": {
            #    "min": 0,
            #    "max": 100
            #},
            "description": "Surface gust wind speed in m/s.",
            "notes": "Can exceed 100 m/s in extreme storms (rare)."
        },
        "geopotential-height": {
            #"data_range": {
            #    "min": 0,
            #    "max": 30000
            #},
            "description": "Height of a given pressure level in geopotential meters (≈ actual meters).",
            "notes": "Values can approach 30,000 gpm at very high altitudes."
        },
        "temperature": {
            #"data_range": {
            #    "min": 150,
            #    "max": 350
            #},
            "description": "Atmospheric temperature in Kelvin at isobaric levels.",
            "notes": "Ranges from very cold (~150K) to very hot (~350K)."
        },
        "relative-humidity": {
            #"data_range": {
            #    "min": 0,
            #    "max": 100
            #},
            "description": "Ratio of water vapor partial pressure to saturation vapor pressure, in percent.",
            "notes": "0% = fully dry, 100% = fully saturated."
        },
        "specific-humidity": {
            #"data_range": {
            #    "min": 0,
            #    "max": 0.04
            #},
            "description": "Mass of water vapor per total mass of air at isobaric levels.",
            "notes": "Ranges typically up to ~0.04 in tropical climates."
        },
        "vertical-velocity": {
            #"data_range": {
            #    "min": -5,
            #    "max": 5
            #},
            "description": "Rate of pressure change with time, indicative of updrafts/downdrafts (Pa/s).",
            "notes": "Negative = rising air, positive = sinking air."
        },
        "geometric-vertical-velocity": {
            #"data_range": {
            #    "min": -10,
            #    "max": 10
            #},
            "description": "Vertical velocity in geometric height coordinates (m/s).",
            "notes": "Negative = upward motion, positive = downward motion."
        },
        "absolute-vorticity": {
            #"data_range": {
            #    "min": -0.001,
            #    "max": 0.001
            #},
            "description": "Sum of Earth's and relative vorticity at isobaric levels.",
            "notes": "Positive = cyclonic (NH), negative = anticyclonic."
        },
        "ozone-mixing-ratio": {
            #"data_range": {
            #    "min": 0,
            #    "max": 0.0001
            #},
            "description": "Mass ratio of ozone to air at a given pressure level.",
            "notes": "Typically small, especially in troposphere; larger in stratosphere."
        },
        "total-cloud-cover": {
            #"data_range": {
            #    "min": 0,
            #    "max": 100
            #},
            "description": "Fraction of sky covered by cloud (0%–100%).",
            "notes": "0% = clear, 100% = fully overcast."
        },
        "haines-index": {
            #"data_range": {
            #    "min": 2,
            #    "max": 6
            #},
            "description": "Index indicating potential for wildfire growth. Higher = more unstable/dry.",
            "notes": "Ranges 2–6, with 6 indicating highest risk."
        },
        "mslp-eta-model-reduction": {
            #"data_range": {
            #    "min": 80000,
            #    "max": 110000
            #},
            "description": "Mean sea level pressure computed via Eta model reduction method.",
            "notes": "Similar to standard MSLP (about 800–1100 hPa)."
        },
        "surface-pressure": {
            #"data_range": {
            #    "min": 50000,
            #    "max": 110000
            #},
            "description": "Atmospheric pressure at the surface.",
            "notes": "Lower near high elevations, higher at sea level."
        },
        "orography": {
            #"data_range": {
            #    "min": -500,
            #    "max": 9000
            #},
            "description": "Elevation of Earth’s surface in meters.",
            "notes": "Negative below sea level; highest peaks > 8000 m."
        },
        "soil-temperature": {
            #"data_range": {
            #    "min": 200,
            #    "max": 320
            #},
            "description": "Soil temperature at a specific depth below ground (K).",
            "notes": "Varies with soil type, moisture, and climate."
        },
        "volumetric-soil-moisture-content": {
            #"data_range": {
            #    "min": 0,
            #    "max": 1
            #},
            "description": "Fraction of soil volume occupied by water.",
            "notes": "0 = completely dry, 1 = fully saturated."
        },
        "liquid-volumetric-soil-moisture-non-frozen": {
            #"data_range": {
            #    "min": 0,
            #    "max": 1
            #},
            "description": "Fraction of soil water that is liquid (unfrozen).",
            "notes": "Excludes frozen water in the soil."
        },
        "plant-canopy-surface-water": {
            #"data_range": {
            #    "min": 0,
            #    "max": 2
            #},
            "description": "Water stored on vegetation surfaces per unit area (kg/m^2).",
            "notes": "Ranges from 0 to a few kg/m^2 on wet canopies."
        },
        "water-equivalent-of-accumulated-snow-depth-deprecated": {
            #"data_range": {
            #    "min": 0,
            #    "max": 1000
            #},
            "description": "Snow water equivalent if melted. (deprecated).",
            "notes": "Can reach up to 1000 kg/m^2 in extreme snowpacks."
        },
        "snow-depth": {
            #"data_range": {
            #    "min": 0,
            #    "max": 10
            #},
            "description": "Depth of snow on the ground in meters.",
            "notes": "Can exceed 10 m in heavy snowfall regions."
        },
        "sea-ice-thickness": {
            #"data_range": {
            #    "min": 0,
            #    "max": 5
            #},
            "description": "Thickness of sea ice on ocean surfaces in meters.",
            "notes": "Ranges up to several meters in polar regions."
        },
        "2-metre-temperature": {
            #"data_range": {
            #    "min": 180,
            #    "max": 330
            #},
            "description": "Air temperature at 2m above ground in Kelvin.",
            "notes": "Ranges from ~180K to ~330K."
        },
        "2-metre-specific-humidity": {
            #"data_range": {
            #    "min": 0,
            #    "max": 0.04
            #},
            "description": "Mass of water vapor per unit mass of air at 2m height.",
            "notes": "Ranges up to ~0.04 in warm/humid conditions."
        },
        "2-metre-dewpoint-temperature": {
            #"data_range": {
            #    "min": 180,
            #    "max": 330
            #},
            "description": "Dewpoint temperature at 2m above ground (K).",
            "notes": "Cannot exceed actual temperature; ~330K is extremely high dewpoint."
        },
        "2-metre-relative-humidity": {
            #"data_range": {
            #    "min": 0,
            #    "max": 100
            #},
            "description": "Relative humidity at 2m above ground, in percent.",
            "notes": "0% = fully dry, 100% = saturated near surface."
        },
        "apparent-temperature": {
            #"data_range": {
            #    "min": 180,
            #    "max": 340
            #},
            "description": "Temperature as perceived factoring humidity/wind (feels-like).",
            "notes": "Sometimes called heat index or wind chill combined."
        },
        "10-metre-u-wind-component": {
            #"data_range": {
            #    "min": -100,
            #    "max": 100
            #},
            "description": "East-west wind at 10m above ground (m/s).",
            "notes": "Negative = westward, positive = eastward."
        },
        "10-metre-v-wind-component": {
            #"data_range": {
            #    "min": -100,
            #    "max": 100
            #},
            "description": "North-south wind at 10m above ground (m/s).",
            "notes": "Negative = southward, positive = northward."
        },
        "unknown": {
            #"data_range": {
            #    "min": 0,
            #    "max": 0
            #},
            "description": "unknown",
            "notes": "unknown"
        },
        "percent-frozen-precipitation": {
            #"data_range": {
            #    "min": 0,
            #    "max": 100
            #},
            "description": "Percentage of precipitation that is frozen (snow/sleet).",
            "notes": "Ranges from 0% (all liquid) to 100% (all frozen)."
        },
        "precipitation-rate": {
            #"data_range": {
            #    "min": 0,
            #    "max": 0.1
            #},
            "description": "Rate of precipitation at the surface (kg m^-2 s^-1).",
            "notes": "0.1 is extremely heavy precipitation."
        },
        "categorical-snow": {
            #"data_range": {
            #    "min": 0,
            #    "max": 1
            #},
            "description": "Indicates presence (1) or absence (0) of snow (code table 4.222).",
            "notes": "Categorical parameter for snow."
        },
        "categorical-ice-pellets": {
            #"data_range": {
            #    "min": 0,
            #    "max": 1
            #},
            "description": "Indicates presence (1) or absence (0) of ice pellets.",
            "notes": "Code table 4.222."
        },
        "categorical-freezing-rain": {
            #"data_range": {
            #    "min": 0,
            #    "max": 1
            #},
            "description": "Indicates presence (1) or absence (0) of freezing rain.",
            "notes": "Code table 4.222 for supercooled liquid precipitation."
        },
        "categorical-rain": {
            #"data_range": {
            #    "min": 0,
            #    "max": 1
            #},
            "description": "Indicates presence (1) or absence (0) of rain (liquid).",
            "notes": "Code table 4.222."
        },
        "forecast-surface-roughness": {
            #"data_range": {
            #    "min": 0,
            #    "max": 5
            #},
            "description": "Surface roughness length used in flux calculations, in meters.",
            "notes": "Up to several meters in forests or urban areas."
        },
        "frictional-velocity": {
            #"data_range": {
            #    "min": 0,
            #    "max": 2
            #},
            "description": "Scaling velocity representing turbulent shear near the surface.",
            "notes": "Higher friction velocity = stronger turbulence."
        },
        "vegetation": {
            #"data_range": {
            #    "min": 0,
            #    "max": 100
            #},
            "description": "Fraction of surface covered by vegetation, in percent.",
            "notes": "0% = bare ground, 100% = fully vegetated."
        },
        "soil-type": {
            #"data_range": {
            #    "min": 0,
            #    "max": 30
            #},
            "description": "Categorical index representing soil texture (code table 4.213).",
            "notes": "Values correspond to sand, clay, silt, etc."
        },
        "wilting-point": {
            #"data_range": {
            #    "min": 0,
            #    "max": 1
            #},
            "description": "Soil moisture fraction below which plants cannot extract water.",
            "notes": "Typically 0.1–0.3 for many soil types."
        },
        "field-capacity": {
            #"data_range": {
            #    "min": 0,
            #    "max": 1
            #},
            "description": "Soil moisture fraction at which drainage stops (not saturated).",
            "notes": "Often 0.2–0.4 for many soils."
        },
        "sunshine-duration": {
            #"data_range": {
            #    "min": 0,
            #    "max": 86400
            #},
            "description": "Number of seconds of bright sunshine at the surface.",
            "notes": "0 = no sun, 86400 = continuous 24h sunlight."
        },
        "surface-lifted-index": {
            #"data_range": {
            #    "min": -10,
            #    "max": 20
            #},
            "description": "Stability index comparing a surface parcel to 500 hPa environment.",
            "notes": "Negative = unstable, positive = stable."
        },
        "convective-available-potential-energy": {
            #"data_range": {
            #    "min": 0,
            #    "max": 7000
            #},
            "description": "Energy available for convection, indicating thunderstorm potential.",
            "notes": "Values over 4000 J/kg can be extreme."
        },
        "convective-inhibition": {
            #"data_range": {
            #    "min": 0,
            #    "max": 1000
            #},
            "description": "Energy preventing convection (negative buoyancy).",
            "notes": "Values >200–300 J/kg often inhibit storms."
        },
        "precipitable-water": {
            #"data_range": {
            #    "min": 0,
            #    "max": 80
            #},
            "description": "Total column water vapor if all condensed (kg/m^2).",
            "notes": "5–60 typical; can exceed 70 in tropics."
        },
        "cloud-water": {
            #"data_range": {
            #    "min": 0,
            #    "max": 5
            #},
            "description": "Integrated column of cloud liquid water (kg/m^2).",
            "notes": "Often <3; can be higher in strong convection."
        },
        "total-ozone": {
            #"data_range": {
            #    "min": 100,
            #    "max": 600
            #},
            "description": "Total column ozone in Dobson Units (DU).",
            "notes": "Typical range ~200–500 DU, <200 = ozone hole conditions."
        },
        "low-cloud-cover": {
            #"data_range": {
            #    "min": 0,
            #    "max": 100
            #},
            "description": "Percentage of sky covered by low-level clouds.",
            "notes": "0% = none, 100% = fully covered by low clouds."
        },
        "medium-cloud-cover": {
            #"data_range": {
            #    "min": 0,
            #    "max": 100
            #},
            "description": "Percentage of sky covered by mid-level clouds.",
            "notes": "0% = none, 100% = fully covered by mid-level clouds."
        },
        "high-cloud-cover": {
            #"data_range": {
            #    "min": 0,
            #    "max": 100
            #},
            "description": "Percentage of sky covered by high-level clouds (e.g., cirrus).",
            "notes": "0% = none, 100% = fully covered by high clouds."
        },
        "storm-relative-helicity": {
            #"data_range": {
            #    "min": 0,
            #    "max": 3000
            #},
            "description": "Measure of horizontal vorticity a storm ingests, linked to rotating storms.",
            "notes": "Values >300–400 can signal supercell potential."
        },
        "u-component-storm-motion": {
            #"data_range": {
            #    "min": -100,
            #    "max": 100
            #},
            "description": "East-west component of storm motion (m/s).",
            "notes": "Negative = westward, positive = eastward."
        },
        "v-component-storm-motion": {
            #"data_range": {
            #    "min": -100,
            #    "max": 100
            #},
            "description": "North-south component of storm motion (m/s).",
            "notes": "Negative = southward, positive = northward."
        },
        "tropopause-pressure": {
            #"data_range": {
            #    "min": 5000,
            #    "max": 30000
            #},
            "description": "Pressure at the tropopause (Pa).",
            "notes": "Typically ~200–300 hPa (~20000–30000 Pa)."
        },
        "icao-standard-atmosphere-reference-height": {
            #"data_range": {
            #    "min": 8000,
            #    "max": 20000
            #},
            "description": "Height in the standard atmosphere for the tropopause or reference altitude.",
            "notes": "Often ~11000 m in mid-latitudes."
        },
        "vertical-speed-shear": {
            #"data_range": {
            #    "min": 0,
            #    "max": 0.01
            #},
            "description": "Shear in vertical wind speed near the tropopause (s^-1).",
            "notes": "Higher shear may indicate strong jet streams."
        },
        "pressure": {
            #"data_range": {
            #    "min": 5000,
            #    "max": 110000
            #},
            "description": "Atmospheric pressure at the level of maximum wind.",
            "notes": "Jet streams often ~200–300 hPa, can vary widely."
        },
        "100-metre-u-wind-component": {
            #"data_range": {
            #    "min": -100,
            #    "max": 100
            #},
            "description": "East-west wind at 100m above ground in m/s.",
            "notes": "Negative = westward, positive = eastward."
        },
        "100-metre-v-wind-component": {
            #"data_range": {
            #    "min": -100,
            #    "max": 100
            #},
            "description": "North-south wind at 100m above ground in m/s.",
            "notes": "Negative = southward, positive = northward."
        },
        "best-4-layer-lifted-index": {
            #"data_range": {
            #    "min": -10,
            #    "max": 20
            #},
            "description": "Lifted index using the best 4 layers near surface, indicating stability.",
            "notes": "Negative = unstable, positive = stable."
        },
        "potential-temperature": {
            #"data_range": {
            #    "min": 150,
            #    "max": 400
            #},
            "description": "Temperature an air parcel would have if brought adiabatically to 1000 hPa.",
            "notes": "Widely used to identify air mass properties."
        },
        "pressure-of-level-from-which-parcel-was-lifted": {
            #"data_range": {
            #    "min": 5000,
            #    "max": 110000
            #},
            "description": "Pressure of the originating level from which a parcel is lifted.",
            "notes": "Near surface ~100000 Pa; can be lower in upper levels."
        },
        "land-sea-mask": {
            #"data_range": {
            #    "min": 0,
            #    "max": 1
            #},
            "description": "Mask indicating land (1) vs. sea (0).",
            "notes": "Coastal cells may have fractional values."
        },
        "sea-ice-area-fraction": {
            #"data_range": {
            #    "min": 0,
            #    "max": 1
            #},
            "description": "Fraction of sea surface covered by ice (0=none, 1=fully covered).",
            "notes": "Intermediate values near the ice edge."
        },
        "sea-ice-temperature": {
            #"data_range": {
            #    "min": 200,
            #    "max": 300
            #},
            "description": "Surface temperature of sea ice in Kelvin.",
            "notes": "Below freezing up to near 273K, can approach 300K if melting."
        }
    },
    "gfswave": {
        "wind-speed": {
             #"data_range": {
             #    "min": 0,
             #    "max": 100
             #},
             "description": "Wind speed at the surface.",
             "notes": "Measured in meters per second (m/s). Values can range from calm conditions (~0 m/s) to hurricane-force winds (>30 m/s).",
             "units": "m s**-1"
         },
         "wind-direction": {
             #"data_range": {
             #    "min": 0,
             #    "max": 360
             #},
             "description": "Wind direction at the surface, indicating the direction from which the wind is blowing.",
             "notes": "Measured in degrees true. 0° indicates wind coming from the north, 90° from the east, 180° from the south, and 270° from the west.",
             "units": "Degree true"
         },
         "u-component-of-wind": {
             #"data_range": {
             #    "min": -100,
             #    "max": 100
             #},
             "description": "Zonal (east-west) component of the wind at the surface.",
             "notes": "Negative values indicate westward winds, while positive values indicate eastward winds. Measured in meters per second (m/s).",
             "units": "m s**-1"
         },
         "v-component-of-wind": {
             #"data_range": {
             #    "min": -100,
             #    "max": 100
             #},
             "description": "Meridional (north-south) component of the wind at the surface.",
             "notes": "Negative values indicate southward winds, while positive values indicate northward winds. Measured in meters per second (m/s).",
             "units": "m s**-1"
         },
         "significant-height-of-combined-wind-waves-and-swell": {
             #"data_range": {
             #    "min": 0,
             #    "max": 20
             #},
             "description": "Significant height of the combined wind waves and swell.",
             "notes": "Represents the average height of the highest one-third of waves. Measured in meters (m).",
             "units": "m"
         },
         "primary-wave-mean-period": {
             #"data_range": {
             #    "min": 0,
             #    "max": 30
             #},
             "description": "Mean period of the primary wave.",
             "notes": "Indicates the average time interval between consecutive wave crests. Measured in seconds (s).",
             "units": "s"
         },
         "primary-wave-direction": {
             #"data_range": {
             #    "min": 0,
             #    "max": 360
             #},
             "description": "Direction from which the primary wave is coming.",
             "notes": "Measured in degrees true. 0° indicates waves coming from the north, 90° from the east, etc.",
             "units": "Degree true"
         },
         "significant-height-of-wind-waves": {
             #"data_range": {
             #    "min": 0,
             #    "max": 15
             #},
             "description": "Significant height of wind-generated waves.",
             "notes": "Represents the average height of the highest one-third of wind-driven waves. Measured in meters (m).",
             "units": "m"
         },
         "significant-height-of-total-swell": {
             #"data_range": {
             #    "min": 0,
             #    "max": 15
             #},
             "description": "Significant height of the total swell.",
             "notes": "Represents the average height of the highest one-third of swell waves. Measured in meters (m).",
             "units": "m"
         },
         "mean-period-of-wind-waves": {
             #"data_range": {
             #    "min": 0,
             #    "max": 20
             #},
             "description": "Mean period of wind-generated waves.",
             "notes": "Indicates the average time interval between consecutive wind wave crests. Measured in seconds (s).",
             "units": "s"
         },
         "mean-period-of-total-swell": {
             #"data_range": {
             #    "min": 0,
             #    "max": 25
             #},
             "description": "Mean period of the total swell.",
             "notes": "Indicates the average time interval between consecutive swell wave crests. Measured in seconds (s).",
             "units": "s"
         },
         "direction-of-wind-waves": {
             #"data_range": {
             #    "min": 0,
             #    "max": 360
             #},
             "description": "Direction from which the wind-generated waves are coming.",
             "notes": "Measured in degrees true. 0° indicates waves coming from the north, 90° from the east, etc.",
             "units": "Degree true"
         },
         "direction-of-swell-waves": {
             #"data_range": {
             #    "min": 0,
             #    "max": 360
             #},
             "description": "Direction from which the swell waves are coming.",
             "notes": "Measured in degrees true. 0° indicates waves coming from the north, 90° from the east, etc.",
             "units": "Degree true"
         }

    }
}


def apply_parameter_meta(parameter_key, param_info, model="gfs"):
    """
    Merges the metadata from PARAMETER_META into the given param_info dict.
    """
    if parameter_key in PARAMETER_META[model]:
        meta_fields = PARAMETER_META[model][parameter_key]
        # Merge each top-level meta field into param_info
        for field_key, field_value in meta_fields.items():
            param_info[field_key] = field_value
    return param_info


"""
# Example usage from '/controllers' route:
#
# from flask import Flask, jsonify
# from parameter_meta import apply_parameter_meta
#
# app = Flask(__name__)
#
# @app.route('/controllers', methods=['GET'])
# def controllers_route():
#     key = "temperature"
#     param_info = {
#         "parameter_key": key,
#         "parameter_name": "Temperature",
#         "units": "K",
#         "type_of_level": "isobaricInPa",
#         "color_map": "coolwarm",
#         #"data_range": {"min": "N/A", "max": "N/A"}
#     #}
#     #updated = apply_parameter_meta(key, param_info)
#     #return jsonify(updated)
"""
