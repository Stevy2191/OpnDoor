"""OpnDoor - skeleton service.

Placeholder app so the image builds and runs. The real scheduler
(windows CRUD, UniFi Access client, lock/unlock jobs) gets added next.
"""
import os

from fastapi import FastAPI

VERSION = os.getenv("APP_VERSION", "dev")
TZ = os.getenv("TZ", "America/Chicago")

app = FastAPI(title="OpnDoor", version=VERSION)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": VERSION, "tz": TZ}
