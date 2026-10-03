"""Request models. FastAPI validates incoming JSON against these and
returns a 422 with details automatically when the body doesn't fit."""

from pydantic import BaseModel, Field


class EventIn(BaseModel):
    """One lid event from the ESP32 (mirrors the overview payload)."""

    device_id: str = Field(min_length=1)
    compartment: int = Field(ge=1)                     # 1 = A (GPIO4), 2 = B (GPIO5)
    lid_open: bool
    camera_compartment: int | None = Field(default=None, ge=1)
    camera_confidence: float | None = Field(default=None, ge=0, le=1)
    weight_change: float | None = None

    