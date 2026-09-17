"""List descriptions based on posting activity."""

from typing import cast

from .models.clustering import GroupDescription
from .models.collection import Person

DESCRIPTION_PREFIX = "Automated curation list."
MARKER = "Managed by Prism."


def describe_group(members: list[Person], created_at: str, days: int) -> GroupDescription:
    def activity_count(member: Person) -> int:
        count = member.get("authored_post_count")
        return count if isinstance(count, int) else -1

    suffix = f"{DESCRIPTION_PREFIX} Snapshot {created_at[:10]}. {MARKER}"
    candidates = sorted(
        (
            member
            for member in members
            if activity_count(member) > 0
            and member.get("handle")
            and member["handle"] != "handle.invalid"
        ),
        key=lambda member: (-activity_count(member), member["did"]),
    )
    description = f"Active posters ({days} days): "
    facets = []
    for member in candidates:
        mention = f"@{member['handle']}"
        separator = ", " if facets else ""
        proposed = description + separator + mention
        # Keep complete handles and leave room for the date and ownership marker.
        if len(proposed + ". " + suffix) > 300:
            continue
        start = len((description + separator).encode("utf-8"))
        facets.append(
            {
                "index": {"byteStart": start, "byteEnd": start + len(mention.encode("utf-8"))},
                "features": [{"$type": "app.bsky.richtext.facet#mention", "did": member["did"]}],
            }
        )
        description = proposed
        if len(facets) == 3:
            break
    if not facets:
        return {"description": suffix}
    return cast(
        GroupDescription,
        {"description": description + ". " + suffix, "descriptionFacets": facets},
    )
