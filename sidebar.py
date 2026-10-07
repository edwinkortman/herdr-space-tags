#!/usr/bin/env python3
"""Stand-alone spaces sidebar for Herdr.

A plugin pane that renders the tag-grouped spaces list and the agent list, so
the native sidebar can stay hidden. Grouping, ordering and colours come from
the same `config.toml` the rest of the plugin reads; tags are still reported as
workspace metadata by `space_tags.py`.

    python3 sidebar.py run      # the pane process (curses TUI)
    python3 sidebar.py toggle   # dock it in the focused tab, or close it
    python3 sidebar.py ensure   # dock it if the focused tab has none
    python3 sidebar.py status   # where it is currently docked
"""

from __future__ import annotations

import curses
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

import space_tags

PANE_TITLE = "Spaces"
MARKER = "spaces_sidebar"
POLL_MS = 1000
GIT_TTL_SECONDS = 10.0
SNOOZE_SECONDS = 12 * 3600

MARGIN = " "  # one column of air on the left
GUTTER = "  "  # the status dot lives here
INDENT = MARGIN + GUTTER  # project names, branches and headers share this column

DEFAULT_TAG_COLORS = (
    "#1e66f5",
    "#40a02b",
    "#8839ef",
    "#fe640b",
    "#179299",
    "#d20f39",
    "#df8e1d",
    "#ea76cb",
)

STATUS_STYLES = {
    "working": "dot-working",
    "blocked": "dot-blocked",
    "idle": "dot-idle",
    "done": "dot-done",
    "unknown": "dot-unknown",
}

STATUS_COLORS = {
    "dot-working": "#40a02b",
    "dot-blocked": "#d20f39",
    "dot-idle": "#1e66f5",
    "dot-done": "#179299",
    "dot-unknown": "#9ca0b0",
}

SECTION_COLOR = "#9ca0b0"


class Row(NamedTuple):
    """One rendered line: styled segments plus what a click should focus."""

    kind: str  # air | header | space | branch | agent
    segments: Tuple[Tuple[str, str], ...] = ()
    target: Optional[str] = None
    focused: bool = False


class View(NamedTuple):
    rows: List[Row]
    styles: Dict[str, str]


# ------------------------------------------------------------------ plumbing


def snapshot() -> dict:
    result = space_tags.socket_request("session.snapshot", {})
    return result.get("snapshot") or {}


def truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return "…"
    return text[: width - 1] + "…"


class GitCache:
    """Branch and ahead/behind per workspace cwd, refreshed on a slow timer."""

    def __init__(self, ttl: float = GIT_TTL_SECONDS) -> None:
        self.ttl = ttl
        self._paths: Dict[str, str] = {}
        self._state: Dict[str, str] = {}
        self._at = 0.0

    def refresh(self, paths: Dict[str, str]) -> Dict[str, str]:
        if time.monotonic() - self._at < self.ttl and paths == self._paths:
            return self._state
        self._state = {ws_id: git_state(cwd) for ws_id, cwd in paths.items()}
        self._paths = dict(paths)
        self._at = time.monotonic()
        return self._state


