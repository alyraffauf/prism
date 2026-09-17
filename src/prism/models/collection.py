"""Data collected from Bluesky and saved for clustering."""

from dataclasses import dataclass
from typing import Any, Literal, NotRequired, TypedDict, cast

TaskStatus = Literal["pending", "complete", "unavailable", "failed"]
InteractionKind = Literal["reply", "quote", "repost"]
CollectionKind = Literal["profile", "follows", "feed", "root_follows"]


@dataclass(frozen=True)
class CollectionTask:
    snapshot_id: str
    actor: str
    kind: CollectionKind


class Person(TypedDict):
    did: str
    handle: NotRequired[str | None]
    displayName: NotRequired[str | None]
    followsCount: NotRequired[int | None]
    followersCount: NotRequired[int | None]
    postsCount: NotRequired[int | None]
    authored_post_count: NotRequired[int | None]
    strength: NotRequired[float]


class Event(TypedDict):
    source: str
    target: str
    kind: InteractionKind
    uri: str
    time: str


class TaskState(TypedDict):
    status: TaskStatus
    cursor: str | None
    pages: int
    cursors: list[str]
    restarts: int
    error: NotRequired[str | None]
    profile: NotRequired[Person]


class CoverageState(TypedDict):
    status: TaskStatus
    error: str | None
    pages: int
    restarts: int


class AccountCoverage(Person):
    profile: CoverageState
    follows: CoverageState
    feed: CoverageState
    reported_follows: int | None
    fetched_follows: int
    follow_count_difference: int | None
    invalid_timestamps: int


class PeoplePage(TypedDict):
    people: list[Person]


class FeedPage(TypedDict):
    events: list[Event]
    authored_posts: list[str]
    invalid_timestamps: int
    before_window: bool


CollectedPage = PeoplePage | FeedPage


class CollectedSnapshot(TypedDict):
    id: str
    actor: Person
    created_at: str
    cutoff: str
    days: int
    dry_run: bool
    stage: str
    options: dict[str, Any]
    people: list[Person]
    follows: list[list[str]]
    events: list[Event]
    coverage: list[AccountCoverage]
    root_coverage: CoverageState
    reported_follows: int | None
    fetched_follows: int
    follow_count_difference: int | None
    temporary_failures: int
    previous: NotRequired[list[Any]]
    pds: NotRequired[str]


