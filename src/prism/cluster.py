"""Group followed accounts with Leiden clustering."""

import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from itertools import combinations
from typing import cast

import igraph
import leidenalg

from .collect import event_key, timestamp
from .descriptions import describe_group
from .gems import GEM_COLORS, gem_color
from .models.clustering import (
    CandidateTrial,
    ClusterCandidate,
    ClusterResult,
    Group,
    GroupDraft,
    NamedGroup,
    WeeklyEvidence,
    WeightedConnection,
)
from .models.collection import CollectedSnapshot
from .models.publication import ManagedList

GEMS = tuple(GEM_COLORS)
STRONGEST_CONNECTION_LIMIT = 50


@dataclass(frozen=True)
class ClusterOptions:
    resolutions: tuple[float, ...] = (0.5, 0.75, 1, 1.25, 1.5, 2, 3)
    seeds: tuple[int, ...] = (17, 42, 93)
    min_size: int = 25
    preferred_lists_min: int = 8
    preferred_lists_max: int = 8
    preferred_size_min: int = 40
    preferred_size_max: int = 100
    one_way_weight: float = 1
    mutual_weight: float = 4
    reply_weight: float = 3
    quote_weight: float = 2
    repost_weight: float = 1
    weekly_cap: int = 3
    reciprocal_multiplier: float = 1.25
    follow_influence: float = 0.5

    def __post_init__(self):
        counts = (
            self.min_size,
            self.preferred_lists_min,
            self.preferred_lists_max,
            self.preferred_size_min,
            self.preferred_size_max,
            self.weekly_cap,
        )
        if any(type(value) is not int for value in counts):
            raise ValueError("Member counts, list counts, and the weekly cap must be integers")
        if not self.resolutions or not self.seeds:
            raise ValueError("At least one resolution and seed are required")
        weights = (
            self.one_way_weight,
            self.mutual_weight,
            self.reply_weight,
            self.quote_weight,
            self.repost_weight,
            self.reciprocal_multiplier,
        )
        if any(not math.isfinite(value) or value <= 0 for value in (*self.resolutions, *weights)):
            raise ValueError("Resolutions and weights must be finite and positive")
        if self.min_size < 25 or self.weekly_cap < 1:
            raise ValueError("Minimum size must be at least 25. Weekly cap must be positive")
        if not 0 <= self.follow_influence <= 1:
            raise ValueError("Follow influence must be between zero and one")
        if not 1 <= self.preferred_lists_min <= self.preferred_lists_max:
            raise ValueError("Invalid preferred list range")
        if not self.min_size <= self.preferred_size_min <= self.preferred_size_max:
            raise ValueError("Invalid preferred member range")
        if any(not isinstance(seed, int) or not 0 <= seed < 2**31 for seed in self.seeds):
            raise ValueError("Seeds must be integers between 0 and 2147483647")


def weighted_edges(
    snapshot: CollectedSnapshot, options: ClusterOptions
) -> list[WeightedConnection]:
    members = {person["did"] for person in snapshot["people"]}
    follows = {
        (source, target)
        for source, target in snapshot["follows"]
        if source != target and source in members and target in members
    }
    pairs = defaultdict(list)
    seen: set[tuple[str, str, str, str, str]] = set()
    start, end = timestamp(snapshot["cutoff"]), timestamp(snapshot["created_at"])
    if start is None:
        raise ValueError("Invalid snapshot.cutoff: expected an ISO timestamp with a time zone")
    if end is None:
        raise ValueError("Invalid snapshot.created_at: expected an ISO timestamp with a time zone")
    for event in snapshot["events"]:
        source, target = event["source"], event["target"]
        when = timestamp(event["time"])
        if source == target or source not in members or target not in members:
            continue
        if when is None or not start <= when <= end or event_key(event) in seen:
            continue
        seen.add(event_key(event))
        pairs[tuple(sorted((source, target)))].append(event)
    all_pairs = set(pairs) | {tuple(sorted(pair)) for pair in follows}
    weights = {
        "reply": options.reply_weight,
        "quote": options.quote_weight,
        "repost": options.repost_weight,
    }
    edges: list[WeightedConnection] = []
    for source, target in sorted(all_pairs):
        events = pairs[(source, target)]
        weeks = Counter()
        replies = set()
        counts = Counter()
        for event in events:
            event_time = timestamp(event["time"])
            if event_time is None:
                continue
            year, week, _ = event_time.isocalendar()
            weeks[(f"{year}-W{week:02}", event["kind"])] += 1
            counts[event["kind"]] += 1
            if event["kind"] == "reply":
                replies.add(event["source"])
        weekly_evidence: list[WeeklyEvidence] = [
            {
                "week": week,
                "kind": kind,
                "observed": count,
                "counted": min(count, options.weekly_cap),
            }
            for (week, kind), count in sorted(weeks.items())
        ]
        interaction_weight = sum(row["counted"] * weights[row["kind"]] for row in weekly_evidence)
        reciprocal = len(replies) == 2
        if reciprocal:
            interaction_weight *= options.reciprocal_multiplier
        directions = [
            list(pair) for pair in ((source, target), (target, source)) if pair in follows
        ]
        follow_weight = (
            options.mutual_weight
            if len(directions) == 2
            else options.one_way_weight
            if directions
            else 0
        )
        edges.append(
            {
                "source": source,
                "target": target,
                "follow_weight": follow_weight,
                "follow_directions": directions,
                "interaction_weight": interaction_weight,
                "reciprocal_replies": reciprocal,
                "counts": dict(counts),
                "weeks": weekly_evidence,
                "examples": events[:5],
            }
        )
    follow_total = sum(edge["follow_weight"] for edge in edges)
    interaction_total = sum(edge["interaction_weight"] for edge in edges)
    follow_influence = options.follow_influence if interaction_total else 1
    interaction_influence = 1 - options.follow_influence if follow_total else 1
    for edge in edges:
        edge["follow_normalized"] = edge["follow_weight"] / follow_total if follow_total else 0
        edge["interaction_normalized"] = (
            edge["interaction_weight"] / interaction_total if interaction_total else 0
        )
        edge["weight"] = (
            edge["follow_normalized"] * follow_influence
            + edge["interaction_normalized"] * interaction_influence
        )
    return edges


