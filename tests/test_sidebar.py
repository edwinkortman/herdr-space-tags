#!/usr/bin/env python3
"""Model tests for the stand-alone sidebar. No terminal, no live server."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

for name in ("space_tags", "sidebar"):
    spec = importlib.util.spec_from_file_location(name, REPO / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)

import sidebar  # noqa: E402
import space_tags  # noqa: E402


def snapshot():
    return {
        "focused_workspace_id": "w2",
        "focused_pane_id": "w2:p1",
        "focused_tab_id": "w2:t1",
        "workspaces": [
            {"workspace_id": "w1", "label": "alpha", "focused": False, "agent_status": "idle"},
            {"workspace_id": "w2", "label": "beta", "focused": True, "agent_status": "working"},
            {"workspace_id": "w3", "label": "gamma", "focused": False, "agent_status": "unknown"},
        ],
        "tabs": [
            {"tab_id": "w1:t1", "workspace_id": "w1", "label": "1"},
            {"tab_id": "w2:t1", "workspace_id": "w2", "label": "1"},
            {"tab_id": "w3:t1", "workspace_id": "w3", "label": "1"},
        ],
        "panes": [
            {"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1", "cwd": "/p/alpha"},
            {"pane_id": "w2:p1", "workspace_id": "w2", "tab_id": "w2:t1", "cwd": "/p/beta"},
            {"pane_id": "w3:p1", "workspace_id": "w3", "tab_id": "w3:t1", "cwd": "/p/gamma"},
        ],
        "agents": [
            {
                "pane_id": "w2:p1",
                "workspace_id": "w2",
                "tab_id": "w2:t1",
                "agent": "pi",
                "agent_status": "working",
                "terminal_title": "π - beta",
            }
        ],
    }


CONFIG = space_tags.PluginConfig(
    order=["work", "personal"],
    rules=[
        space_tags.Rule("work", ["alpha", "beta"], []),
        space_tags.Rule("personal", ["gamma"], []),
    ],
    colors={"work": "#1e66f5", "personal": "#8839ef"},
)


def texts(view):
    return ["".join(text for text, _ in row.segments) for row in view.rows]


class SidebarModelTests(unittest.TestCase):
    def test_groups_carry_a_header_with_divider_and_air(self):
        view = sidebar.build_rows(snapshot(), CONFIG, [], 40)
        lines = texts(view)
        self.assertEqual(lines[0], "")
        self.assertTrue(lines[1].startswith("   work ─"))
        self.assertEqual(lines[2], "")
        self.assertEqual(lines[3], "   alpha")
        self.assertEqual(lines[4], "   beta")
        self.assertEqual(lines[5], "")
        self.assertTrue(lines[6].startswith("   personal ─"))
        self.assertEqual(lines[8], "   gamma")

    def test_names_branches_and_headers_share_one_column(self):
        snap = snapshot()
        view = sidebar.build_rows(
            snap, CONFIG, [], 40, branches={"w1": "main", "w2": "feat/x", "w3": "release/1.4"}
        )
        for row in view.rows:
            if row.kind in ("header", "space", "branch", "agent"):
                self.assertTrue(
                    row.segments[0][0].startswith(sidebar.INDENT)
                    or row.segments[0][0].startswith(sidebar.MARGIN),
                    row,
                )
        lines = texts(view)
        self.assertEqual(lines[4], "   main")
        self.assertEqual(lines[6], "   feat/x")

    def test_divider_fills_to_the_pane_width(self):
        view = sidebar.build_rows(snapshot(), CONFIG, [], 40)
        header = texts(view)[1]
        self.assertEqual(len(header), 39)

    def test_tag_colors_come_from_config(self):
        view = sidebar.build_rows(snapshot(), CONFIG, [], 40)
        self.assertEqual(view.styles["tag:work"], "#1e66f5")
        self.assertEqual(view.styles["tag:personal"], "#8839ef")
        self.assertEqual(view.styles["divider:work"], "#1e66f5")

    def test_focused_workspace_is_marked(self):
        view = sidebar.build_rows(snapshot(), CONFIG, [], 40)
        focused = [row for row in view.rows if row.focused]
        self.assertEqual([row.target for row in focused], ["w2"])

    def test_agents_section_lists_agents_with_their_location(self):
        view = sidebar.build_rows(snapshot(), CONFIG, [], 40)
        lines = texts(view)
        self.assertTrue(any(line.startswith("   agents ─") for line in lines))
        agent = [row for row in view.rows if row.kind == "agent"]
        self.assertEqual(len(agent), 1)
        self.assertEqual(agent[0].target, "w2:p1")
        self.assertIn("pi", "".join(text for text, _ in agent[0].segments))
        self.assertIn("beta", "".join(text for text, _ in agent[0].segments))

    def test_status_dot_and_narrow_widths(self):
        config = CONFIG._replace(status_dot=True)
        view = sidebar.build_rows(snapshot(), config, [], 20)
        first = [row for row in view.rows if row.kind == "space"][0]
        self.assertEqual(first.segments[0][0] + first.segments[1][0], " ●")
        for row in view.rows:
            for text, _ in row.segments:
                self.assertLessEqual(len(text), 20)

    def test_truncation_uses_an_ellipsis(self):
        self.assertEqual(sidebar.truncate("abcdef", 4), "abc…")
        self.assertEqual(sidebar.truncate("abc", 4), "abc")
        self.assertEqual(sidebar.truncate("abc", 0), "")


if __name__ == "__main__":
    unittest.main()
