#!/usr/bin/env python3
"""Tag Herdr workspaces ("spaces") and group them by tag.

Herdr renders a workspace's custom `$tag` metadata token in the Space sidebar
rows. This plugin keeps that token in sync with a small tags file and groups
tagged spaces by reordering workspaces through the `workspace.move_block`
socket method, which has no CLI wrapper.

Commands are invoked by the Herdr plugin manifest; they can also be run by hand:

    python3 space_tags.py set --workspace w1 --tag work
    python3 space_tags.py apply
    python3 space_tags.py regroup
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

PLUGIN_ID = "herdr-space-tags"
SOURCE = "plugin:" + PLUGIN_ID
TOKEN = "tag"
MAX_TAG_LENGTH = 24


class SpaceTagsError(RuntimeError):
    """Any failure that should end the command with a message on stderr."""


class TagRecord(NamedTuple):
    workspace_id: str
    tag: str
    cwd: str
    label: str


# --------------------------------------------------------------------- Herdr


def herdr_bin() -> str:
    return os.environ.get("HERDR_BIN_PATH") or "herdr"


def _run_herdr(args: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [herdr_bin(), *args], capture_output=True, text=True
    )


def herdr(*args: str) -> dict:
    """Run a Herdr CLI command that answers with a JSON envelope."""
    proc = _run_herdr(list(args))
    if proc.returncode != 0:
        raise SpaceTagsError(
            "herdr {} failed: {}".format(
                " ".join(args), (proc.stderr or proc.stdout).strip()
            )
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise SpaceTagsError(
            "herdr {} returned no JSON: {}".format(" ".join(args), exc)
        ) from exc


def herdr_ok(*args: str) -> None:
    """Run a Herdr CLI command that does not answer with JSON."""
    proc = _run_herdr(list(args))
    if proc.returncode != 0:
        raise SpaceTagsError(
            "herdr {} failed: {}".format(
                " ".join(args), (proc.stderr or proc.stdout).strip()
            )
        )


def socket_request(method: str, params: dict) -> dict:
    """Send one raw socket request, the only transport for workspace.move_block."""
    request = {"id": "space-tags:{}".format(os.getpid()), "method": method, "params": params}
    if os.environ.get("SPACE_TAGS_DRY_RUN") == "1":
        print(json.dumps({"dry_run": True, **request}, sort_keys=True))
        return {}
    socket_path = os.environ.get("HERDR_SOCKET_PATH")
    if not socket_path:
        raise SpaceTagsError("HERDR_SOCKET_PATH is not set")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(socket_path)
        sock.sendall(json.dumps(request).encode() + b"\n")
        buffer = b""
        while not buffer.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            buffer += chunk
    if not buffer.strip():
        raise SpaceTagsError("{}: no response from the Herdr server".format(method))
    response = json.loads(buffer.decode())
    if "error" in response:
        error = response["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise SpaceTagsError("{}: {}".format(method, message or "request failed"))
    return response.get("result", {})


def snapshot_state() -> Tuple[List[dict], Dict[str, str]]:
    """The ordered workspaces plus each workspace's tracked cwd."""
    result = herdr("api", "snapshot")
    snapshot = result.get("result", {}).get("snapshot", {})
    workspaces = [
        ws for ws in snapshot.get("workspaces", []) if ws.get("workspace_id")
    ]
    cwds: Dict[str, str] = {}
    for pane in snapshot.get("panes", []):
        workspace_id = pane.get("workspace_id")
        cwd = pane.get("cwd")
        if workspace_id and cwd and workspace_id not in cwds:
            cwds[workspace_id] = cwd
    return workspaces, cwds


def report_args(workspace_id: str, tag: str) -> List[str]:
    args = ["workspace", "report-metadata", workspace_id, "--source", SOURCE]
    if tag:
        args += ["--token", "{}={}".format(TOKEN, tag)]
    else:
        args += ["--clear-token", TOKEN]
    return args


