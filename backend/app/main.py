# API Entry Point
from fastapi import FastAPI
from .db import get_client

app = FastAPI(title="MediSense API", version="0.1.0")

@app.get("/health")
def health():
    """Server Running?"""
    return {"status": "ok"}

@app.get("/health/db")
def health_db():
    """Server reach Supabase? Return seeded devices."""
    result = get_client().table("devices").select("id, firmware_version, last_seen").execute()
    return {"status": "ok", "devices": result.data}