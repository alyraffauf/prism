"""Collect follows and 90 days of interactions."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

from .api import APIError, Bluesky
from .models.collection import (
    AccountCoverage,
    CollectedSnapshot,
    CollectionKind,
    CoverageState,
    Event,
    FeedPage,
    PeoplePage,
    Person,
    TaskState,
    validate_page,
    validate_person,
)
from .models.runs import RunRecord, validate_run_record
from .storage import Store

DEFAULT_CONCURRENCY = 32


class IncompleteSnapshot(APIError):
    pass


def timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def uri_author(uri: str | None) -> str | None:
    if isinstance(uri, str) and uri.startswith("at://did:"):
        return uri.split("/")[2]
    return None


def activity_time(item: dict[str, Any]) -> datetime | None:
    reason = item.get("reason", {})
    if reason.get("$type") == "app.bsky.feed.defs#reasonRepost":
        return timestamp(reason.get("indexedAt"))
    return timestamp(item.get("post", {}).get("record", {}).get("createdAt"))


def ordering_time(item: dict[str, Any]) -> datetime | None:
    reason = item.get("reason", {})
    if reason.get("$type") == "app.bsky.feed.defs#reasonRepost":
        return timestamp(reason.get("indexedAt"))
    # A backdated post can be indexed recently. Use feed order to decide when
    # to stop, or later pages with activity inside the window could be skipped.
    return timestamp(item.get("post", {}).get("indexedAt")) or activity_time(item)


def event_key(event: Event) -> tuple[str, str, str, str, str]:
    return (
        event["source"],
        event["target"],
        event["kind"],
        event["uri"],
        event["time"] if event["kind"] == "repost" else "",
    )


def interactions(item: dict[str, Any], actor: str) -> list[Event]:
    post = item.get("post", {})
    record = post.get("record", {})
    reason = item.get("reason", {})
    when = activity_time(item)
    uri = post.get("uri")
    if when is None or not isinstance(uri, str) or not uri:
        return []
    event_time = when.isoformat()
    if reason.get("$type") == "app.bsky.feed.defs#reasonRepost":
        if reason.get("by", {}).get("did") != actor:
            return []
        target = post.get("author", {}).get("did")
        return (
            [
                {
                    "source": actor,
                    "target": target,
                    "kind": "repost",
                    "time": event_time,
                    "uri": uri,
                }
            ]
            if target and target != actor
            else []
        )
    if post.get("author", {}).get("did") != actor:
        return []
    events: list[Event] = []
    parent = record.get("reply", {}).get("parent", {}).get("uri")
    if (target := uri_author(parent)) and target != actor:
        events.append(
            {"source": actor, "target": target, "kind": "reply", "time": event_time, "uri": uri}
        )
    embed = record.get("embed", {})
    if embed.get("$type") == "app.bsky.embed.recordWithMedia":
        embed = embed.get("record", {})
    if embed.get("$type") == "app.bsky.embed.record":
        quoted = embed.get("record", {}).get("uri", "")
        target = uri_author(quoted)
        if "/app.bsky.feed.post/" in quoted and target and target != actor:
            events.append(
                {
                    "source": actor,
                    "target": target,
                    "kind": "quote",
                    "time": event_time,
                    "uri": uri,
                }
            )
    return events


def profile_fields(person: object) -> Person:
    if not isinstance(person, dict) or not isinstance(person.get("did"), str):
        raise APIError("The API returned a profile without an account DID")
    profile = {
        key: person.get(key)
        for key in ("did", "handle", "displayName", "followsCount", "followersCount", "postsCount")
    }
    return validate_person(profile, "response.profile")


class Collector:
    def __init__(
        self,
        api: Bluesky | None,
        store: Store,
        snapshot: RunRecord,
        progress: Callable[[str], None] = print,
        *,
        concurrency: int = DEFAULT_CONCURRENCY,
    ):
        if type(concurrency) is not int or concurrency < 1:
            raise ValueError("Concurrency must be a positive integer")
        self.api = api
        self.store = store
        self.snapshot = validate_run_record(snapshot)
        self.identifier = snapshot["id"]
        start = timestamp(snapshot["cutoff"])
        end = timestamp(snapshot["created_at"])
        if start is None:
            raise ValueError("Invalid run.cutoff: expected an ISO timestamp with a time zone")
        if end is None:
            raise ValueError("Invalid run.created_at: expected an ISO timestamp with a time zone")
        self.start: datetime = start
        self.end: datetime = end
        self.progress = progress
        self.concurrency = concurrency
        self.members: set[str] = set()
        self.last_progress = 0.0

    async def fetch_profile(self, actor: str) -> TaskState:
        state = self.store.task(self.identifier, actor, "profile")
        if state["status"] == "complete" or (
            state["status"] == "unavailable" and actor != self.snapshot["actor"]["did"]
        ):
            return state
        try:
            if self.api is None:
                raise ValueError("Collection requires a Bluesky client")
            profile = await self.api.get("app.bsky.actor.getProfile", actor=actor)
            if profile.get("did") != actor:
                raise APIError("The API returned a profile for a different account")
            state.update(status="complete", profile=profile_fields(profile), error=None)
        except APIError as error:
            state.update(status="unavailable" if error.unavailable else "failed", error=str(error))
        self.store.save_task(self.identifier, actor, "profile", state)
        return state

    def feed_page(self, page: dict[str, Any], actor: str) -> FeedPage:
        raw_items = page.get("feed")
        if not isinstance(raw_items, list):
            raise ValueError("Invalid response.feed: expected a list")
        if not all(isinstance(item, dict) for item in raw_items):
            raise ValueError("Invalid response.feed: expected objects")
        items = [
            item
            for item in raw_items
            if item.get("reason", {}).get("$type") != "app.bsky.feed.defs#reasonPin"
        ]
        events: dict[tuple[str, str, str, str, str], Event] = {}
        authored_posts = set()
        invalid = 0
        times = []
        for item in items:
            when = activity_time(item)
            times.append(ordering_time(item))
            if when is None:
                invalid += 1
            elif self.start <= when <= self.end:
                post = item.get("post", {})
                if (
                    item.get("reason", {}).get("$type") != "app.bsky.feed.defs#reasonRepost"
                    and post.get("author", {}).get("did") == actor
                    and post.get("uri")
                ):
                    authored_posts.add(post["uri"])
                for event in interactions(item, actor):
                    if event["target"] in self.members:
                        events[event_key(event)] = event
        before_window = bool(times) and all(when and when < self.start for when in times)
        saved: FeedPage = {
            "events": list(events.values()),
            "authored_posts": sorted(authored_posts),
            "invalid_timestamps": invalid,
            "before_window": before_window,
        }
        return saved

    async def fetch_pages(self, actor: str, kind: CollectionKind) -> None:
        state = self.store.task(self.identifier, actor, kind)
        if state["status"] == "complete" or (
            state["status"] == "unavailable" and kind != "root_follows"
        ):
            return
        state.update(status="pending", error=None)
        restarted = False
        while True:
            params = {"actor": actor, "limit": 100}
            if state["cursor"]:
                params["cursor"] = state["cursor"]
            method = "app.bsky.graph.getFollows"
            if kind == "feed":
                method = "app.bsky.feed.getAuthorFeed"
                params.update(filter="posts_with_replies", includePins=False)
            try:
                if self.api is None:
                    raise ValueError("Collection requires a Bluesky client")
                page = await self.api.get(method, **params)
                if kind == "feed":
                    saved = self.feed_page(page, actor)
                else:
                    raw_people = page.get("follows")
                    if not isinstance(raw_people, list):
                        raise ValueError("Invalid response.follows: expected a list")
                    people = [profile_fields(person) for person in raw_people]
                    if kind == "follows":
                        people = [{"did": person["did"]} for person in people]
                    saved = cast(
                        PeoplePage,
                        {"people": people},
                    )
                validate_page(saved, kind, "collected_page")
                cursor = page.get("cursor")
                done = not cursor or saved.get("before_window", False)
                if cursor and cursor in state["cursors"] and not done:
                    raise APIError(f"Repeated {kind} pagination cursor for {actor}")
                state.update(
                    cursor=cursor,
                    pages=state["pages"] + 1,
                    status="complete" if done else "pending",
                    error=None,
                )
                if cursor:
                    state["cursors"].append(cursor)
                self.store.save_page(self.identifier, actor, kind, state, saved)
                now = asyncio.get_running_loop().time()
                if now - self.last_progress >= 20:
                    self.progress(f"Collecting {kind}: {actor}, page {state['pages']}")
                    self.last_progress = now
                if done:
                    return
            except APIError as error:
                if error.invalid_cursor and state["cursor"] and not restarted:
                    self.store.restart_task(self.identifier, actor, kind, state)
                    restarted = True
                    continue
                state.update(
                    status="unavailable" if error.unavailable else "failed", error=str(error)
                )
                self.store.save_task(self.identifier, actor, kind, state)
                return
            except (KeyError, TypeError, AttributeError) as error:
                state.update(
                    status="failed", error=f"Malformed {kind} page: {type(error).__name__}"
                )
                self.store.save_task(self.identifier, actor, kind, state)
                return

    async def collect(self) -> CollectedSnapshot:
        actor = self.snapshot["actor"]["did"]
        profile = await self.fetch_profile(actor)
        if profile["status"] != "complete":
            raise IncompleteSnapshot("Could not fetch the account profile. Run prism resume")
        self.snapshot["actor"] = profile["profile"]
        self.store.save_snapshot(self.snapshot)
        await self.fetch_pages(actor, "root_follows")
        if self.store.task(self.identifier, actor, "root_follows")["status"] != "complete":
            raise IncompleteSnapshot("The account's follow list is incomplete. Run prism resume")
        people = self.followed_people(actor, "root_follows")
        self.members = set(people)
        self.progress(
            f"Found {len(people)} followed accounts. The profile reports "
            f"{profile['profile'].get('followsCount')}"
        )
        self.progress(f"Collecting with up to {self.concurrency} concurrent requests")
        queue: asyncio.Queue[tuple[str, CollectionKind]] = asyncio.Queue()
        remaining = dict.fromkeys(people, 3)
        for did in sorted(people):
            for kind in ("profile", "follows", "feed"):
                queue.put_nowait((did, kind))
        finished = 0

        async def reader():
            nonlocal finished
            while not queue.empty():
                did, kind = queue.get_nowait()
                if kind == "profile":
                    await self.fetch_profile(did)
                else:
                    await self.fetch_pages(did, kind)
                remaining[did] -= 1
                if remaining[did] == 0:
                    finished += 1
                    if finished % 20 == 0 or finished == len(people):
                        self.progress(f"Collected {finished}/{len(people)} accounts")

        await asyncio.gather(*(reader() for _ in range(self.concurrency)))
        snapshot = self.export()
        if snapshot["temporary_failures"]:
            raise IncompleteSnapshot(
                f"{snapshot['temporary_failures']} requests remain unresolved. "
                "Run prism resume to finish collection before publishing"
            )
        self.snapshot["stage"] = "collected"
        self.store.save_snapshot(self.snapshot)
        return snapshot

    def followed_people(self, actor: str, kind: CollectionKind) -> dict[str, Person]:
        return {
            person["did"]: person
            for page in self.store.pages(self.identifier, actor, kind)
            for person in cast(PeoplePage, page)["people"]
        }

    def coverage(self, actor: str, kind: CollectionKind) -> CoverageState:
        state = self.store.task(self.identifier, actor, kind)
        return {
            "status": state["status"],
            "error": state.get("error"),
            "pages": state["pages"],
            "restarts": state["restarts"],
        }

    def export_metadata(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in self.snapshot.items()
            if key not in {"result", "publication"}
        }

    def export(self) -> CollectedSnapshot:
        actor = self.snapshot["actor"]["did"]
        people = self.followed_people(actor, "root_follows")
        members = set(people)
        follows = set()
        events: dict[tuple[str, str, str, str, str], Event] = {}
        coverage: list[AccountCoverage] = []
        root = self.coverage(actor, "root_follows")
        failures = int(root["status"] != "complete")
        for did, person in sorted(people.items()):
            profile = self.store.task(self.identifier, did, "profile")
            if "profile" in profile:
                person.update(profile["profile"])
            followed = self.followed_people(did, "follows")
            follows.update(
                (did, target) for target in followed if target in members and target != did
            )
            invalid = 0
            authored_posts = set()
            activity_available = (
                self.store.task(self.identifier, did, "feed")["status"] == "complete"
            )
            for page in self.store.pages(self.identifier, did, "feed"):
                page = cast(FeedPage, page)
                invalid += page["invalid_timestamps"]
                activity_available = activity_available and "authored_posts" in page
                authored_posts.update(page.get("authored_posts", []))
                for event in page["events"]:
                    events[event_key(event)] = event
            person["authored_post_count"] = (
                len(authored_posts) if activity_available and not invalid else None
            )
            profile_coverage = self.coverage(did, "profile")
            follows_coverage = self.coverage(did, "follows")
            feed_coverage = self.coverage(did, "feed")
            states = (profile_coverage, follows_coverage, feed_coverage)
            failures += sum(state["status"] not in {"complete", "unavailable"} for state in states)
            expected = person.get("followsCount")
            coverage.append(
                {
                    "did": did,
                    "handle": person.get("handle"),
                    "profile": profile_coverage,
                    "follows": follows_coverage,
                    "feed": feed_coverage,
                    "reported_follows": expected,
                    "fetched_follows": len(followed),
                    "follow_count_difference": expected - len(followed)
                    if expected is not None
                    else None,
                    "invalid_timestamps": invalid,
                }
            )
        reported = self.snapshot["actor"].get("followsCount")
        return cast(
            CollectedSnapshot,
            {
                **self.export_metadata(),
                "people": list(people.values()),
                "follows": [list(pair) for pair in sorted(follows)],
                "events": sorted(events.values(), key=event_key),
                "coverage": coverage,
                "root_coverage": root,
                "reported_follows": reported,
                "fetched_follows": len(people),
                "follow_count_difference": reported - len(people) if reported is not None else None,
                "temporary_failures": failures,
            },
        )
