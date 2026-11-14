"""Request models. FastAPI validates incoming JSON against these and
returns a 422 with details automatically when the body doesn't fit."""

import datetime as dt
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

# Python's weekday() convention, same as the database: 0 = Monday ... 6 = Sunday
DayOfWeek = Annotated[int, Field(ge=0, le=6)]


class EventIn(BaseModel):
    """One lid event from the ESP32 (mirrors the overview payload)."""

    device_id: str = Field(min_length=1)
    compartment: int = Field(ge=1)                     # 1 = Sun ... 7 = Sat (= AprilTag ID)
    lid_open: bool
    camera_compartment: int | None = Field(default=None, ge=1)
    camera_confidence: float | None = Field(default=None, ge=0, le=1)
    weight_change: float | None = None


class MedicationIn(BaseModel):
    """A new medication from the schedule editor. No compartment: a
    medication can be in every day's compartment."""

    model_config = ConfigDict(str_strip_whitespace=True)  # "  " fails min_length

    display_name: str = Field(min_length=1, max_length=100)
    notes: str | None = Field(default=None, max_length=500)


class DoseIn(BaseModel):
    """One dose from the editor: a medication at one time on some days.
    The backend makes one schedule row per day, in that day's compartment."""

    medication_id: int = Field(ge=1)
    time: dt.time                                       # "08:00"
    days: list[DayOfWeek] = Field(min_length=1, max_length=7)


class DoseUpdate(BaseModel):
    """Edit an existing dose group, found by (medication_id, time).
    `days` is the complete new set of days, not a change list."""

    medication_id: int = Field(ge=1)
    time: dt.time                                       # current time of the group
    new_time: dt.time | None = None                     # omit to keep the time
    days: list[DayOfWeek] = Field(min_length=1, max_length=7)