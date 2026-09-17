# Prism

Prism uses the Leiden algorithm to discover community clusters within your Bluesky follows, then creates curation lists for each.

Communities are discovered by weighing follows (one-way and mutual), plus the last 90 days of replies, quotes, and reposts. It's a good way to keep track of subsets of the accounts you follow when algorithmic feeds drown them out.

## Get started

You need Python 3.11 or newer and `uv`. On NixOS, run `nix develop` first.

```sh
uv sync --locked
uv run prism run --actor YOUR_HANDLE --dry-run
```

Open the report path that Prism prints. Add `--lists 12` if you want to aim for a different number of lists.

## Publish

Do a dry run first. Prism saves that plan. Set `BSKY_APP_PASSWORD` in your environment or `.env`,
then run:

```sh
uv run prism run --actor YOUR_HANDLE
```

Prism publishes the saved dry-run plan without collecting again. To change the lists or clustering settings,
run another dry run. If publication stops, pick it back up with `uv run prism resume`.

## Recluster saved data

```sh
uv run prism recluster SNAPSHOT --lists 12
uv run prism recluster reports/SNAPSHOT/snapshot.json.gz --settings experiment.json
```

This stays offline and writes a new report.

Each online run writes a readable `result.json` with the 50 strongest connections in each group
and a compressed `snapshot.json.gz` with the full reclustering input. Completed dry runs keep their
clustering result and publication plan until Prism publishes them. Completed publish runs keep collection data.

## Check the code

```sh
uv run ruff check src
uv run ruff format --check src
uv run ty check src
uv build
```

Run `uv run prism --help` for the rest of the options.
