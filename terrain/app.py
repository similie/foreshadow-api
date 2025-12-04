# terrain/app.py

from fastapi import FastAPI
from terrain.api import hydrology

app = FastAPI(
    title="4Shadow Terrain & Hydrology Engine",
    version="0.1.0",
    description="Processes DEM, flow directions, and rainfall for flood simulations.",
)

# Include your hydrology router
app.include_router(hydrology.router)


@app.get("/")
def root():
    return {
        "status": "terrain hydrology API running",
        "routes": [
            "/hydrology/jobs",
            "/hydrology/jobs/{job_id}",
            "/hydrology/jobs/{job_id}/tiles/{layer}/{z}/{x}/{y}.png",
        ],
    }


# Allow "python terrain/app.py" to run the server
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("terrain.app:app", host="0.0.0.0", port=5002, reload=True)
