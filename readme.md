# Foreshadow API

Foreshadow API provides efficient, real-time access to Global Forecast System (GFS) data, optimized for point-based forecasts and map visualizations. Leveraging FastAPI, it offers streamlined, performant endpoints tailored for applications requiring precise weather forecasting data.

## Available API Routes

### Base Route
- **GET /**
  Provides a simple status message describing the API's functionality, caching strategies, and data processing capabilities.

### Tile Rendering
- **GET /tiles/{model}/{param_key}/{hour_offset}/{z}/{x}/{y}.png**
  Fetches pre-rendered tile images based on weather data parameters.
  - **Parameters:**
    - `model`: The weather forecast model (e.g., GFS).
    - `param_key`: Specific parameter to visualize (e.g., temperature).
    - `hour_offset`: Forecast hour offset from the latest data run.
    - `z`, `x`, `y`: Tile coordinates for map rendering.

### Parameter Listings
- **GET /list_parameters/{model}/{hour_offset}**
  Returns a list of available parameters for a specified model and hour offset.

- **GET /parameters**
  Provides detailed definitions and metadata for available weather forecast models.

### Point Forecasts
- **POST /point** or **POST /point/{hour_offset}**
  Fetches weather data for specific geographic coordinates.
  - **Request Body:** JSON containing `model`, `param_keys`, `lat`, `lon`, and optional parameters (`start_hour_offset`, `level`, `typeOfLevel`, `stepType`).

### Streaming Forecast
- **POST /forecast-stream**
  Initiates a streaming connection providing incremental updates of forecast computation progress, useful for long-running data processes.
  - **Request Body:** Same JSON structure as `/forecast`.
  - **Response:** Streams JSON progress messages and final forecast data.

### Forecast Data
- **POST /forecast**
  Retrieves full timeseries forecast data based on provided parameters.
  - **Request Body:** JSON with details such as `model`, `param_keys`, `lat`, `lon`, and additional optional fields.

## Key Features
- **Efficient Caching:** Uses Redis-backed caching to significantly enhance performance and reduce latency.
- **Concurrent Processing:** Utilizes thread pools for optimized CPU utilization.
- **Dynamic Handling:** Robust handling for missing values and coordinate transformations ensuring accurate forecasts.

## Running the API
To launch the API server:

```bash
uvicorn main:app --host 0.0.0.0 --port 5001 --workers $(nproc)
```

Replace `$(nproc)` with the desired number of worker processes.

## Dependencies
- FastAPI
- Uvicorn
- Redis
- NumPy
- GDAL

Ensure Redis is installed and running to enable caching.

Foreshadow API is designed to deliver high-performance weather forecasts tailored specifically to your application's geographical needs.

## Contributions & Community

Want to help build the future of weather insights? We're open-source and community-driven!

Check out our [CONTRIBUTING.md](./CONTRIBUTING.md) file for guidance on how you can contribute to Parabl Foreshadow and make the world a bit safer, one forecast at a time!

## Contributors

Huge thanks to our awesome contributors for helping Parabl Foreshadow shine! _(Your name could be here—come join us!)_

## About Similie

[**Similie**](https://similie.org) is a tech company from Timor-Leste dedicated to building innovative solutions for international development and climate adaptation. We believe tech should **empower, not overwhelm**, and we're here to make positive impacts worldwide.

---

Built with ❤️ by **Similie** in Timor-Leste