# ------------------------------------------------------------------- mapping


def config_dir() -> Path:
    value = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if not value:
        raise SpaceTagsError(
            "HERDR_PLUGIN_CONFIG_DIR is not set; run this through a Herdr plugin hook"
        )
    return Path(value)


def mapping_path() -> Path:
    return config_dir() / "tags.tsv"


def order_path() -> Path:
    return config_dir() / "order.txt"


def sanitize(value: str) -> str:
    return " ".join(value.replace("\t", " ").split())


def validate_tag(value: str) -> str:
    tag = sanitize(value)
    if not tag:
        raise SpaceTagsError("tag must not be empty")
    if len(tag) > MAX_TAG_LENGTH:
        raise SpaceTagsError(
            "tag must be at most {} characters".format(MAX_TAG_LENGTH)
        )
    return tag


def load_mapping() -> List[TagRecord]:
    path = mapping_path()
    if not path.exists():
        return []
    records: List[TagRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 2 or not fields[0].strip() or not fields[1].strip():
            continue
        cwd = fields[2].strip() if len(fields) > 2 else ""
        label = fields[3].strip() if len(fields) > 3 else ""
        records.append(TagRecord(fields[0].strip(), fields[1].strip(), cwd, label))
    return records


def save_mapping(records: Sequence[TagRecord]) -> None:
    path = mapping_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# workspace_id\ttag\tcwd\tlabel"]
    for record in sorted(records, key=lambda r: (r.tag, r.label, r.workspace_id)):
        lines.append(
            "\t".join(
                (
                    sanitize(record.workspace_id),
                    sanitize(record.tag),
                    sanitize(record.cwd),
                    sanitize(record.label),
                )
            )
        )
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)


def tag_order(records: Sequence[TagRecord]) -> List[str]:
    """Known tags: order.txt first when present, then the rest alphabetically."""
    known: List[str] = []
    if order_path().exists():
        for line in order_path().read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and line not in known:
                known.append(line)
    for tag in sorted({record.tag for record in records}):
        if tag not in known:
            known.append(tag)
    return known


def find_stale(
    records: Sequence[TagRecord], live_ids: set, cwd: str, label: str
) -> Optional[TagRecord]:
    """A mapping whose workspace is closed, matched by project cwd then label."""
    stale = [record for record in records if record.workspace_id not in live_ids]
    if cwd:
        for record in stale:
            if record.cwd == cwd:
                return record
    if label:
        for record in stale:
            if record.label == label:
                return record
    return None


# ------------------------------------------------------------------ grouping


def unit_groups(workspaces: Sequence[dict]) -> List[List[dict]]:
    """Worktree members travel together, parent first; other spaces stand alone."""
    units: List[List[dict]] = []
    index_by_key: Dict[str, int] = {}
    for workspace in workspaces:
        worktree = workspace.get("worktree") or {}
        key = worktree.get("repo_key")
        if not key:
            units.append([workspace])
            continue
        if key in index_by_key:
            units[index_by_key[key]].append(workspace)
        else:
            index_by_key[key] = len(units)
            units.append([workspace])
    for unit in units:
        unit.sort(key=lambda ws: bool((ws.get("worktree") or {}).get("is_linked_worktree")))
    return units


def desired_order(workspaces: Sequence[dict], records: Sequence[TagRecord]) -> List[str]:
    tags = {record.workspace_id: record.tag for record in records}
    rank = {tag: index for index, tag in enumerate(tag_order(records))}
    units = unit_groups(workspaces)

    def unit_key(item: Tuple[int, List[dict]]):
        index, unit = item
        tag = next(
            (tags.get(ws["workspace_id"]) for ws in unit if tags.get(ws["workspace_id"])),
            "",
        )
        if tag in rank:
            return (0, rank[tag], index)
        if tag:
            return (1, tag, index)  # not in order.txt: after listed tags, alphabetical
        return (2, "", index)  # untagged last

    ordered = [unit for _, unit in sorted(enumerate(units), key=unit_key)]
    return [ws["workspace_id"] for unit in ordered for ws in unit]


