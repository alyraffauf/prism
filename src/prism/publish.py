"""Synchronize managed Bluesky lists."""

import asyncio
import base64
import copy
import hashlib
from collections import defaultdict
from collections.abc import Callable
from typing import Any, cast

from .api import APIError, Bluesky
from .descriptions import DESCRIPTION_PREFIX, MARKER
from .icons import gem_icon
from .models.clustering import ClusterResult, Group
from .models.publication import (
    ManagedList,
    PublicationIntent,
    PublicationPreparation,
    Repository,
    RepositoryRecord,
    WriteKind,
    WriteOperation,
    validate_managed_lists,
    validate_repository,
)
from .models.runs import RunRecord
from .storage import Store

LIST = "app.bsky.graph.list"
ITEM = "app.bsky.graph.listitem"
PURPOSE = "app.bsky.graph.defs#curatelist"
METADATA = "prism"
TID_ALPHABET = "234567abcdefghijklmnopqrstuvwxyz"


def record_key(identity: str) -> str:
    # A deterministic 63-bit value encoded using the protocol's 13-character TID syntax.
    value = int.from_bytes(hashlib.sha256(identity.encode()).digest()[:8], "big") & (2**63 - 1)
    return "".join(TID_ALPHABET[(value >> shift) & 31] for shift in range(60, -1, -5))


def list_uri(did: str, name: str) -> str:
    return f"at://{did}/{LIST}/{record_key(f'prism:list:{did}:{name}')}"


def item_key(uri: str, did: str) -> str:
    return record_key(f"prism:item:{uri}:{did}")


def list_url(uri: str) -> str:
    _, _, did, _, key = uri.split("/")
    return f"https://bsky.app/profile/{did}/lists/{key}"


async def repo_records(api: Bluesky, pds: str, did: str, collection: str) -> list[RepositoryRecord]:
    records: dict[str, RepositoryRecord] = {}
    cursor: str | None = None
    seen: set[str] = set()
    while True:
        if cursor:
            page = await api.repo(
                pds,
                "com.atproto.repo.listRecords",
                repo=did,
                collection=collection,
                limit=100,
                cursor=cursor,
            )
        else:
            page = await api.repo(
                pds,
                "com.atproto.repo.listRecords",
                repo=did,
                collection=collection,
                limit=100,
            )
        raw_records = page.get("records")
        if not isinstance(raw_records, list):
            raise ValueError("Invalid response.records: expected a list")
        for index, raw_record in enumerate(raw_records):
            if not isinstance(raw_record, dict):
                raise ValueError(f"Invalid response.records[{index}]: expected an object")
            uri = raw_record.get("uri")
            if not isinstance(uri, str):
                raise ValueError(f"Invalid response.records[{index}].uri: expected a string")
            if not isinstance(raw_record.get("value"), dict):
                raise ValueError(f"Invalid response.records[{index}].value: expected an object")
            records[uri] = cast(RepositoryRecord, raw_record)
        cursor = page.get("cursor")
        if not cursor:
            return list(records.values())
        if cursor in seen:
            raise APIError("The repository API repeated its pagination cursor")
        seen.add(cursor)


async def read_repository(api: Bluesky, pds: str, did: str) -> Repository:
    for _ in range(3):
        before = await api.repo(pds, "com.atproto.sync.getLatestCommit", did=did)
        lists, items = await asyncio.gather(
            repo_records(api, pds, did, LIST), repo_records(api, pds, did, ITEM)
        )
        after = await api.repo(pds, "com.atproto.sync.getLatestCommit", did=did)
        if before["cid"] == after["cid"]:
            return validate_repository(
                {
                    "commit": after["cid"],
                    "lists": {record["uri"]: record for record in lists},
                    "items": {record["uri"]: record for record in items},
                }
            )
    raise APIError("The repository kept changing while being read. Run prism resume")


def check_marker(record: RepositoryRecord) -> None:
    value = record["value"]
    if MARKER not in value.get("description", "") or value.get("purpose") != PURPOSE:
        raise APIError(f"Managed list {record['uri']} lost its tool marker or curation purpose")
    if value.get(METADATA, {}).get("uri") != record["uri"]:
        raise APIError(f"Managed list {record['uri']} has conflicting identity metadata")


