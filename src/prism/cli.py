"""Command-line entry points for Prism."""

import asyncio
import getpass
import json
import os
import sys
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

import typer
from dotenv import load_dotenv

from .api import APIError, Bluesky, RetryingRequest
from .cluster import ClusterOptions, cluster_snapshot
from .collect import DEFAULT_CONCURRENCY, Collector
from .models.collection import validate_collected_snapshot
from .models.publication import validate_managed_lists
from .report import write_report
from .storage import Store, read_gzipped_json
from .workflow import execute_online

app = typer.Typer(
    help="Prism finds social circles and publishes gemstone-named Bluesky lists.",
    context_settings={"help_option_names": ["-h", "--help"]},
    pretty_exceptions_enable=False,
)

ConcurrencyOption = Annotated[
    int, typer.Option(min=1, help="Maximum concurrent collection requests")
]
ListsOption = Annotated[
    int | None, typer.Option(min=1, help="Target list count, default 8 for new runs")
]
SettingsOption = Annotated[Path | None, typer.Option(help="JSON file with clustering settings")]
ResolutionOption = Annotated[
    list[float] | None,
    typer.Option(help="Try this resolution. Repeat the option to try several"),
]


def options_for(
    *,
    settings: Path | None,
    resolution: list[float] | None,
    lists: int | None,
    saved: dict[str, Any] | None = None,
) -> ClusterOptions:
    values = dict(saved or {})
    if settings:
        values.update(json.loads(settings.read_text(encoding="utf-8")))
    if resolution:
        values["resolutions"] = resolution
    if lists is not None:
        values.update(preferred_lists_min=lists, preferred_lists_max=lists)
    return ClusterOptions(**values)


async def resolve_actor(actor: str) -> str:
    actor = actor.strip().removeprefix("@")
    if not actor.startswith("did:"):
        request = RetryingRequest(timeout=45)
        try:
            actor = (await Bluesky(request).get_profile(actor)).did
        finally:
            await request.close()
    if not actor.startswith(("did:plc:", "did:web:")):
        raise ValueError("Use a Bluesky handle, did:plc, or did:web account")
    return actor


def app_password() -> str:
    load_dotenv(Path(".env"), override=False)
    password = os.environ.get("BSKY_APP_PASSWORD")
    if password:
        return password
    if not sys.stdin.isatty():
        raise ValueError("Set BSKY_APP_PASSWORD in the environment or .env, then run prism resume")
    password = getpass.getpass("Bluesky app password: ")
    if not password:
        raise ValueError("Publishing needs an app password. Set it, then run prism resume")
    return password


@contextmanager
def open_store(database: Path):
    store = Store(database)
    try:
        with store.locked():
            yield store
    except (APIError, ValueError, TypeError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise typer.Exit(1) from error
    except KeyboardInterrupt as error:
        print("Interrupted. Completed pages are saved. Run prism resume.", file=sys.stderr)
        raise typer.Exit(130) from error
    finally:
        store.close()


@app.command()
def run(
    actor: Annotated[str, typer.Option(help="Account handle or DID")],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Preview list changes without publishing")
    ] = False,
    lists: ListsOption = None,
    settings: SettingsOption = None,
    resolution: ResolutionOption = None,
    concurrency: ConcurrencyOption = DEFAULT_CONCURRENCY,
    database: Path = Path("data/clusters.sqlite"),
    reports: Path = Path("reports"),
):
    """Collect data and update the lists."""
    with open_store(database) as store:
        if pending := store.pending():
            raise ValueError(f"Snapshot {pending['id']} is unfinished. Run prism resume first")
        options = options_for(settings=settings, resolution=resolution, lists=lists)
        did = asyncio.run(resolve_actor(actor))
        if not dry_run and not store.has_completed_dry_run(did):
            raise ValueError(f"Complete prism run --actor {actor} --dry-run before publishing")
        snapshot = store.create_snapshot(did, dry_run=dry_run, options=asdict(options))
        asyncio.run(
            execute_online(
                store,
                snapshot,
                reports / snapshot["id"],
                password_provider=app_password,
                concurrency=concurrency,
            )
        )


@app.command()
def resume(
    concurrency: ConcurrencyOption = DEFAULT_CONCURRENCY,
    database: Path = Path("data/clusters.sqlite"),
    reports: Path = Path("reports"),
):
    """Continue an interrupted run with its saved settings and mode."""
    with open_store(database) as store:
        if not (snapshot := store.pending()):
            print("No interrupted snapshot to resume.")
            return
        asyncio.run(
            execute_online(
                store,
                snapshot,
                reports / snapshot["id"],
                password_provider=app_password,
                concurrency=concurrency,
            )
        )


@app.command()
def recluster(
    snapshot: Annotated[
        str, typer.Argument(help="Snapshot ID in SQLite, or an exported gzip-compressed JSON file")
    ],
    lists: ListsOption = None,
    settings: SettingsOption = None,
    resolution: ResolutionOption = None,
    database: Path = Path("data/clusters.sqlite"),
    reports: Path = Path("reports"),
):
    """Cluster saved data offline without publishing."""
    with open_store(database) as store:
        source = Path(snapshot)
        if source.is_file():
            exported = validate_collected_snapshot(read_gzipped_json(source), "imported snapshot")
        else:
            saved = store.snapshot(snapshot)
            exported = Collector(None, store, saved).export()
        if exported["root_coverage"]["status"] != "complete":
            raise ValueError("The account's follow list is incomplete. Run prism resume first")
        options = options_for(
            settings=settings, resolution=resolution, lists=lists, saved=exported.get("options")
        )
        previous = validate_managed_lists(exported.get("previous", []), "snapshot.previous")
        result = cluster_snapshot(exported, options, previous)
        result["publishing"] = {"status": "offline_experiment"}
        identifier = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:6]
        directory = reports / f"{exported['id']}-experiment-{identifier}"
        write_report(directory, result)
        print(f"{len(result['groups'])} circles; offline report: {directory / 'report.html'}")


if __name__ == "__main__":
    app()