def git_state(cwd: str) -> str:
    """`branch`, plus ↑ahead/↓behind when there is an upstream."""
    if not cwd or not os.path.isdir(cwd):
        return ""
    try:
        branch = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
        if not branch or branch == "HEAD":
            return ""
        counts = subprocess.run(
            ["git", "-C", cwd, "rev-list", "--left-right", "--count", "HEAD...@{u}"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if counts.returncode == 0:
            parts = counts.stdout.split()
            if len(parts) == 2:
                if parts[0] != "0":
                    branch += " ↑" + parts[0]
                if parts[1] != "0":
                    branch += " ↓" + parts[1]
        return branch
    except (OSError, subprocess.SubprocessError):
        return ""


# --------------------------------------------------------------------- model


def color_styles(config, tags: Sequence[str]) -> Dict[str, str]:
    """Style name -> colour, for every tag and status the view can use."""
    styles: Dict[str, str] = dict(STATUS_COLORS)
    configured = dict(getattr(config, "colors", None) or {})
    for index, tag in enumerate(dict.fromkeys(tag for tag in tags if tag)):
        color = configured.get(tag) or DEFAULT_TAG_COLORS[index % len(DEFAULT_TAG_COLORS)]
        styles["tag:" + tag] = color
        styles["divider:" + tag] = color
    styles["section"] = SECTION_COLOR
    styles["section-divider"] = SECTION_COLOR
    return styles


def divider(width: int, used: int) -> str:
    return "─" * max(0, width - used - 1)


def header_row(tag: str, width: int) -> Row:
    return Row(
        "header",
        (
            (INDENT + tag, "tag:" + tag),
            (" " + divider(width, len(INDENT) + len(tag) + 1), "divider:" + tag),
        ),
    )


def section_row(title: str, width: int) -> Row:
    return Row(
        "header",
        (
            (INDENT + title, "section"),
            (" " + divider(width, len(INDENT) + len(title) + 1), "section-divider"),
        ),
    )


def space_row(workspace: dict, tag: str, status_dot: bool, width: int) -> Row:
    name = truncate(
        workspace.get("label") or workspace.get("workspace_id", ""),
        width - len(INDENT),
    )
    style = "tag:" + tag if tag else "name"
    if status_dot:
        dot = STATUS_STYLES.get(workspace.get("agent_status") or "unknown", "dot-unknown")
        segments = ((MARGIN, "plain"), ("●", dot), (" ", "plain"), (name, style))
    else:
        segments = ((INDENT, "plain"), (name, style))
    return Row("space", segments, workspace.get("workspace_id"), bool(workspace.get("focused")))


def branch_row(branch: str, width: int) -> Row:
    return Row("branch", ((INDENT, "plain"), (truncate(branch, width - len(INDENT)), "branch")))


def agent_row(agent: dict, snap: dict, width: int) -> Row:
    dot = STATUS_STYLES.get(agent.get("agent_status") or "unknown", "dot-unknown")
    labels = {ws["workspace_id"]: ws.get("label", "") for ws in snap.get("workspaces", [])}
    tabs = {tab["tab_id"]: tab.get("label", "") for tab in snap.get("tabs", [])}
    name = agent.get("agent") or "terminal"
    tail = " · ".join(
        part
        for part in (
            labels.get(agent.get("workspace_id", ""), ""),
            tabs.get(agent.get("tab_id", ""), ""),
        )
        if part
    )
    room = width - len(INDENT)
    if tail:
        tail = truncate(tail, max(4, room - len(name) - 3))
        room -= len(tail) + 3
    return Row(
        "agent",
        (
            (MARGIN, "plain"),
            ("●", dot),
            (" ", "plain"),
            (truncate(name, max(3, room)), "agent"),
            ((" · " + tail) if tail else "", "branch"),
        ),
        agent.get("pane_id"),
    )


def build_rows(
    snap: dict,
    config,
    mapping: Sequence,
    width: int,
    *,
    branches: Optional[Dict[str, str]] = None,
) -> View:
    workspaces = snap.get("workspaces", [])
    panes = [pane for pane in snap.get("panes", []) if not is_sidebar(pane)]
    cwds: Dict[str, str] = {}
    for pane in panes:
        ws_id = pane.get("workspace_id")
        cwd = pane.get("cwd")
        if ws_id and cwd and ws_id not in cwds:
            cwds[ws_id] = cwd

    tags = space_tags.resolved_tags(workspaces, cwds, mapping, config)
    groups: Dict[str, List[dict]] = {}
    for workspace in workspaces:
        groups.setdefault(tags.get(workspace["workspace_id"], ""), []).append(workspace)

    styles = color_styles(config, list(tags.values()))
    status_dot = bool(getattr(config, "status_dot", False))
    show_branch = bool(getattr(config, "show_branch", True))
    branches = branches or {}

    def append_group(members: List[dict], tag: str) -> None:
        for workspace in members:
            rows.append(space_row(workspace, tag, status_dot, width))
            if show_branch:
                state = branches.get(workspace["workspace_id"], "")
                if state:
                    rows.append(branch_row(state, width))

    rows: List[Row] = []
    for tag in space_tags.tag_order(config.order, tags.values()):
        members = groups.pop(tag, [])
        if not members:
            continue
        rows.append(Row("air"))
        rows.append(header_row(tag, width))
        rows.append(Row("air"))
        append_group(members, tag)

    untagged = groups.pop("", [])
    if untagged:
        rows.append(Row("air"))
        append_group(untagged, "")

    agents = snap.get("agents", [])
    if getattr(config, "show_agents", True) and agents:
        order = {
            ws["workspace_id"]: index for index, ws in enumerate(snap.get("workspaces", []))
        }
        ranked = sorted(
            agents,
            key=lambda a: (order.get(a.get("workspace_id", ""), 999), a.get("pane_id", "")),
        )
        rows.append(Row("air"))
        rows.append(section_row("agents", width))
        rows.append(Row("air"))
        for agent in ranked:
            rows.append(agent_row(agent, snap, width))

    return View(rows, styles)


# ------------------------------------------------------------------- docking


def focused_tab(snap: dict) -> Tuple[str, str]:
    return (snap.get("focused_workspace_id") or "", snap.get("focused_tab_id") or "")


def is_sidebar(pane: dict) -> bool:
    return (pane.get("label") or "") == PANE_TITLE or MARKER in (pane.get("tokens") or {})


def sidebars_in_tab(snap: dict, tab_id: str) -> List[dict]:
    return [
        pane
        for pane in snap.get("panes", [])
        if pane.get("tab_id") == tab_id and is_sidebar(pane)
    ]


# Closing the sidebar changes the layout, which makes Herdr emit focus events;
# without a snooze the ensure hook would dock it straight back in the tab the
# user just closed it in.


def state_dir() -> Path:
    return Path(
        os.environ.get("HERDR_PLUGIN_STATE_DIR")
        or os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
        or "/tmp"
    )


def snooze_path() -> Path:
    return state_dir() / "sidebar-snooze.json"


def load_snooze() -> Dict[str, float]:
    try:
        data = json.loads(snooze_path().read_text())
    except (OSError, ValueError):
        return {}
    now = time.time()
    return {
        str(tab): float(at)
        for tab, at in data.items()
        if isinstance(at, (int, float)) and now - at < SNOOZE_SECONDS
    }


def snooze(tab_id: str) -> None:
    data = load_snooze()
    data[tab_id] = time.time()
    try:
        snooze_path().parent.mkdir(parents=True, exist_ok=True)
        snooze_path().write_text(json.dumps(data))
    except OSError:
        pass


def wake(tab_id: str = "") -> None:
    data = load_snooze()
    if tab_id:
        data.pop(tab_id, None)
    else:
        data = {}
    try:
        snooze_path().write_text(json.dumps(data))
    except OSError:
        pass


def dock(width: int = 30, tab_id: str = "") -> Optional[str]:
    """Open the sidebar as the left column of a tab, returning its pane id."""
    snap = snapshot()
    _, focused = focused_tab(snap)
    tab_id = tab_id or focused
    if not tab_id or sidebars_in_tab(snap, tab_id):
        return None
    focused_pane = snap.get("focused_pane_id") or ""
    if not focused_pane:
        return None

    layout = space_tags.socket_request("pane.layout", {"pane_id": focused_pane})
    layout = layout.get("layout") or {}
    area = layout.get("area") or {}
    total = float(area.get("width") or 0)
    panes = [pane for pane in layout.get("panes") or [] if pane.get("pane_id")]
    if not panes or total <= 0:
        return None
    edge = min(
        panes,
        key=lambda pane: (
            (pane.get("rect") or {}).get("x", 0),
            (pane.get("rect") or {}).get("y", 0),
        ),
    )
    edge_pane = edge["pane_id"]

    response = space_tags.herdr(
        "plugin",
        "pane",
        "open",
        "--plugin",
        space_tags.PLUGIN_ID,
        "--entrypoint",
        "sidebar",
        "--placement",
        "split",
        "--target-pane",
        edge_pane,
        "--direction",
        "right",
        "--no-focus",
    )
    opened = ((response.get("result") or {}).get("plugin_pane") or {}).get("pane") or {}
    pane_id = opened.get("pane_id")
    if not pane_id:
        return None

    space_tags.socket_request(
        "pane.swap", {"source_pane_id": pane_id, "target_pane_id": edge_pane}
    )
    share = max(0.08, min(0.45, float(width) / total))
    amount = abs(share - 0.5)
    if amount >= 0.005:
        space_tags.socket_request(
            "pane.resize", {"pane_id": pane_id, "direction": "left", "amount": amount}
        )
    return pane_id


def close_sidebars(tab_id: str = "") -> int:
    snap = snapshot()
    _, focused = focused_tab(snap)
    tab_id = tab_id or focused
    closed = 0
    for pane in sidebars_in_tab(snap, tab_id):
        space_tags.socket_request("pane.close", {"pane_id": pane["pane_id"]})
        closed += 1
    if closed and tab_id:
        snooze(tab_id)
    return closed


def toggle(tab_id: str = "") -> int:
    snap = snapshot()
    _, focused = focused_tab(snap)
    tab_id = tab_id or focused
    if not tab_id:
        return 1
    if sidebars_in_tab(snap, tab_id):
        close_sidebars(tab_id)
        return 0
    wake(tab_id)
    config = space_tags.load_config()
    return 0 if dock(int(getattr(config, "sidebar_width", 30)), tab_id) else 1


def ensure(tab_id: str = "") -> int:
    config = space_tags.load_config()
    if not getattr(config, "auto_dock", True):
        return 0
    snap = snapshot()
    _, focused = focused_tab(snap)
    tab_id = tab_id or focused
    if not tab_id or sidebars_in_tab(snap, tab_id) or tab_id in load_snooze():
        return 0
    return 0 if dock(int(getattr(config, "sidebar_width", 30)), tab_id) else 1


# ----------------------------------------------------------------------- tui


def hex_to_256(value: str) -> int:
    text = (value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    try:
        target = tuple(int(text[i : i + 2], 16) for i in (0, 2, 4))
    except (ValueError, IndexError):
        return 7
    candidates: List[Tuple[int, Tuple[int, int, int]]] = []
    for r in range(6):
        for g in range(6):
            for b in range(6):
                candidates.append(
                    (
                        16 + 36 * r + 6 * g + b,
                        (0 if r == 0 else 55 + r * 40, 0 if g == 0 else 55 + g * 40, 0 if b == 0 else 55 + b * 40),
                    )
                )
    for level in range(24):
        shade = 8 + level * 10
        candidates.append((232 + level, (shade, shade, shade)))
    return min(candidates, key=lambda item: sum((a - b) ** 2 for a, b in zip(item[1], target)))[0]


class Sidebar:
    def __init__(self, stdscr) -> None:
        self.stdscr = stdscr
        self.rows: List[Row] = []
        self.styles: Dict[str, str] = {}
        self.pairs: Dict[str, int] = {}
        self.selected = 0
        self.offset = 0
        self.git = GitCache()
        self.fingerprint = ""
        self.last_focused: Optional[str] = None
        self.error = ""
        self.config: Optional[space_tags.PluginConfig] = None
        self.mapping: List[space_tags.TagRecord] = []

    # -- setup

    def setup(self) -> None:
        curses.curs_set(0)
        self.stdscr.keypad(True)
        self.stdscr.timeout(POLL_MS)
        try:
            curses.start_color()
            curses.use_default_colors()
        except curses.error:
            pass
        masks = getattr(curses, "ALL_MOUSE_EVENTS", 0)
        for name in ("BUTTON1_CLICKED", "BUTTON1_PRESSED", "BUTTON4_PRESSED", "BUTTON5_PRESSED"):
            masks |= getattr(curses, name, 0)
        try:
            curses.mousemask(masks)
        except curses.error:
            pass
        os.environ.setdefault("ESCDELAY", "25")

    def pair(self, style: str) -> int:
        if style in self.pairs:
            return self.pairs[style]
        value = self.styles.get(style)
        if value is None:
            return 0
        index = len(self.pairs) + 1
        try:
            curses.init_pair(index, hex_to_256(value), -1)
        except curses.error:
            return 0
        self.pairs[style] = index
        return index

    def attrs(self, style: str, focused: bool = False) -> int:
        attr = curses.color_pair(self.pair(style))
        if style.startswith("tag:") or style in STATUS_STYLES.values() or style == "section":
            attr |= curses.A_BOLD
        if style == "branch" or style == "section-divider" or style.startswith("divider:"):
            attr |= curses.A_DIM
        if focused:
            attr |= curses.A_BOLD
        return attr

    # -- data

    def reload(self, force: bool = False) -> bool:
        snap = snapshot()
        width, height = self.stdscr.getmaxyx()[1], self.stdscr.getmaxyx()[0]
        fingerprint = repr(
            (
                [(w.get("workspace_id"), w.get("label"), w.get("agent_status")) for w in snap.get("workspaces", [])],
                [(p.get("pane_id"), p.get("tab_id"), p.get("agent_status"), p.get("label")) for p in snap.get("panes", [])],
                [(a.get("pane_id"), a.get("agent_status"), a.get("terminal_title")) for a in snap.get("agents", [])],
                snap.get("focused_workspace_id"),
                (width, height),
            )
        )
        if not force and fingerprint == self.fingerprint:
            return False
        self.fingerprint = fingerprint
        try:
            self.config = space_tags.load_config()
            self.mapping = space_tags.load_mapping()
        except space_tags.SpaceTagsError as exc:
            self.error = str(exc)
        cwds: Dict[str, str] = {}
        for pane in snap.get("panes", []):
            ws_id = pane.get("workspace_id")
            cwd = pane.get("cwd")
            if ws_id and cwd and ws_id not in cwds:
                cwds[ws_id] = cwd
        branches = self.git.refresh(cwds)
        if self.config is not None:
            view = build_rows(snap, self.config, self.mapping, width, branches=branches)
            self.rows = view.rows
            self.styles = view.styles
            self.pairs = {}
        focused = snap.get("focused_workspace_id")
        if focused != self.last_focused:
            self.last_focused = focused
            self.follow(focused)
        self.clamp()
        return True

    def follow(self, target: Optional[str]) -> None:
        for index, row in enumerate(self.rows):
            if row.target and row.target == target:
                self.selected = index
                return

    def clamp(self) -> None:
        height = self.stdscr.getmaxyx()[0]
        self.selected = min(max(0, self.selected), max(0, len(self.rows) - 1))
        if self.selected < self.offset:
            self.offset = self.selected
        elif self.selected >= self.offset + height:
            self.offset = max(0, self.selected - height + 1)
        self.offset = min(self.offset, max(0, len(self.rows) - height))

    # -- actions

    def selectable(self, index: int) -> bool:
        return 0 <= index < len(self.rows) and self.rows[index].kind in ("space", "agent")

    def move(self, delta: int) -> None:
        index = self.selected
        while True:
            index += delta
            if not 0 <= index < len(self.rows):
                return
            if self.selectable(index):
                self.selected = index
                return

    def activate(self) -> None:
        if not self.selectable(self.selected):
            return
        row = self.rows[self.selected]
        if not row.target:
            return
        if row.kind == "space":
            space_tags.socket_request("workspace.focus", {"workspace_id": row.target})
        else:
            try:
                space_tags.herdr("agent", "focus", row.target)
            except space_tags.SpaceTagsError:
                return
        time.sleep(0.05)
        self.reload(force=True)

    def handle_mouse(self) -> None:
        try:
            _, _, my, _, button = curses.getmouse()
        except curses.error:
            return
        if button & getattr(curses, "BUTTON4_PRESSED", 0):
            self.offset = max(0, self.offset - 3)
            return
        if button & getattr(curses, "BUTTON5_PRESSED", 0):
            self.offset += 3
            self.clamp()
            return
        index = self.offset + my
        if self.selectable(index):
            self.selected = index
            self.activate()

    # -- drawing

    def draw(self) -> None:
        height, width = self.stdscr.getmaxyx()
        self.stdscr.erase()
        for line, row in enumerate(self.rows[self.offset : self.offset + height]):
            x = 0
            cursor = self.offset + line == self.selected and row.kind in ("space", "agent")
            for text, style in row.segments:
                if x >= width:
                    break
                text = truncate(text, width - x)
                attr = self.attrs(style, row.focused)
                if cursor:
                    attr |= curses.A_REVERSE
                try:
                    self.stdscr.addstr(line, x, text, attr)
                except curses.error:
                    pass
                x += len(text)
        if self.error:
            try:
                self.stdscr.addstr(height - 1, 0, truncate(self.error, width), curses.A_REVERSE)
            except curses.error:
                pass
        self.stdscr.noutrefresh()
        curses.doupdate()

    def run(self) -> None:
        self.setup()
        try:
            self.reload(force=True)
        except space_tags.SpaceTagsError as exc:
            self.error = str(exc)
        while True:
            self.clamp()
            self.draw()
            try:
                key = self.stdscr.getch()
            except KeyboardInterrupt:
                return
            if key == -1:
                try:
                    self.reload()
                except space_tags.SpaceTagsError as exc:
                    self.error = str(exc)
                continue
            if key == curses.KEY_RESIZE:
                try:
                    self.reload(force=True)
                except space_tags.SpaceTagsError as exc:
                    self.error = str(exc)
            elif key == curses.KEY_MOUSE:
                self.handle_mouse()
            elif key in (ord("j"), curses.KEY_DOWN):
                self.move(1)
            elif key in (ord("k"), curses.KEY_UP):
                self.move(-1)
            elif key in (curses.KEY_HOME, ord("g")):
                self.move(-len(self.rows))
            elif key in (curses.KEY_END, ord("G")):
                self.move(len(self.rows))
            elif key in (curses.KEY_ENTER, 10, 13):
                self.activate()
            elif key == ord("r"):
                try:
                    self.reload(force=True)
                except space_tags.SpaceTagsError as exc:
                    self.error = str(exc)
            elif key == curses.KEY_PPAGE:
                self.offset = max(0, self.offset - self.stdscr.getmaxyx()[0])
                self.move(-self.stdscr.getmaxyx()[0])
            elif key == curses.KEY_NPAGE:
                self.offset += self.stdscr.getmaxyx()[0]
                self.move(self.stdscr.getmaxyx()[0])


def run() -> int:
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(lambda stdscr: Sidebar(stdscr).run())
    return 0


# ---------------------------------------------------------------------- main


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[0] if argv else "run"
    try:
        if command == "run":
            return run()
        if command == "toggle":
            return toggle()
        if command == "ensure":
            return ensure()
        if command == "close":
            close_sidebars()
            return 0
        if command == "status":
            snap = snapshot()
            workspace_id, tab_id = focused_tab(snap)
            print(
                json.dumps(
                    {
                        "focused_workspace_id": workspace_id,
                        "focused_tab_id": tab_id,
                        "sidebars": [p["pane_id"] for p in sidebars_in_tab(snap, tab_id)],
                    }
                )
            )
            return 0
    except space_tags.SpaceTagsError as exc:
        print("sidebar: {}".format(exc), file=sys.stderr)
        return 1
    print("usage: sidebar.py [run|toggle|ensure|close|status]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
