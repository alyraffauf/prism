"""Saved run records and their stages."""

from typing import Any, Literal, NotRequired, TypedDict, cast

from .clustering import ClusterResult
from .collection import Person
from .publication import ManagedList, PublicationIntent

SnapshotStage = Literal["collecting", "collected", "publishing", "complete"]


class RunRecord(TypedDict):
    id: str
    actor: Person
    created_at: str
    cutoff: str
    days: int
    dry_run: bool
    stage: SnapshotStage
    options: dict[str, Any]
    pds: NotRequired[str]
    previous: NotRequired[list[ManagedList]]
    result: NotRequired[ClusterResult]
    publication: NotRequired[PublicationIntent]


def validate_run_record(value: object, field: str = "run") -> RunRecord:
    if not isinstance(value, dict):
        raise ValueError(f"Invalid {field}: expected an object")
    for key in ("id", "created_at", "cutoff"):
        if not isinstance(value.get(key), str):
            raise ValueError(f"Invalid {field}.{key}: expected a string")
    actor = value.get("actor")
    if not isinstance(actor, dict) or not isinstance(actor.get("did"), str):
        raise ValueError(f"Invalid {field}.actor.did: expected a string")
    if value.get("stage") not in {"collecting", "collected", "publishing", "complete"}:
        raise ValueError(f"Invalid {field}.stage")
    if type(value.get("days")) is not int:
        raise ValueError(f"Invalid {field}.days: expected an integer")
    if type(value.get("dry_run")) is not bool:
        raise ValueError(f"Invalid {field}.dry_run: expected a boolean")
    if not isinstance(value.get("options"), dict):
        raise ValueError(f"Invalid {field}.options: expected an object")
    return cast(RunRecord, value)
