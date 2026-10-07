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

    def reports(self):
        """workspace_id -> ('token', 'tag=value') or ('clear', 'tag')."""
        found = {}
        for call in self.calls():
            if call[:2] != ["workspace", "report-metadata"]:
                continue
            action = "token" if call[5] == "--token" else "clear"
            found[call[2]] = (action, call[6])
        return found

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


class ConfigTests(unittest.TestCase):
    def test_missing_config_is_empty(self):
        with Fixture():
            self.assertEqual(space_tags.load_config(), space_tags.PluginConfig([], []))

    def test_order_and_rules_are_parsed(self):
        with Fixture() as fx:
            fx.write(
                "config.toml",
                'order = ["work", "personal"]\n'
                "[[rule]]\n"
                'tag = "work"\n'
                'labels = ["alpha*"]\n'
                'cwds = ["/p/alpha"]\n',
            )
            config = space_tags.load_config()
            self.assertEqual(config.order, ["work", "personal"])
            self.assertEqual(
                config.rules,
                [space_tags.Rule("work", ["alpha*"], ["/p/alpha"])],
            )

    def test_invalid_toml_raises(self):
        with Fixture() as fx:
            fx.write("config.toml", "order = [")
            with self.assertRaises(space_tags.SpaceTagsError):
                space_tags.load_config()

    def test_broken_rules_warn_and_are_skipped(self):
        with Fixture() as fx:
            fx.write(
                "config.toml",
                '[[rule]]\nlabels = ["alpha"]\n'          # no tag
                '[[rule]]\ntag = "work"\n'                # no matchers
                '[[rule]]\ntag = "personal"\nlabels = ["beta"]\n',
            )
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                config = space_tags.load_config()
            self.assertEqual([rule.tag for rule in config.rules], ["personal"])
            self.assertEqual(stderr.getvalue().count("warning"), 2)

    def test_match_rules_label_case_insensitive_and_cwd_glob(self):
        rules = [
            space_tags.Rule("work", ["acme-*"], []),
            space_tags.Rule("personal", [], ["/p/*/beta"]),
        ]
        self.assertEqual(space_tags.match_rules("ACME-admin", "", rules), "work")
        self.assertEqual(space_tags.match_rules("", "/p/x/beta", rules), "personal")
        self.assertIsNone(space_tags.match_rules("other", "/q/x/beta", rules))

    def test_first_matching_rule_wins(self):
        rules = [
            space_tags.Rule("work", ["*"], []),
            space_tags.Rule("personal", ["alpha"], []),
        ]
        self.assertEqual(space_tags.match_rules("alpha", "", rules), "work")

    def test_resolve_tag_manual_wins_and_can_suppress(self):
        rules = [space_tags.Rule("work", ["alpha"], [])]
        self.assertEqual(
            space_tags.resolve_tag("w1", "alpha", "", {}, rules), "work"
        )
        self.assertEqual(
            space_tags.resolve_tag("w1", "alpha", "", {"w1": "personal"}, rules),
            "personal",
        )
        self.assertIsNone(
            space_tags.resolve_tag("w1", "alpha", "", {"w1": ""}, rules)
        )


class MappingTests(unittest.TestCase):
    def test_round_trip_preserves_records_including_suppression(self):
        with Fixture():
            records = [
                space_tags.TagRecord("w1", "work", "/p/alpha", "alpha"),
                space_tags.TagRecord("w2", "", "/p/beta", "beta"),
            ]
            space_tags.save_mapping(records)
            self.assertEqual(sorted(space_tags.load_mapping()), sorted(records))
            self.assertTrue(
                space_tags.mapping_path().read_text().startswith("#")
            )

    def test_load_skips_comments_and_broken_lines(self):
        with Fixture() as fx:
            fx.write("manual.tsv", "# note\n\nw1\twork\t/p\talpha\nbroken\n")
            self.assertEqual(
                space_tags.load_mapping(),
                [space_tags.TagRecord("w1", "work", "/p", "alpha")],
            )


class OrderingTests(unittest.TestCase):
    def test_tag_order_prefers_config_then_alphabetical(self):
        self.assertEqual(
            space_tags.tag_order(["personal"], ["work", "home", ""]),
            ["personal", "home", "work"],
        )

    def test_untagged_spaces_end_up_last(self):
        workspaces = SNAPSHOT["result"]["snapshot"]["workspaces"]
        tags = {"w2": "work"}
        self.assertEqual(
            space_tags.desired_order(workspaces, tags, []), ["w2", "w1", "w3"]
        )

    def test_config_order_sets_group_sequence(self):
        workspaces = SNAPSHOT["result"]["snapshot"]["workspaces"]
        tags = {"w1": "work", "w2": "personal"}
        self.assertEqual(
            space_tags.desired_order(workspaces, tags, ["work", "personal"]),
            ["w1", "w2", "w3"],
        )

    def test_worktree_members_stay_together_parent_first(self):
        workspaces = [
            {"workspace_id": "w2", "label": "child",
             "worktree": {"repo_key": "r1", "is_linked_worktree": True}},
            {"workspace_id": "w1", "label": "parent",
             "worktree": {"repo_key": "r1", "is_linked_worktree": False}},
            {"workspace_id": "w3", "label": "other"},
        ]
        self.assertEqual(
            space_tags.desired_order(workspaces, {"w3": "work"}, []),
            ["w3", "w1", "w2"],
        )


