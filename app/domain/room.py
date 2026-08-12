from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Room:
    id: UUID
    org_id: UUID
    name: str
    capacity: int