def managed_lists(repository: Repository, registered: list[ManagedList]) -> list[ManagedList]:
    known: dict[str, ManagedList] = {
        item["uri"]: cast(
            ManagedList,
            {
                key: item[key]
                for key in ("uri", "name", "value", "members", "active", "last_snapshot")
            },
        )
        for item in registered
    }
    by_list = defaultdict(set)
    for record in repository["items"].values():
        by_list[record["value"]["list"]].add(record["value"]["subject"])
    for uri, record in repository["lists"].items():
        value = record["value"]
        meta = value.get(METADATA, {})
        gem = meta.get("gem")
        if uri not in known:
            continue
        check_marker(record)
        known[uri] = {
            "uri": uri,
            "name": gem,
            "members": sorted(by_list[uri]),
            "last_snapshot": meta["snapshot"],
            "active": meta["active"],
            "value": value,
        }
    names = [entry["name"] for entry in known.values()]
    if len(names) != len(set(names)):
        raise APIError("Managed list identities contain duplicate gem names")
    return validate_managed_lists(
        sorted(known.values(), key=lambda entry: entry["name"]), "managed lists"
    )


def validate_intent(intent: PublicationIntent) -> None:
    assigned = set()
    names = set()
    uris = set()
    for entry in intent["lists"]:
        uri = entry["uri"]
        if (
            not uri.startswith(f"at://{intent['actor']}/{LIST}/")
            or uri in uris
            or entry["name"] in names
        ):
            raise ValueError("Published lists need unique names and record identities")
        uris.add(uri)
        names.add(entry["name"])
        metadata = entry["value"].get(METADATA, {})
        if (
            metadata.get("uri") != uri
            or metadata.get("gem") != entry["name"]
            or metadata.get("active") != entry["active"]
        ):
            raise ValueError(f"Publication metadata disagrees with the saved list: {uri}")
        if not entry["active"]:
            continue
        members = set(entry["members"])
        if len(members) < 25 or len(members) != len(entry["members"]) or assigned & members:
            raise ValueError(
                "Published circles need at least 25 unique members and one active list per person"
            )
        assigned.update(members)


def make_inactive_entry(entry: ManagedList) -> ManagedList:
    old = entry["value"]
    value = {
        **old,
        "description": f"{DESCRIPTION_PREFIX} Inactive circle. "
        f"Last snapshot {entry['last_snapshot']}. "
        f"Membership retained. {MARKER}",
        METADATA: {
            **old.get(METADATA, {}),
            "uri": entry["uri"],
            "gem": entry["name"],
            "snapshot": entry["last_snapshot"],
            "active": False,
        },
    }
    value.pop("descriptionFacets", None)
    return {
        "uri": entry["uri"],
        "name": entry["name"],
        "active": False,
        "last_snapshot": entry["last_snapshot"],
        "members": entry["members"],
        "value": value,
    }


