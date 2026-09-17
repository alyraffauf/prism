"""Write HTML and JSON reports."""

import copy
from html import escape
from pathlib import Path
from typing import cast

from .models.clustering import ClusterResult, PublicationSummary
from .models.collection import CollectedSnapshot, Person
from .models.runs import RunRecord
from .storage import write_json

DOCUMENT_STYLE = "".join(
    (
        "body{font:16px/1.55 system-ui,sans-serif;max-width:1100px;margin:auto;padding:24px;",
        "color:#17213a;background:#fafbff}a{color:#174bab}small{color:#4d5870}",
        "table{border-collapse:collapse;width:100%;display:block;overflow-x:auto}",
        "th,td{text-align:left;padding:8px;border-bottom:1px solid #d3daea;vertical-align:top}",
        "section{margin:28px 0;padding:20px;background:white;border:1px solid #d3daea;",
        "border-radius:12px}summary{cursor:pointer;font-weight:600}li{margin:5px 0}",
        ".members{columns:3;column-width:250px}code{overflow-wrap:anywhere}",
        ".list-icon{display:inline-block;width:36px;height:36px;vertical-align:middle;",
        "margin-right:12px;border:1px solid #d3daea}",
    )
)


def render_header(result: ClusterResult) -> list[str]:
    return [
        "<!doctype html><html lang='en'><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>Prism social circles</title><style>{DOCUMENT_STYLE}</style><body>",
        "<h1>Prism social circles</h1>",
        f"<p>Snapshot <code>{html(result['snapshot_id'])}</code>. "
        f"Interactions from {html(result.get('cutoff'))} through {html(result['created_at'])}.</p>",
        f"<p>{len(result.get('groups', []))} active circles; "
        f"{len(result.get('unassigned', []))} unassigned accounts. "
        f"Resolution {html(result.get('chosen_resolution'))}; "
        f"seed {html(result.get('chosen_seed'))}.</p>",
        f"<p>Publication: <strong>{html(result['publishing']['status'])}</strong>. "
        f"{html(result['publishing'].get('error') or '')}</p>",
        f"<p>Planned writes: {html(result['publishing'].get('planned_writes'))}. "
        f"Confirmed writes: {html(result['publishing'].get('writes', 0))}. "
        f"Confirmed batches: {html(result['publishing'].get('batches', 0))}.</p>",
        "<nav><a href='#coverage'>Coverage</a> · <a href='#unassigned'>Unassigned accounts</a> · "
        "<a href='#settings'>Scoring and settings</a></nav>",
    ]


def publication_summary(snapshot: RunRecord) -> PublicationSummary:
    intent = snapshot["publication"]
    return {
        "status": "dry_run" if snapshot["dry_run"] else intent["status"],
        "batches": intent["batches"],
        "writes": intent["writes"],
        "planned_writes": intent.get("planned_writes"),
        "error": intent.get("error"),
        "active_lists": [
            {key: entry[key] for key in ("name", "uri", "members")}
            for entry in intent["lists"]
            if entry["active"]
        ],
        "inactive_lists": [
            {key: entry[key] for key in ("name", "uri", "members", "last_snapshot")}
            for entry in intent["lists"]
            if not entry["active"]
        ],
    }


def failure_report(snapshot: RunRecord, exported: CollectedSnapshot, error: str) -> ClusterResult:
    existing = snapshot.get("result")
    result = (
        copy.deepcopy(existing)
        if existing
        else cast(
            ClusterResult,
            {
                **{
                    key: value
                    for key, value in exported.items()
                    if key not in {"events", "follows", "people"}
                },
                "snapshot_id": snapshot["id"],
                "groups": [],
                "unassigned": exported["people"],
            },
        )
    )
    result["publishing"] = {"status": "interrupted", "error": error}
    if snapshot.get("publication"):
        result["publishing"].update(publication_summary(snapshot))
        result["publishing"].update(status="interrupted", error=error)
    return result


def html(value: object) -> str:
    return escape(str(value if value is not None else "unknown"), quote=True)


def person_link(person: Person) -> str:
    did = person["did"]
    name = person.get("displayName") or person.get("handle") or did
    return (
        f'<a href="https://bsky.app/profile/{html(did)}">{html(name)}</a> '
        f"<small>{html(person.get('handle') or did)}</small>"
    )