def assign_names(groups: list[GroupDraft], previous: list[ManagedList]) -> list[NamedGroup]:
    candidates: list[tuple[float, str, int, int]] = []
    for index, group in enumerate(groups):
        members = {person["did"] for person in group["members"]}
        for former_index, former in enumerate(previous):
            old = set(former["members"])
            similarity = len(members & old) / len(members | old) if members | old else 0
            if similarity >= 0.30:
                candidates.append((-similarity, former["name"], index, former_index))
    assignments: dict[int, tuple[str, float, str | None]] = {}
    assigned: set[int] = set()
    used: set[int] = set()
    for score, _, index, former_index in sorted(candidates):
        if index in assigned or former_index in used:
            continue
        former = previous[former_index]
        assignments[index] = (former["name"], -score, former["uri"])
        assigned.add(index)
        used.add(former_index)
    reserved = {former["name"] for former in previous}
    number = 0
    for index, _group in enumerate(groups):
        if index in assigned:
            continue
        while True:
            name = GEMS[number % len(GEMS)]
            if number >= len(GEMS):
                name += f" {number // len(GEMS) + 1}"
            number += 1
            if name not in reserved:
                break
        assignments[index] = (name, 0, None)
        reserved.add(name)
    return [
        {
            **group,
            "name": assignments[index][0],
            "overlap": assignments[index][1],
            "previous_uri": assignments[index][2],
        }
        for index, group in enumerate(groups)
    ]


def _result_group(group: NamedGroup, created_at: str, days: int) -> Group:
    return {
        "members": group["members"],
        "strongest_connections": group["connections"][:STRONGEST_CONNECTION_LIMIT],
        "name": group["name"],
        "overlap": group["overlap"],
        "previous_uri": group["previous_uri"],
        "icon_color": gem_color(group["name"]),
        **describe_group(group["members"], created_at, days),
    }


def connected_groups(membership: list[int], graph: igraph.Graph) -> list[list[int]]:
    groups = defaultdict(list)
    for index, community in enumerate(membership):
        groups[community].append(index)
    connected = []
    for indices in groups.values():
        subgraph = graph.induced_subgraph(indices)
        for component in subgraph.connected_components():
            connected.append([indices[index] for index in component])
    return sorted(connected, key=lambda group: (-len(group), group))


def candidate_preference(candidate: ClusterCandidate) -> tuple[int, float, float]:
    return (
        -candidate["list_count_distance"],
        candidate["preferred_size_fraction"],
        candidate["agreement"],
    )