def regroup() -> bool:
    """Reorder spaces so each tag forms one block. True when the order changed."""
    workspaces, _ = snapshot_state()
    records = load_mapping()
    if not any(record.tag for record in records):
        return False
    current = [ws["workspace_id"] for ws in workspaces]
    desired = desired_order(workspaces, records)
    if desired == current:
        return False
    socket_request("workspace.move_block", {"workspace_ids": desired})
    return True


# ------------------------------------------------------------------ tagging


def live_context() -> dict:
    raw = os.environ.get("HERDR_PLUGIN_CONTEXT_JSON")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def apply_tag(workspace_id: str, tag: str, cwd: str = "", label: str = "") -> None:
    records = [record for record in load_mapping() if record.workspace_id != workspace_id]
    records.append(TagRecord(workspace_id, validate_tag(tag), cwd, label))
    save_mapping(records)
    herdr_ok(*report_args(workspace_id, tag))
    regroup()


def clear_tag(workspace_id: str) -> None:
    records = load_mapping()
    remaining = [record for record in records if record.workspace_id != workspace_id]
    if len(remaining) != len(records):
        save_mapping(remaining)
    herdr_ok(*report_args(workspace_id, ""))
    regroup()


def apply_all() -> None:
    """Re-report every known tag after a server restart or first link."""
    workspaces, cwds = snapshot_state()
    records = load_mapping()
    live_ids = {ws["workspace_id"] for ws in workspaces}
    by_id = {record.workspace_id: record for record in records if record.workspace_id in live_ids}

    adopted: List[TagRecord] = []
    claimed = set()
    for workspace in workspaces:
        workspace_id = workspace["workspace_id"]
        if workspace_id in by_id:
            continue
        cwd = cwds.get(workspace_id, "")
        label = workspace.get("label", "")
        match = find_stale(
            [r for r in records if r.workspace_id not in claimed], live_ids, cwd, label
        )
        if match is None:
            continue
        claimed.add(match.workspace_id)
        adopted.append(
            TagRecord(workspace_id, match.tag, cwd or match.cwd, label or match.label)
        )

    if adopted:
        records = [r for r in records if r.workspace_id not in claimed] + adopted
        save_mapping(records)

    reported = 0
    for workspace in workspaces:
        workspace_id = workspace["workspace_id"]
        record = by_id.get(workspace_id) or next(
            (r for r in adopted if r.workspace_id == workspace_id), None
        )
        if record is not None:
            herdr_ok(*report_args(workspace_id, record.tag))
            reported += 1
    print("space-tags: {} tagged space(s) refreshed".format(reported))


def workspace_created() -> None:
    context = live_context()
    workspace_id = context.get("workspace_id")
    if not workspace_id:
        return
    records = load_mapping()
    if any(record.workspace_id == workspace_id for record in records):
        return
    workspaces, _ = snapshot_state()
    live_ids = {ws["workspace_id"] for ws in workspaces}
    cwd = context.get("workspace_cwd") or ""
    label = context.get("workspace_label") or ""
    match = find_stale(records, live_ids, cwd, label)
    if match is None:
        return
    remaining = [r for r in records if r.workspace_id != match.workspace_id]
    remaining.append(TagRecord(workspace_id, match.tag, cwd or match.cwd, label or match.label))
    save_mapping(remaining)
    herdr_ok(*report_args(workspace_id, match.tag))
    regroup()


# ------------------------------------------------------------------- picker


def open_picker() -> int:
    context = live_context()
    workspace_id = context.get("workspace_id")
    if not workspace_id:
        raise SpaceTagsError("no active workspace to tag")
    env = {
        "SPACE_TAGS_TARGET": workspace_id,
        "SPACE_TAGS_TARGET_CWD": context.get("workspace_cwd") or "",
        "SPACE_TAGS_TARGET_LABEL": context.get("workspace_label") or "",
    }
    args = ["plugin", "pane", "open", "--plugin", PLUGIN_ID, "--entrypoint", "picker"]
    for key, value in env.items():
        args += ["--env", "{}={}".format(key, value)]
    herdr_ok(*args)
    return 0