def _object(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Invalid {field}: expected an object")
    return value


def _list(value: object, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"Invalid {field}: expected a list")
    return value


def _string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Invalid {field}: expected a string")
    return value


def validate_person(value: object, field: str = "person") -> Person:
    person = _object(value, field)
    _string(person.get("did"), f"{field}.did")
    for key in ("handle", "displayName"):
        if key in person and person[key] is not None and not isinstance(person[key], str):
            raise ValueError(f"Invalid {field}.{key}: expected a string or null")
    for key in ("followsCount", "followersCount", "postsCount", "authored_post_count"):
        if key in person and person[key] is not None and type(person[key]) is not int:
            raise ValueError(f"Invalid {field}.{key}: expected an integer or null")
    if "strength" in person and (
        not isinstance(person["strength"], (int, float)) or isinstance(person["strength"], bool)
    ):
        raise ValueError(f"Invalid {field}.strength: expected a number")
    return cast(Person, person)


def validate_task_state(value: object, field: str = "task") -> TaskState:
    state = _object(value, field)
    if state.get("status") not in {"pending", "complete", "unavailable", "failed"}:
        raise ValueError(f"Invalid {field}.status")
    if not isinstance(state.get("pages"), int):
        raise ValueError(f"Invalid {field}.pages: expected an integer")
    if not isinstance(state.get("restarts"), int):
        raise ValueError(f"Invalid {field}.restarts: expected an integer")
    cursors = _list(state.get("cursors"), f"{field}.cursors")
    if not all(isinstance(cursor, str) for cursor in cursors):
        raise ValueError(f"Invalid {field}.cursors: expected strings")
    cursor = state.get("cursor")
    if cursor is not None and not isinstance(cursor, str):
        raise ValueError(f"Invalid {field}.cursor: expected a string or null")
    return cast(TaskState, state)


def validate_coverage_state(value: object, field: str = "coverage") -> CoverageState:
    state = _object(value, field)
    if state.get("status") not in {"pending", "complete", "unavailable", "failed"}:
        raise ValueError(f"Invalid {field}.status")
    if state.get("error") is not None and not isinstance(state.get("error"), str):
        raise ValueError(f"Invalid {field}.error: expected a string or null")
    for key in ("pages", "restarts"):
        if type(state.get(key)) is not int:
            raise ValueError(f"Invalid {field}.{key}: expected an integer")
    return cast(CoverageState, state)


def validate_account_coverage(value: object, field: str = "account coverage") -> AccountCoverage:
    coverage = validate_person(value, field)
    for key in ("profile", "follows", "feed"):
        validate_coverage_state(coverage.get(key), f"{field}.{key}")
    for key in ("reported_follows", "follow_count_difference"):
        if coverage.get(key) is not None and type(coverage.get(key)) is not int:
            raise ValueError(f"Invalid {field}.{key}: expected an integer or null")
    for key in ("fetched_follows", "invalid_timestamps"):
        if type(coverage.get(key)) is not int:
            raise ValueError(f"Invalid {field}.{key}: expected an integer")
    return cast(AccountCoverage, coverage)


def validate_event(value: object, field: str = "event") -> Event:
    event = _object(value, field)
    for key in ("source", "target", "uri", "time"):
        _string(event.get(key), f"{field}.{key}")
    if event.get("kind") not in {"reply", "quote", "repost"}:
        raise ValueError(f"Invalid {field}.kind")
    return cast(Event, event)


def validate_page(value: object, kind: CollectionKind, field: str = "page") -> CollectedPage:
    page = _object(value, field)
    if kind in {"follows", "root_follows"}:
        people = _list(page.get("people"), f"{field}.people")
        for index, person in enumerate(people):
            validate_person(person, f"{field}.people[{index}]")
        return cast(PeoplePage, page)
    events = _list(page.get("events"), f"{field}.events")
    for index, event_value in enumerate(events):
        validate_event(event_value, f"{field}.events[{index}]")
    _list(page.get("authored_posts", []), f"{field}.authored_posts")
    if type(page.get("invalid_timestamps")) is not int:
        raise ValueError(f"Invalid {field}.invalid_timestamps: expected an integer")
    if type(page.get("before_window")) is not bool:
        raise ValueError(f"Invalid {field}.before_window: expected a boolean")
    return cast(FeedPage, page)


def validate_collected_snapshot(value: object, field: str = "snapshot") -> CollectedSnapshot:
    snapshot = _object(value, field)
    for key in ("id", "created_at", "cutoff", "stage"):
        _string(snapshot.get(key), f"{field}.{key}")
    validate_person(snapshot.get("actor"), f"{field}.actor")
    people = _list(snapshot.get("people"), f"{field}.people")
    for index, person in enumerate(people):
        validate_person(person, f"{field}.people[{index}]")
    follows = _list(snapshot.get("follows"), f"{field}.follows")
    for index, pair in enumerate(follows):
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not all(isinstance(item, str) for item in pair)
        ):
            raise ValueError(f"Invalid {field}.follows[{index}]: expected two account DIDs")
    events = _list(snapshot.get("events"), f"{field}.events")
    for index, event in enumerate(events):
        validate_event(event, f"{field}.events[{index}]")
    coverage = _list(snapshot.get("coverage"), f"{field}.coverage")
    for index, account in enumerate(coverage):
        validate_account_coverage(account, f"{field}.coverage[{index}]")
    validate_coverage_state(snapshot.get("root_coverage"), f"{field}.root_coverage")
    _object(snapshot.get("options"), f"{field}.options")
    for key in ("days", "fetched_follows", "temporary_failures"):
        if type(snapshot.get(key)) is not int:
            raise ValueError(f"Invalid {field}.{key}: expected an integer")
    for key in ("reported_follows", "follow_count_difference"):
        if snapshot.get(key) is not None and type(snapshot.get(key)) is not int:
            raise ValueError(f"Invalid {field}.{key}: expected an integer or null")
    if type(snapshot.get("dry_run")) is not bool:
        raise ValueError(f"Invalid {field}.dry_run: expected a boolean")
    if "previous" in snapshot:
        _list(snapshot["previous"], f"{field}.previous")
    if "pds" in snapshot:
        _string(snapshot["pds"], f"{field}.pds")
    return cast(CollectedSnapshot, snapshot)
