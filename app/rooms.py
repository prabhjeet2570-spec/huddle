"""A starter room catalog, kept in memory until persistence is introduced."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field


class Room(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    capacity: int = Field(gt=0)


ROOMS = (
    Room(id="cedar", name="Cedar", capacity=4),
    Room(id="maple", name="Maple", capacity=8),
    Room(id="birch", name="Birch", capacity=12),
)

router = APIRouter(prefix="/rooms", tags=["rooms"])


@router.get("", response_model=list[Room])
def list_rooms(
    min_capacity: Annotated[int | None, Query(ge=1)] = None,
) -> list[Room]:
    """List rooms by capacity; this does not check booking availability."""
    return [room for room in ROOMS if min_capacity is None or room.capacity >= min_capacity]


@router.get("/{room_id}", response_model=Room)
def get_room(room_id: str) -> Room:
    """Look up a room by its exact catalog ID."""
    for room in ROOMS:
        if room.id == room_id:
            return room
    raise HTTPException(status_code=404, detail="Room not found")
