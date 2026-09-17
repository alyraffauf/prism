"""Repository state and publication plans."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict, cast

if TYPE_CHECKING:
    from .clustering import ClusterResult

WriteKind = Literal["create", "update", "delete"]
PublicationStatus = Literal["pending", "complete"]


class RepositoryRecord(TypedDict):
    uri: str
    value: dict[str, Any]


class Repository(TypedDict):
    commit: str
    lists: dict[str, RepositoryRecord]
    items: dict[str, RepositoryRecord]


class Icon(TypedDict):
    color: str
    sha256: str
    png: str


class ManagedList(TypedDict):
    uri: str
    name: str
    active: bool
    last_snapshot: str
    value: dict[str, Any]
    members: list[str]


class PlannedList(ManagedList):
    icon: Icon


class PublicationIntent(TypedDict):
    actor: str
    created_at: str
    lists: list[PlannedList]
    status: PublicationStatus
    batches: int
    writes: int
    planned_writes: NotRequired[int]
    error: NotRequired[str | None]


WriteOperation = TypedDict(
    "WriteOperation",
    {
        "$type": str,
        "collection": str,
        "rkey": str,
        "value": NotRequired[dict[str, Any]],
    },
)


@dataclass(frozen=True)
class PublicationPreparation:
    result: "ClusterResult"
    intent: PublicationIntent


def validate_repository(value: object, field: str = "repository") -> Repository:
    if not isinstance(value, dict):
        raise ValueError(f"Invalid {field}: expected an object")
    if not isinstance(value.get("commit"), str):
        raise ValueError(f"Invalid {field}.commit: expected a string")
    for collection in ("lists", "items"):
        records = value.get(collection)
        if not isinstance(records, dict):
            raise ValueError(f"Invalid {field}.{collection}: expected an object")
        for uri, record in records.items():
            if not isinstance(uri, str) or not isinstance(record, dict):
                raise ValueError(f"Invalid {field}.{collection}: invalid record")
            if record.get("uri") != uri:
                raise ValueError(f"Invalid {field}.{collection}.{uri}.uri")
            if not isinstance(record.get("value"), dict):
                raise ValueError(f"Invalid {field}.{collection}.{uri}.value")
    return cast(Repository, value)


def validate_managed_lists(value: object, field: str = "lists") -> list[ManagedList]:
    if not isinstance(value, list):
        raise ValueError(f"Invalid {field}: expected a list")
    for index, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise ValueError(f"Invalid {field}[{index}]: expected an object")
        for key in ("uri", "name", "last_snapshot"):
            if not isinstance(entry.get(key), str):
                raise ValueError(f"Invalid {field}[{index}].{key}: expected a string")
        if type(entry.get("active")) is not bool:
            raise ValueError(f"Invalid {field}[{index}].active: expected a boolean")
        if not isinstance(entry.get("members"), list):
            raise ValueError(f"Invalid {field}[{index}].members: expected a list")
        if not isinstance(entry.get("value"), dict):
            raise ValueError(f"Invalid {field}[{index}].value: expected an object")
    return cast(list[ManagedList], value)


def validate_planned_lists(value: object, field: str = "planned lists") -> list[PlannedList]:
    lists = validate_managed_lists(value, field)
    for index, entry in enumerate(lists):
        icon = entry.get("icon")
        if not isinstance(icon, dict):
            raise ValueError(f"Invalid {field}[{index}].icon: expected an object")
        for key in ("color", "sha256", "png"):
            if not isinstance(icon.get(key), str):
                raise ValueError(f"Invalid {field}[{index}].icon.{key}: expected a string")
    return cast(list[PlannedList], lists)
