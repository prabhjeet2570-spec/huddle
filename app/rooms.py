"""The local room catalog, shared by discovery, validation and assistant tools."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field


class Room(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    capacity: int = Field(gt=0)
    floor: int = Field(default=1, ge=1, le=4)


ROOMS = (
    Room(id="cedar", name="Room 101", capacity=4, floor=1),
    Room(id="maple", name="Room 102", capacity=8, floor=1),
    Room(id="birch", name="Room 103", capacity=12, floor=1),
    Room(id="willow", name="Room 104", capacity=2, floor=1),
    Room(id="aspen", name="Room 105", capacity=2, floor=1),
    Room(id="fern", name="Room 106", capacity=3, floor=1),
    Room(id="elm", name="Room 201", capacity=4, floor=2),
    Room(id="sage", name="Room 202", capacity=4, floor=2),
    Room(id="ivy", name="Room 203", capacity=4, floor=2),
    Room(id="oak", name="Room 204", capacity=6, floor=2),
    Room(id="pine", name="Room 205", capacity=6, floor=2),
    Room(id="hazel", name="Room 206", capacity=6, floor=2),
    Room(id="alder", name="Room 301", capacity=8, floor=3),
    Room(id="laurel", name="Room 302", capacity=8, floor=3),
    Room(id="olive", name="Room 303", capacity=10, floor=3),
    Room(id="juniper", name="Room 304", capacity=10, floor=3),
    Room(id="magnolia", name="Room 305", capacity=12, floor=3),
    Room(id="sequoia", name="Room 306", capacity=16, floor=3),
    Room(id="cypress", name="Room 401", capacity=16, floor=4),
    Room(id="grove", name="Room 402", capacity=20, floor=4),
    Room(id="atrium", name="Room 403", capacity=24, floor=4),
    Room(id="forum", name="Room 404", capacity=30, floor=4),
    Room(id="studio", name="Room 405", capacity=6, floor=4),
    Room(id="loft", name="Room 406", capacity=10, floor=4),
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