def make_intent(
    result: ClusterResult, previous: list[ManagedList], repository: Repository
) -> PublicationPreparation:
    if result["temporary_failures"] or result["root_coverage"]["status"] != "complete":
        raise ValueError("An incomplete snapshot cannot be published")
    former = {entry["uri"]: entry for entry in previous}
    did = result["actor"]["did"]
    entries: list[ManagedList] = []
    updated_groups: list[Group] = []
    active_uris: set[str] = set()
    for group in result["groups"]:
        uri = group.get("previous_uri") or list_uri(did, group["name"])
        if group.get("previous_uri") and uri not in former:
            raise ValueError("A continuing list is missing its stored identity")
        if not uri.startswith(f"at://{did}/{LIST}/") or uri in active_uris:
            raise ValueError("Invalid or duplicate list identity")
        published_group = cast(Group, {**group, "uri": uri, "url": list_url(uri)})
        updated_groups.append(published_group)
        active_uris.add(uri)
        # Recreated lists get a new creation date but can reuse their URI and icon.
        old = former.get(uri, {}).get("value", {}) if uri in repository["lists"] else {}
        record = {
            **old,
            "$type": LIST,
            "name": group["name"],
            "description": group["description"],
            "purpose": PURPOSE,
            "createdAt": old.get("createdAt", result["created_at"]),
            METADATA: {
                "uri": uri,
                "gem": group["name"],
                "snapshot": result["created_at"][:10],
                "active": True,
            },
        }
        record.pop("descriptionFacets", None)
        if "descriptionFacets" in group:
            record["descriptionFacets"] = group["descriptionFacets"]
        entries.append(
            {
                "uri": uri,
                "name": group["name"],
                "active": True,
                "last_snapshot": result["created_at"][:10],
                "value": record,
                "members": sorted(person["did"] for person in group["members"]),
            }
        )
    for entry in previous:
        if entry["uri"] in active_uris:
            continue
        entries.append(make_inactive_entry(entry))
    for entry in entries:
        entry["icon"] = gem_icon(entry["name"])
        old_value = former.get(entry["uri"], {}).get("value", {})
        old_metadata = old_value.get(METADATA, {})
        metadata = entry["value"][METADATA]
        metadata["icon_sha256"] = entry["icon"]["sha256"]
        old_avatar = old_value.get("avatar", {})
        if (
            old_metadata.get("icon_sha256") == entry["icon"]["sha256"]
            and old_metadata.get("icon_cid")
            and old_avatar.get("ref", {}).get("$link") == old_metadata["icon_cid"]
        ):
            entry["value"]["avatar"] = old_avatar
            metadata["icon_cid"] = old_metadata["icon_cid"]
        else:
            entry["value"].pop("avatar", None)
            metadata.pop("icon_cid", None)
    intent: PublicationIntent = {
        "actor": did,
        "created_at": result["created_at"],
        "lists": entries,
        "status": "pending",
        "batches": 0,
        "writes": 0,
    }
    validate_intent(intent)
    updated_result = cast(ClusterResult, {**result, "groups": updated_groups})
    return PublicationPreparation(result=updated_result, intent=intent)


def operation(
    kind: WriteKind, collection: str, uri: str, value: dict[str, Any] | None = None
) -> WriteOperation:
    write: WriteOperation = {
        "$type": f"com.atproto.repo.applyWrites#{kind}",
        "collection": collection,
        "rkey": uri.rsplit("/", 1)[-1],
    }
    if value is not None:
        write["value"] = value
    return write


def plan_changes(intent: PublicationIntent, repository: Repository) -> list[WriteOperation]:
    validate_intent(intent)
    deletes: list[WriteOperation] = []
    records: list[WriteOperation] = []
    creates: list[WriteOperation] = []
    by_list = defaultdict(list)
    for item in repository["items"].values():
        by_list[item["value"]["list"]].append(item)
    for entry in intent["lists"]:
        uri = entry["uri"]
        existing = repository["lists"].get(uri)
        if existing:
            check_marker(existing)
            meta = existing["value"].get(METADATA, {})
            if meta.get("gem") != entry["name"]:
                raise APIError(f"List identity collision at {uri}")
        if not entry["active"] and not existing:
            continue
        if not existing or existing["value"] != entry["value"]:
            records.append(operation("update" if existing else "create", LIST, uri, entry["value"]))
        if not entry["active"]:
            continue
        wanted = set(entry["members"])
        current = defaultdict(list)
        for item in by_list[uri]:
            current[item["value"]["subject"]].append(item)
        for member, items in current.items():
            for item in (
                sorted(items, key=lambda item: item["uri"])[1:] if member in wanted else items
            ):
                deletes.append(operation("delete", ITEM, item["uri"]))
        for member in sorted(wanted - current.keys()):
            key = item_key(uri, member)
            item_uri = f"at://{intent['actor']}/{ITEM}/{key}"
            if item_uri in repository["items"]:
                raise APIError(f"List item key is occupied by an unrelated record: {item_uri}")
            value = {
                "$type": ITEM,
                "list": uri,
                "subject": member,
                "createdAt": intent["created_at"],
            }
            creates.append(operation("create", ITEM, item_uri, value))
    # Remove old memberships and mark retired lists inactive before adding members.
    return deletes + records + creates