def picker() -> int:
    context = live_context()
    target = os.environ.get("SPACE_TAGS_TARGET") or context.get("workspace_id") or ""
    cwd = os.environ.get("SPACE_TAGS_TARGET_CWD") or context.get("workspace_cwd") or ""
    label = os.environ.get("SPACE_TAGS_TARGET_LABEL") or context.get("workspace_label") or ""
    if not target:
        raise SpaceTagsError("no workspace to tag")
    records = load_mapping()
    tags = tag_order(records)
    current = next((r.tag for r in records if r.workspace_id == target), "")

    print("Tag space: {}".format(label or target))
    if current:
        print("current tag: {}".format(current))
    for index, tag in enumerate(tags, 1):
        print("  {}. {}".format(index, tag))
    print("  n. new tag")
    if current:
        print("  x. remove tag")
    print("  q. cancel")

    if not sys.stdin.isatty():
        raise SpaceTagsError("the picker needs an interactive terminal")
    try:
        choice = input("> ").strip()
    except EOFError:
        return 0

    if choice in ("", "q", "Q"):
        return 0
    if choice in ("x", "X"):
        clear_tag(target)
        print("cleared tag on {}".format(label or target))
        return 0
    if choice in ("n", "N"):
        new_tag = validate_tag(input("new tag: "))
        apply_tag(target, new_tag, cwd, label)
        print("tagged {} as {}".format(label or target, new_tag))
        return 0
    if choice.isdigit() and 1 <= int(choice) <= len(tags):
        tag = tags[int(choice) - 1]
        apply_tag(target, tag, cwd, label)
        print("tagged {} as {}".format(label or target, tag))
        return 0
    tag = validate_tag(choice)
    apply_tag(target, tag, cwd, label)
    print("tagged {} as {}".format(label or target, tag))
    return 0


def list_tags() -> int:
    records = load_mapping()
    workspaces, _ = snapshot_state()
    labels = {ws["workspace_id"]: ws.get("label", "") for ws in workspaces}
    if not records:
        print("space-tags: no tags yet")
        return 0
    for tag in tag_order(records):
        for record in sorted(records, key=lambda r: r.label or r.workspace_id):
            if record.tag != tag:
                continue
            label = labels.get(record.workspace_id) or record.label or record.workspace_id
            live = "" if record.workspace_id in labels else " (closed)"
            print("{}\t{}{}".format(tag, label, live))
    return 0


# -------------------------------------------------------------------- entry


def _set_command(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="space_tags.py set")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--cwd", default="")
    parser.add_argument("--label", default="")
    args = parser.parse_args(list(argv))
    apply_tag(args.workspace, args.tag, args.cwd, args.label)
    return 0


def _unset_command(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="space_tags.py unset")
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args(list(argv))
    clear_tag(args.workspace)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    command, rest = argv[0], argv[1:]
    try:
        if command == "tag":
            return open_picker()
        if command == "untag":
            context = live_context()
            workspace_id = context.get("workspace_id")
            if not workspace_id:
                raise SpaceTagsError("no active workspace to untag")
            clear_tag(workspace_id)
            return 0
        if command == "set":
            return _set_command(rest)
        if command == "unset":
            return _unset_command(rest)
        if command in ("apply", "startup"):
            apply_all()
            return 0
        if command == "regroup":
            changed = regroup()
            print("space-tags: {}".format("order updated" if changed else "order already grouped"))
            return 0
        if command == "picker":
            return picker()
        if command == "list":
            return list_tags()
        if command == "workspace-created":
            workspace_created()
            return 0
        raise SpaceTagsError("unknown command: {}".format(command))
    except SpaceTagsError as exc:
        print("space-tags: {}".format(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