def render_report(result: ClusterResult) -> str:
    sections = render_header(result)
    for group in result.get("groups", []):
        icon = (
            f"<span class='list-icon' role='img' "
            f"aria-label='{html(group['name'])} icon: {html(group['icon_color'])}' "
            f"style='background-color:{html(group['icon_color'])}'></span>"
            if group.get("icon_color")
            else ""
        )
        sections.append(
            f"<section><h2>{icon}{html(group['name'])} · {len(group['members'])} members</h2>"
        )
        if group.get("description"):
            sections.append(f"<p>{html(group['description'])}</p>")
        if group.get("url"):
            sections.append(f"<p><a href='{html(group['url'])}'>Bluesky list</a></p>")
        sections.append(f"<p>Overlap with previous membership: {group.get('overlap', 0):.0%}.</p>")
        sections.append("<details open><summary>Members</summary><ul class='members'>")
        sections.extend(f"<li>{person_link(person)}</li>" for person in group["members"])
        sections.append("</ul></details><details><summary>Strongest connections</summary>")
        sections.append(
            "<table><tr><th>Accounts</th><th>Follows</th><th>Interactions</th>"
            "<th>Combined weight</th></tr>"
        )
        members = {person["did"]: person for person in group["members"]}
        for edge in group["strongest_connections"]:
            directions = len(edge["follow_directions"])
            follow = {0: "None", 1: "One-way", 2: "Mutual"}[directions]
            counts = (
                ", ".join(f"{kind}: {count}" for kind, count in edge["counts"].items()) or "None"
            )
            weeks = len({row["week"] for row in edge["weeks"]})
            sections.append(
                f"<tr><td>{person_link(members[edge['source']])}<br>"
                f"{person_link(members[edge['target']])}</td><td>{follow}; "
                f"score {edge['follow_weight']:g}</td><td>{html(counts)}; {weeks} weeks; "
                f"score {edge['interaction_weight']:g}; reciprocal replies: "
                f"{html(edge['reciprocal_replies'])}</td><td>{edge['weight']:.6f}</td></tr>"
            )
        sections.append(
            "</table><p>The compressed snapshot contains the full follow and interaction "
            "data used to calculate these connections.</p></details></section>"
        )
    inactive = result.get("publishing", {}).get("inactive_lists", [])
    if inactive:
        sections.append("<section><h2>Inactive lists</h2><ul>")
        for entry in inactive:
            sections.append(
                f"<li>{html(entry['name'])}: last snapshot "
                f"{html(entry['last_snapshot'])}; {len(entry['members'])} members.</li>"
            )
        sections.append("</ul></section>")
    sections.extend(
        [
            "<section id='coverage'><h2>Coverage</h2>",
            f"<p>The profile reports {html(result.get('reported_follows'))} follows. "
            f"The API returned {html(result.get('fetched_follows'))} distinct accounts. "
            f"Difference: {html(result.get('follow_count_difference'))}. "
            "Counts and account visibility can change during collection.</p>",
            f"<p>Unresolved requests: {html(result.get('temporary_failures'))}. "
            "Unavailable data can leave connections out of these scores.</p>",
            "<details><summary>Per-account coverage</summary><table><tr><th>Account</th>"
            "<th>Profile</th><th>Follows</th><th>Feed</th><th>Reported / returned follows</th>"
            "<th>Invalid timestamps</th></tr>",
        ]
    )
    for row in result.get("coverage", []):
        statuses = "".join(
            f"<td>{html(row[kind]['status'])} {html(row[kind].get('error') or '')}</td>"
            for kind in ("profile", "follows", "feed")
        )
        sections.append(
            f"<tr><td>{person_link(row)}</td>{statuses}<td>"
            f"{html(row['reported_follows'])} / {row['fetched_follows']}</td>"
            f"<td>{row['invalid_timestamps']}</td></tr>"
        )
    sections.append(
        "</table></details></section><section id='unassigned'><h2>Unassigned accounts</h2>"
        "<p>These accounts are in groups below the minimum size."
        "</p><ul class='members'>"
    )
    sections.extend(f"<li>{person_link(person)}</li>" for person in result.get("unassigned", []))
    sections.append(
        "</ul></section><section id='settings'><h2>Scoring and settings</h2>"
        "<p>Weekly counts use ISO calendar weeks in UTC. "
        "Both directions share the cap for each interaction type. "
        "Prism scales the follow and interaction graphs to a total weight of 1 each. "
        "Leiden clustering accounts for how many connections each account has. "
        "Quality scores are compared only at the same resolution.</p>"
        "<table><tr><th>Setting</th><th>Value</th></tr>"
    )
    for key, value in result.get("options", {}).items():
        sections.append(f"<tr><td>{html(key)}</td><td>{html(value)}</td></tr>")
    sections.append("</table></section></body></html>")
    return "\n".join(sections)


def write_report(
    directory: Path,
    result: ClusterResult,
    snapshot: object | None = None,
) -> None:
    write_json(directory / "result.json", result)
    if snapshot is not None:
        write_json(directory / "snapshot.json.gz", snapshot, compress=True)
    temporary = directory / "report.html.tmp"
    temporary.write_text(render_report(result), encoding="utf-8")
    temporary.replace(directory / "report.html")
