# Prism

Prism turns the accounts you follow into gemstone-named Bluesky lists. It groups people using follows and the last 90 days of replies, quotes, and reposts.

## Get started

You need Python 3.11 or newer and `uv`. On NixOS, run `nix develop` first.

```sh
uv sync --locked
uv run prism run --actor YOUR_HANDLE --dry-run
```

Open the report path that Prism prints. Add `--lists 12` if you want to aim for a different number of lists.

## Publish

Do a dry run first, then set `BSKY_APP_PASSWORD` in your environment or `.env` and run:

```sh
uv run prism run --actor YOUR_HANDLE
```

If a run stops, pick it back up with `uv run prism resume`. Saved pages and publication progress are reused.

## Recluster saved data

```sh
uv run prism recluster SNAPSHOT --lists 12
uv run prism recluster reports/SNAPSHOT/snapshot.json.gz --settings experiment.json
```

This stays offline and writes a new report.

Each online run writes a readable `result.json` with the 50 strongest connections in each group
and a compressed `snapshot.json.gz` with the full reclustering input. Prism also accepts legacy
uncompressed snapshots. Completed SQLite run records keep collection data but discard the copied
clustering result and publication plan.

## Check the code

```sh
uv run ruff check src
uv run ruff format --check src
uv run ty check src
uv build
```

Run `uv run prism --help` for the rest of the options.
