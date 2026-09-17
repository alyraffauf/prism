"""Application workflow for online Prism runs."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import httpx

from .api import APIError, Bluesky, RetryingRequest
from .cluster import ClusterOptions, cluster_snapshot
from .collect import DEFAULT_CONCURRENCY, Collector
from .models.collection import CollectedSnapshot
from .models.publication import ManagedList, Repository
from .models.runs import RunRecord
from .publish import (
    make_intent,
    managed_lists,
    plan_changes,
    read_repository,
    synchronize,
)
from .report import failure_report, publication_summary, write_report
from .storage import Store

PasswordProvider = Callable[[], str]
ProgressReporter = Callable[[str], None]


@dataclass(frozen=True)
class PublicationContext:
    pds: str
    repository: Repository
    previous: list[ManagedList]


async def _collect_or_export(collector: Collector, snapshot: RunRecord) -> CollectedSnapshot:
    if snapshot["stage"] == "collecting":
        return await collector.collect()
    return collector.export()


async def _load_publication_context(
    api: Bluesky, store: Store, snapshot: RunRecord
) -> PublicationContext:
    did = snapshot["actor"]["did"]
    pds = snapshot.get("pds") or await api.pds(did)
    repository = await read_repository(api, pds, did)
    previous = managed_lists(repository, store.registered_lists(did))
    return PublicationContext(pds=pds, repository=repository, previous=previous)


def _initialize_publication(
    snapshot: RunRecord,
    exported: CollectedSnapshot,
    context: PublicationContext,
) -> None:
    if "publication" in snapshot:
        return
    snapshot["previous"] = context.previous
    result = cluster_snapshot(
        exported,
        ClusterOptions(**snapshot["options"]),
        context.previous,
    )
    preparation = make_intent(result, context.previous, context.repository)
    snapshot.update(
        result=preparation.result,
        publication=preparation.intent,
        stage="publishing",
        pds=context.pds,
    )


def _checkpoint_plan(store: Store, snapshot: RunRecord, repository: Repository) -> None:
    intent = snapshot.get("publication")
    if intent is None:
        raise ValueError("The run has no publication intent")
    intent.setdefault("planned_writes", len(plan_changes(intent, repository)))
    # Save the plan before the first account write so publication can resume.
    store.save_snapshot(snapshot)


async def _publish_if_requested(
    api: Bluesky,
    store: Store,
    snapshot: RunRecord,
    context: PublicationContext,
    password_provider: PasswordProvider,
) -> None:
    if snapshot["dry_run"]:
        return
    password = password_provider()
    await api.authenticate(context.pds, snapshot["actor"]["did"], password)
    del password
    await synchronize(api, store, snapshot, context.pds)


def _report_snapshot(collector: Collector, exported: CollectedSnapshot) -> CollectedSnapshot:
    current = cast(CollectedSnapshot, {**exported})
    cast(dict[str, Any], current).update(collector.export_metadata())
    return current


def _complete_run(
    store: Store,
    snapshot: RunRecord,
    collector: Collector,
    exported: CollectedSnapshot,
    directory: Path,
    progress: ProgressReporter,
) -> None:
    result = snapshot.get("result")
    if result is None:
        raise ValueError("The run has no clustering result")
    result["publishing"] = publication_summary(snapshot)
    write_report(directory, result, _report_snapshot(collector, exported))
    store.complete_snapshot(snapshot)
    progress(f"{len(result['groups'])} circles; report: {directory / 'report.html'}")


def _write_failure_artifacts(
    snapshot: RunRecord,
    collector: Collector,
    exported: CollectedSnapshot,
    directory: Path,
    error: BaseException,
) -> None:
    current = _report_snapshot(collector, exported)
    result = failure_report(snapshot, current, str(error) or "Interrupted; use resume")
    write_report(directory, result, current)


async def execute_online(
    store: Store,
    snapshot: RunRecord,
    directory: Path,
    *,
    password_provider: PasswordProvider,
    concurrency: int = DEFAULT_CONCURRENCY,
    progress: ProgressReporter = print,
) -> None:
    """Advance the intentionally mutable run record and publication progress."""
    message = f"Snapshot {snapshot['id']}; {'dry run' if snapshot['dry_run'] else 'publish'}"
    if progress is print:
        print(message, flush=True)
    else:
        progress(message)
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    request = RetryingRequest(timeout=45, limits=limits)
    api = Bluesky(request)
    collector = Collector(api, store, snapshot, progress=progress, concurrency=concurrency)
    exported: CollectedSnapshot | None = None
    try:
        exported = await _collect_or_export(collector, snapshot)
        context = await _load_publication_context(api, store, snapshot)
        _initialize_publication(snapshot, exported, context)
        _checkpoint_plan(store, snapshot, context.repository)
        await _publish_if_requested(api, store, snapshot, context, password_provider)
        _complete_run(store, snapshot, collector, exported, directory, progress)
    except (APIError, ValueError, KeyboardInterrupt, asyncio.CancelledError) as error:
        _write_failure_artifacts(
            snapshot,
            collector,
            exported if exported is not None else collector.export(),
            directory,
            error,
        )
        raise
    finally:
        await request.close()
