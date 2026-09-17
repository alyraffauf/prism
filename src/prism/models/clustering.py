"""Clustering inputs, candidates, groups, and results."""

from typing import Any, NotRequired, TypedDict

from .collection import AccountCoverage, CoverageState, Event, InteractionKind, Person


class WeeklyEvidence(TypedDict):
    week: str
    kind: InteractionKind
    observed: int
    counted: int


class WeightedConnection(TypedDict):
    source: str
    target: str
    follow_weight: float
    follow_directions: list[list[str]]
    interaction_weight: float
    reciprocal_replies: bool
    counts: dict[str, int]
    weeks: list[WeeklyEvidence]
    examples: list[Event]
    follow_normalized: NotRequired[float]
    interaction_normalized: NotRequired[float]
    weight: NotRequired[float]


class CandidateTrial(TypedDict):
    seed: int
    score: float
    membership: list[int]


class ClusterCandidate(TypedDict):
    resolution: float
    seed: int
    score: float
    trials: list[CandidateTrial]
    groups: NotRequired[list[list[int]]]
    publishable_count: int
    list_count_distance: int
    preferred_size_fraction: float
    agreement: float


class GroupDraft(TypedDict):
    members: list[Person]
    connections: list[WeightedConnection]


class NamedGroup(GroupDraft):
    name: str
    overlap: float
    previous_uri: str | None


class Group(TypedDict):
    members: list[Person]
    strongest_connections: list[WeightedConnection]
    # Version 1 results used this field. Keep it readable so interrupted runs
    # created by an older Prism version can still produce a report.
    connections: NotRequired[list[WeightedConnection]]
    name: str
    overlap: float
    previous_uri: str | None
    icon_color: str
    description: str
    descriptionFacets: NotRequired[list[dict[str, Any]]]
    uri: NotRequired[str]
    url: NotRequired[str]


class GroupDescription(TypedDict):
    description: str
    descriptionFacets: NotRequired[list[dict[str, Any]]]


class PublicationSummary(TypedDict):
    status: str
    batches: NotRequired[int]
    writes: NotRequired[int]
    planned_writes: NotRequired[int | None]
    error: NotRequired[str | None]
    active_lists: NotRequired[list[dict[str, Any]]]
    inactive_lists: NotRequired[list[dict[str, Any]]]


class ClusterResult(TypedDict):
    version: int
    snapshot_id: str
    created_at: str
    cutoff: str
    days: int
    actor: Person
    options: dict[str, Any]
    chosen_resolution: float | None
    chosen_seed: int | None
    candidates: list[ClusterCandidate]
    groups: list[Group]
    small_groups: list[list[str]]
    unassigned: list[Person]
    coverage: list[AccountCoverage]
    root_coverage: CoverageState
    reported_follows: int | None
    fetched_follows: int
    follow_count_difference: int | None
    temporary_failures: int
    observed_interactions: int
    publishing: PublicationSummary