class ApplyTests(unittest.TestCase):
    def test_apply_uses_rules_and_clears_the_rest(self):
        with Fixture() as fx:
            fx.write(
                "config.toml",
                '[[rule]]\ntag = "work"\nlabels = ["alpha"]\n'
                '[[rule]]\ntag = "personal"\ncwds = ["/p/beta"]\n',
            )
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                space_tags.apply_all()
            self.assertEqual(
                fx.reports(),
                {
                    "w1": ("token", "tag=work"),
                    "w2": ("token", "tag=personal"),
                    "w3": ("clear", "tag"),
                },
            )
            self.assertIn("2 of 3 space(s) tagged", out.getvalue())

    def test_apply_manual_override_wins_over_rule(self):
        with Fixture() as fx:
            fx.write("config.toml", '[[rule]]\ntag = "work"\nlabels = ["*"]\n')
            fx.write(
                "manual.tsv",
                "# workspace_id\ttag\tcwd\tlabel\nw2\t\t/p/beta\tbeta\n",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                space_tags.apply_all()
            self.assertEqual(fx.reports()["w1"], ("token", "tag=work"))
            self.assertEqual(fx.reports()["w2"], ("clear", "tag"))

    def test_apply_adopts_stale_manual_record_by_cwd(self):
        with Fixture() as fx:
            fx.write(
                "manual.tsv",
                "# workspace_id\ttag\tcwd\tlabel\n"
                "w9\twork\t/p/alpha\talpha\n",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                space_tags.apply_all()
            self.assertEqual(fx.reports()["w1"], ("token", "tag=work"))
            self.assertEqual(
                [record.workspace_id for record in space_tags.load_mapping()], ["w1"]
            )

    def test_workspace_created_matches_rule(self):
        with Fixture() as fx:
            fx.write("config.toml", '[[rule]]\ntag = "work"\nlabels = ["delta*"]\n')
            os.environ["HERDR_PLUGIN_CONTEXT_JSON"] = json.dumps(
                {"workspace_id": "w2", "workspace_cwd": "/p/beta", "workspace_label": "delta"}
            )
            with contextlib.redirect_stdout(io.StringIO()):
                space_tags.main(["workspace-created"])
            self.assertEqual(fx.reports()["w2"], ("token", "tag=work"))

    def test_workspace_renamed_re_resolves_rules(self):
        with Fixture() as fx:
            fx.write("config.toml", '[[rule]]\ntag = "work"\nlabels = ["*renamed*"]\n')
            os.environ["HERDR_PLUGIN_CONTEXT_JSON"] = json.dumps(
                {"workspace_id": "w1", "workspace_cwd": "/p/alpha",
                 "workspace_label": "renamed-alpha"}
            )
            with contextlib.redirect_stdout(io.StringIO()):
                space_tags.main(["workspace-renamed"])
            self.assertEqual(fx.reports()["w1"], ("token", "tag=work"))

    def test_workspace_created_adopts_closed_project(self):
        with Fixture() as fx:
            fx.write(
                "manual.tsv",
                "# workspace_id\ttag\tcwd\tlabel\nw9\twork\t/p/alpha\talpha\n",
            )
            os.environ["HERDR_PLUGIN_CONTEXT_JSON"] = json.dumps(
                {"workspace_id": "w1", "workspace_cwd": "/p/alpha",
                 "workspace_label": "alpha"}
            )
            with contextlib.redirect_stdout(io.StringIO()):
                space_tags.main(["workspace-created"])
            self.assertEqual(fx.reports()["w1"], ("token", "tag=work"))
            self.assertEqual(
                [record.workspace_id for record in space_tags.load_mapping()], ["w1"]
            )


class RegroupTests(unittest.TestCase):
    def test_regroup_sends_one_atomic_move_block(self):
        with Fixture() as fx:
            fx.write(
                "config.toml",
                'order = ["work"]\n[[rule]]\ntag = "work"\nlabels = ["gamma"]\n',
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
            fx.write("config.toml", '[[rule]]\ntag = "work"\nlabels = ["alpha"]\n')
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertFalse(space_tags.regroup())
            self.assertEqual(out.getvalue(), "")

    def test_regroup_is_a_noop_without_any_tags(self):
        with Fixture():
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(space_tags.regroup())


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
            reports = fx.reports()
            self.assertEqual(reports["w1"], ("clear", "tag"))
            tokens = [
                call for call in fx.calls()
                if call[:2] == ["workspace", "report-metadata"]
                and call[5] == "--token"
            ]
            self.assertEqual(tokens[0][6], "tag=work")
            self.assertEqual(
                [record.tag for record in space_tags.load_mapping()], [""]
            )

    def test_reset_removes_manual_record_and_follows_rules(self):
        with Fixture() as fx:
            fx.write("config.toml", '[[rule]]\ntag = "work"\nlabels = ["alpha"]\n')
            fx.write(
                "manual.tsv",
                "# workspace_id\ttag\tcwd\tlabel\nw1\t\t/p/alpha\talpha\n",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(space_tags.main(["reset", "--workspace", "w1"]), 0)
            self.assertEqual(fx.reports()["w1"], ("token", "tag=work"))
            self.assertEqual(space_tags.load_mapping(), [])

    def test_unknown_command_fails_cleanly(self):
        with Fixture():
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(space_tags.main(["nope"]), 1)

    def test_list_shows_tag_source_and_closed_records(self):
        with Fixture() as fx:
            fx.write("config.toml", '[[rule]]\ntag = "work"\nlabels = ["alpha"]\n')
            fx.write(
                "manual.tsv",
                "# workspace_id\ttag\tcwd\tlabel\n"
                "w2\tpersonal\t/p/beta\tbeta\n"
                "w9\twork\t/p/gone\tgone\n",
            )
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(space_tags.main(["list"]), 0)
            text = out.getvalue()
            self.assertIn("work\talpha\trule", text)
            self.assertIn("personal\tbeta\tmanual", text)
            self.assertIn("-\tgamma\t-", text)
            self.assertIn("work\tgone\t(closed)", text)


if __name__ == "__main__":
    unittest.main()