async def upload_batch_icons(
    api: Bluesky,
    pds: str,
    batch: list[WriteOperation],
    intent: PublicationIntent,
    repository: Repository,
) -> tuple[PublicationIntent, list[WriteOperation]]:
    updated_intent = copy.deepcopy(intent)
    updated_batch = copy.deepcopy(batch)
    entries = {entry["uri"].rsplit("/", 1)[-1]: entry for entry in updated_intent["lists"]}
    for write in updated_batch:
        if write["collection"] != LIST or "value" not in write:
            continue
        entry = entries[write["rkey"]]
        if "icon" not in entry:
            continue  # Pending plans from before icons were supported retain their intention.
        existing = repository["lists"].get(entry["uri"], {}).get("value", {})
        avatar = entry["value"].get("avatar")
        if avatar and existing.get("avatar") == avatar:
            continue
        # The server can expire unused uploads. Upload again if the list does not
        # already reference the saved icon, including when resuming.
        contents = base64.b64decode(entry["icon"]["png"], validate=True)
        response = await api.repo(pds, "com.atproto.repo.uploadBlob", blob=contents)
        blob = response.get("blob", {})
        if (
            blob.get("$type") != "blob"
            or blob.get("mimeType") != "image/png"
            or blob.get("size") != len(contents)
            or not blob.get("ref", {}).get("$link")
        ):
            raise APIError("The server returned an invalid PNG blob reference")
        entry["value"]["avatar"] = blob
        entry["value"][METADATA]["icon_cid"] = blob["ref"]["$link"]
        write["value"] = copy.deepcopy(entry["value"])
    return updated_intent, updated_batch


async def synchronize(
    api: Bluesky,
    store: Store,
    snapshot: RunRecord,
    pds: str,
    progress: Callable[[str], None] = print,
) -> None:
    """Update the intentionally mutable run record as publication advances."""
    intent = snapshot["publication"]
    did = intent["actor"]
    retries = 0
    previous_changes = None
    stalled = 0
    while True:
        repository = await read_repository(api, pds, did)
        writes = plan_changes(intent, repository)
        if not writes:
            intent.update(status="complete", error=None)
            store.register_lists(did, intent["lists"])
            store.save_snapshot(snapshot)
            return
        signature = tuple((write["$type"], write["collection"], write["rkey"]) for write in writes)
        stalled = stalled + 1 if signature == previous_changes else 0
        if stalled >= 3:
            raise APIError("Publication made no progress after repeated attempts. Run prism resume")
        previous_changes = signature
        restart = False
        for offset in range(0, len(writes), 50):
            batch = writes[offset : offset + 50]
            try:
                intent, batch = await upload_batch_icons(api, pds, batch, intent, repository)
                snapshot["publication"] = intent
                store.save_snapshot(snapshot)
                response = await api.repo(
                    pds,
                    "com.atproto.repo.applyWrites",
                    data={
                        "repo": did,
                        "validate": True,
                        "swapCommit": repository["commit"],
                        "writes": batch,
                    },
                )
            except APIError as error:
                intent["error"] = str(error)
                store.save_snapshot(snapshot)
                if error.code == "InvalidSwap" or error.status >= 500 or error.status == 0:
                    retries += 1
                    if retries <= 3:
                        # The server may have applied the batch. Read before retrying.
                        restart = True
                        break
                raise
            intent.update(
                batches=intent["batches"] + 1, writes=intent["writes"] + len(batch), error=None
            )
            store.save_snapshot(snapshot)
            progress(f"Synced {intent['writes']} changes in {intent['batches']} batches")
            commit = response.get("commit", {}).get("cid")
            if not commit:
                restart = True
                break
            repository["commit"] = commit
        if not restart:
            # Read once more to verify final memberships, markers, and inactive descriptions.
            retries = 0
