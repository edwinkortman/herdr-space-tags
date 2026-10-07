#!/usr/bin/env python3
"""Unit tests for space_tags.py. No live Herdr server, no live mutations.

The Herdr CLI is replaced by a stub executable that records every call and
answers `api snapshot` with a fixed session, and socket requests are put in
dry-run mode so grouping only prints its payload.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location("space_tags", REPO / "space_tags.py")
space_tags = importlib.util.module_from_spec(spec)
spec.loader.exec_module(space_tags)

STUB = """#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")

if args[:2] == ["api", "snapshot"]:
    print(open(os.environ["STUB_SNAPSHOT"]).read())
elif args[:1] == ["plugin"]:
    print("{}")
"""

SNAPSHOT = {
    "result": {
        "type": "session_snapshot",
        "snapshot": {
            "workspaces": [
                {"workspace_id": "w1", "label": "alpha"},
                {"workspace_id": "w2", "label": "beta"},
                {"workspace_id": "w3", "label": "gamma"},
            ],
            "panes": [
                {"workspace_id": "w1", "cwd": "/p/alpha"},
                {"workspace_id": "w2", "cwd": "/p/beta"},
                {"workspace_id": "w3", "cwd": "/p/gamma"},
            ],
        },
    }
}


class Fixture:
    def __init__(self, snapshot=SNAPSHOT):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        self.config = root / "config"
        self.config.mkdir()
        self.log = root / "calls.jsonl"
        snapshot_file = root / "snapshot.json"
        snapshot_file.write_text(json.dumps(snapshot))
        stub = root / "herdr"
        stub.write_text(STUB)
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        self.root = root
        self._env = dict(os.environ)

    def __enter__(self):
        os.environ.update(
            {
                "HERDR_BIN_PATH": str(self.root / "herdr"),
                "HERDR_PLUGIN_CONFIG_DIR": str(self.config),
                "STUB_LOG": str(self.log),
                "STUB_SNAPSHOT": str(self.root / "snapshot.json"),
                "SPACE_TAGS_DRY_RUN": "1",
            }
        )
        os.environ.pop("HERDR_PLUGIN_CONTEXT_JSON", None)
        os.environ.pop("SPACE_TAGS_TARGET", None)
        return self

    def __exit__(self, *exc):
        os.environ.clear()
        os.environ.update(self._env)
        self.dir.cleanup()
        return False

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def write(self, name, content):
        (self.config / name).write_text(content)


class TagValueTests(unittest.TestCase):
    def test_validate_tag_trims_and_collapses(self):
        self.assertEqual(space_tags.validate_tag("  hello   world  "), "hello world")

    def test_validate_tag_rejects_empty_and_long(self):
        with self.assertRaises(space_tags.SpaceTagsError):
            space_tags.validate_tag("   ")
        with self.assertRaises(space_tags.SpaceTagsError):
            space_tags.validate_tag("x" * (space_tags.MAX_TAG_LENGTH + 1))


class MappingTests(unittest.TestCase):
    def test_round_trip_preserves_records(self):
        with Fixture() as fx:
            records = [
                space_tags.TagRecord("w1", "work", "/p/alpha", "alpha"),
                space_tags.TagRecord("w2", "personal", "/p/beta", "beta"),
            ]
            space_tags.save_mapping(records)
            self.assertEqual(sorted(space_tags.load_mapping()), sorted(records))
            self.assertTrue(fx.config.joinpath("tags.tsv").read_text().startswith("#"))

    def test_load_skips_comments_and_broken_lines(self):
        with Fixture() as fx:
            fx.write("tags.tsv", "# note\n\nw1\twork\t/p\talpha\nbroken\n")
            self.assertEqual(
                space_tags.load_mapping(),
                [space_tags.TagRecord("w1", "work", "/p", "alpha")],
            )

    def test_tag_order_prefers_order_file(self):
        with Fixture() as fx:
            records = [
                space_tags.TagRecord("w1", "work", "", ""),
                space_tags.TagRecord("w2", "personal", "", ""),
                space_tags.TagRecord("w3", "home", "", ""),
            ]
            self.assertEqual(
                space_tags.tag_order(records), ["home", "personal", "work"]
            )
            fx.write("order.txt", "personal\nwork\n")
            self.assertEqual(
                space_tags.tag_order(records), ["personal", "work", "home"]
            )


class OrderingTests(unittest.TestCase):
    def test_untagged_spaces_end_up_last(self):
        workspaces = SNAPSHOT["result"]["snapshot"]["workspaces"]
        records = [space_tags.TagRecord("w2", "work", "", "beta")]
        with Fixture():
            self.assertEqual(
                space_tags.desired_order(workspaces, records), ["w2", "w1", "w3"]
            )

    def test_order_file_sets_group_sequence(self):
        workspaces = SNAPSHOT["result"]["snapshot"]["workspaces"]
        records = [
            space_tags.TagRecord("w1", "work", "", "alpha"),
            space_tags.TagRecord("w2", "personal", "", "beta"),
        ]
        with Fixture() as fx:
            fx.write("order.txt", "work\npersonal\n")
            self.assertEqual(
                space_tags.desired_order(workspaces, records), ["w1", "w2", "w3"]
            )

    def test_worktree_members_stay_together_parent_first(self):
        workspaces = [
            {"workspace_id": "w2", "label": "child",
             "worktree": {"repo_key": "r1", "is_linked_worktree": True}},
            {"workspace_id": "w1", "label": "parent",
             "worktree": {"repo_key": "r1", "is_linked_worktree": False}},
            {"workspace_id": "w3", "label": "other"},
        ]
        records = [space_tags.TagRecord("w3", "work", "", "other")]
        with Fixture():
            self.assertEqual(
                space_tags.desired_order(workspaces, records), ["w3", "w1", "w2"]
            )


class ApplyTests(unittest.TestCase):
    def test_apply_reports_live_tags_and_adopts_stale_by_cwd(self):
        with Fixture() as fx:
            fx.write(
                "tags.tsv",
                "# workspace_id\ttag\tcwd\tlabel\n"
                "w9\twork\t/p/alpha\talpha\n"
                "w2\tpersonal\t/p/beta\tbeta\n",
            )
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                space_tags.apply_all()
            reports = {
                tuple(call[:5])
                for call in fx.calls()
                if call[:2] == ["workspace", "report-metadata"]
            }
            self.assertEqual(
                reports,
                {
                    ("workspace", "report-metadata", "w1", "--source",
                     space_tags.SOURCE),
                    ("workspace", "report-metadata", "w2", "--source",
                     space_tags.SOURCE),
                },
            )
            report_values = {
                call[2]: call[6]
                for call in fx.calls()
                if call[:2] == ["workspace", "report-metadata"]
            }
            self.assertEqual(
                report_values,
                {"w1": "tag=work", "w2": "tag=personal"},
            )
            saved = {r.workspace_id: r.tag for r in space_tags.load_mapping()}
            self.assertEqual(saved, {"w1": "work", "w2": "personal"})
            self.assertIn("2 tagged space(s) refreshed", out.getvalue())

    def test_workspace_created_adopts_known_project(self):
        with Fixture() as fx:
            fx.write(
                "tags.tsv",
                "# workspace_id\ttag\tcwd\tlabel\n"
                "w9\twork\t/p/alpha\talpha\n",
            )
            os.environ["HERDR_PLUGIN_CONTEXT_JSON"] = json.dumps(
                {
                    "workspace_id": "w1",
                    "workspace_cwd": "/p/alpha",
                    "workspace_label": "alpha",
                }
            )
            with contextlib.redirect_stdout(io.StringIO()):
                space_tags.workspace_created()
            reports = [
                call for call in fx.calls()
                if call[:2] == ["workspace", "report-metadata"]
            ]
            self.assertEqual(reports[0][6], "tag=work")
            self.assertEqual(
                [r.workspace_id for r in space_tags.load_mapping()], ["w1"]
            )


class RegroupTests(unittest.TestCase):
    def test_regroup_sends_one_atomic_move_block(self):
        with Fixture() as fx:
            fx.write(
                "tags.tsv",
                "# workspace_id\ttag\tcwd\tlabel\n"
                "w3\twork\t/p/gamma\tgamma\n",
            )
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertTrue(space_tags.regroup())
            payload = json.loads(out.getvalue().strip())
            self.assertEqual(payload["method"], "workspace.move_block")
            self.assertEqual(
                payload["params"], {"workspace_ids": ["w3", "w1", "w2"]}
            )

    def test_regroup_is_a_noop_when_already_grouped(self):
        with Fixture() as fx:
            fx.write(
                "tags.tsv",
                "# workspace_id\ttag\tcwd\tlabel\n"
                "w1\twork\t/p/alpha\talpha\n",
            )
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertFalse(space_tags.regroup())
            self.assertEqual(out.getvalue(), "")


class CommandTests(unittest.TestCase):
    def test_set_and_unset_report_and_clear(self):
        with Fixture() as fx:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    space_tags.main(
                        ["set", "--workspace", "w1", "--tag", "work",
                         "--cwd", "/p/alpha", "--label", "alpha"]
                    ),
                    0,
                )
                self.assertEqual(
                    space_tags.main(["unset", "--workspace", "w1"]), 0
                )
            reports = [
                call for call in fx.calls()
                if call[:2] == ["workspace", "report-metadata"]
            ]
            self.assertEqual(reports[0][6], "tag=work")
            self.assertEqual(reports[-1][5], "--clear-token")
            self.assertEqual(reports[-1][6], "tag")

    def test_unknown_command_fails_cleanly(self):
        with Fixture():
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(space_tags.main(["nope"]), 1)

    def test_list_prints_tagged_and_closed_spaces(self):
        with Fixture() as fx:
            fx.write(
                "tags.tsv",
                "# workspace_id\ttag\tcwd\tlabel\n"
                "w1\twork\t/p/alpha\talpha\n"
                "w9\twork\t/p/gone\tgone\n",
            )
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(space_tags.main(["list"]), 0)
            self.assertIn("work\talpha", out.getvalue())
            self.assertIn("work\tgone (closed)", out.getvalue())


if __name__ == "__main__":
    unittest.main()