def cluster_snapshot(
    snapshot: CollectedSnapshot, options: ClusterOptions, previous: list[ManagedList]
) -> ClusterResult:
    people = {person["did"]: person for person in snapshot["people"]}
    nodes = sorted(people)
    indices = {did: index for index, did in enumerate(nodes)}
    edges = weighted_edges(snapshot, options)
    union = igraph.Graph(
        n=len(nodes),
        edges=[
            (indices[edge["source"]], indices[edge["target"]])
            for edge in edges
            if edge["weight"] > 0
        ],
    )
    graphs, influences = [], []
    for weight_key, influence in (
        ("follow_normalized", options.follow_influence),
        ("interaction_normalized", 1 - options.follow_influence),
    ):
        if weight_key == "follow_normalized":
            selected = [edge for edge in edges if edge["follow_normalized"] > 0]
            graph_weights = [edge["follow_normalized"] for edge in selected]
        else:
            selected = [edge for edge in edges if edge["interaction_normalized"] > 0]
            graph_weights = [edge["interaction_normalized"] for edge in selected]
        if not selected:
            continue
        graph = igraph.Graph(
            n=len(nodes),
            edges=[(indices[edge["source"]], indices[edge["target"]]) for edge in selected],
        )
        graph.es["weight"] = graph_weights
        graphs.append(graph)
        influences.append(influence)
    if len(graphs) == 1:
        influences = [1]
    candidates: list[ClusterCandidate] = []
    for resolution in options.resolutions if graphs else ():
        trials: list[CandidateTrial] = []
        for seed in options.seeds:
            partitions = [
                leidenalg.RBConfigurationVertexPartition(
                    graph, weights="weight", resolution_parameter=resolution
                )
                for graph in graphs
            ]
            optimiser = leidenalg.Optimiser()
            optimiser.set_rng_seed(seed)
            optimiser.optimise_partition_multiplex(
                partitions, layer_weights=influences, n_iterations=-1
            )
            quality = sum(
                partition.quality() * influence
                for partition, influence in zip(partitions, influences, strict=True)
            )
            trials.append({"seed": seed, "score": quality, "membership": partitions[0].membership})
        best = max(trials, key=lambda trial: trial["score"])
        groups = connected_groups(best["membership"], union)
        publishable = [group for group in groups if len(group) >= options.min_size]
        count = len(publishable)
        agreement = [
            igraph.compare_communities(
                first["membership"], second["membership"], method="adjusted_rand"
            )
            for first, second in combinations(trials, 2)
        ]
        candidates.append(
            {
                "resolution": resolution,
                "seed": best["seed"],
                "score": best["score"],
                "trials": trials,
                "groups": groups,
                "publishable_count": count,
                "list_count_distance": max(
                    options.preferred_lists_min - count, 0, count - options.preferred_lists_max
                ),
                "preferred_size_fraction": sum(
                    len(group)
                    for group in publishable
                    if options.preferred_size_min <= len(group) <= options.preferred_size_max
                )
                / len(nodes)
                if nodes
                else 0,
                "agreement": sum(agreement) / len(agreement) if agreement else 1,
            }
        )
    chosen = max(candidates, key=candidate_preference) if candidates else None
    communities = chosen["groups"] if chosen else [[index] for index in range(len(nodes))]
    group_drafts: list[GroupDraft] = []
    small_groups: list[list[str]] = []
    for community in communities:
        members = {nodes[index] for index in community}
        if len(community) < options.min_size:
            small_groups.append(sorted(members))
            continue
        within = [edge for edge in edges if edge["source"] in members and edge["target"] in members]
        strengths: defaultdict[str, float] = defaultdict(float)
        for edge in within:
            strengths[edge["source"]] += edge["weight"]
            strengths[edge["target"]] += edge["weight"]
        group_drafts.append(
            {
                "members": [
                    {**people[did], "strength": strengths[did]}
                    for did in sorted(members, key=lambda did: (-strengths[did], did))
                ],
                "connections": sorted(within, key=lambda edge: (-edge["weight"], edge["source"])),
            }
        )
    groups = [
        _result_group(named_group, snapshot["created_at"], snapshot["days"])
        for named_group in assign_names(group_drafts, previous)
    ]
    assigned = {person["did"] for group in groups for person in group["members"]}
    return cast(
        ClusterResult,
        {
            "snapshot_id": snapshot["id"],
            "created_at": snapshot["created_at"],
            "cutoff": snapshot["cutoff"],
            "days": snapshot["days"],
            "actor": snapshot["actor"],
            "options": asdict(options),
            "chosen_resolution": chosen["resolution"] if chosen else None,
            "chosen_seed": chosen["seed"] if chosen else None,
            "candidates": [
                {key: value for key, value in candidate.items() if key != "groups"}
                for candidate in candidates
            ],
            "groups": groups,
            "small_groups": small_groups,
            "unassigned": [people[did] for did in nodes if did not in assigned],
            "coverage": snapshot["coverage"],
            "root_coverage": snapshot["root_coverage"],
            "reported_follows": snapshot["reported_follows"],
            "fetched_follows": len(people),
            "follow_count_difference": snapshot["follow_count_difference"],
            "temporary_failures": snapshot["temporary_failures"],
            "observed_interactions": len(snapshot["events"]),
            "publishing": {"status": "not_started"},
        },
    )
