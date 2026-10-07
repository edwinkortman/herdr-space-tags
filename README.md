# herdr-space-tags

Tag [Herdr](https://herdr.dev) workspaces ("spaces") and group the Spaces
sidebar by tag.

Herdr has no built-in space grouping: the Spaces panel is a flat list, and the
only grouping it knows is Git-worktree provenance. This plugin adds the missing
pieces:

- a **popup tag picker** bound to a keybinding,
- a **`$tag` workspace token** rendered in the sidebar through the existing
  `ui.sidebar.spaces.rows` styling,
- **real grouping**: spaces are reordered so that equal tags form one
  contiguous block, untagged spaces last,
- **persistence**: tags survive server restarts (re-reported by a startup
  hook) and closed-and-reopened projects adopt their old tag.

## Requirements

- Herdr 0.9.0 or newer (workspace metadata tokens: 0.7.4, `workspace.move_block`:
  0.8.0, sidebar value rules: 0.9.0). Linux and macOS only: grouping uses the
  Unix socket API directly, because `workspace.move_block` has no CLI wrapper.
- Python 3.8+ (`python3` on `PATH`).

## Install

```sh
git clone https://github.com/edwinkortman/herdr-space-tags.git
herdr plugin link ~/Development/herdr-space-tags
herdr plugin action invoke herdr-space-tags.apply   # re-apply tags now
```

`[[startup]]` hooks do not run when a plugin is linked, so invoke `apply` once
after linking; from then on tags are re-applied after every server restart.

## Keybindings

Add to `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+t"
type = "plugin_action"
command = "herdr-space-tags.tag"
description = "tag this space"

[[keys.command]]
key = "prefix+alt+t"
type = "plugin_action"
command = "herdr-space-tags.untag"
description = "clear this space's tag"

[[keys.command]]
key = "prefix+alt+g"
type = "plugin_action"
command = "herdr-space-tags.regroup"
description = "group spaces by tag"
```

`tag` opens a popup listing existing tags; pick one, type a new one, or remove
the current tag. `untag` clears the tag of the active space. `regroup` re-sorts
the sidebar after you moved spaces by hand.

## Sidebar styling

```toml
[ui.sidebar.spaces]
row_gap = 1
rows = [
  [{ token = "$tag", bold = true, rules = [
    { equals = "work", fg = "#89b4fa" },
    { equals = "personal", fg = "#f5c2e7" },
    { equals = "home", fg = "#a6e3a1" },
  ] }],
  ["state_icon", "workspace"],
  [{ token = "branch", dim = true }, { token = "git_status", dim = true }],
]
```

A space without a tag simply does not render the `$tag` row, so untagged
spaces keep the compact two-row layout. Reload with `prefix+shift+r` or
`herdr server reload-config`.

## Group order

Groups are ordered by a plain text file:

```
~/.config/herdr/plugins/config/herdr-space-tags/order.txt
```

One tag per line. Tags not listed there follow alphabetically after the listed
ones, and untagged spaces always stay at the end.

## Tags file

Tags are stored in a tab-separated file next to it, editable by hand while
Herdr runs (the next `apply` picks changes up):

```
~/.config/herdr/plugins/config/herdr-space-tags/tags.tsv
# workspace_id	tag	cwd	label
wB	work	/home/edwin/Development/fuckmyday.app	fuckmyday.app
```

The `cwd` column is what lets a closed-and-reopened project keep its tag: a new
workspace whose first pane uses a known cwd adopts that tag on
`workspace.created`.

## Commands

The script also runs standalone, which is useful in scripts and tests:

```sh
export HERDR_PLUGIN_CONFIG_DIR=~/.config/herdr/plugins/config/herdr-space-tags
python3 space_tags.py list
python3 space_tags.py set --workspace w1 --tag work
python3 space_tags.py unset --workspace w1
python3 space_tags.py apply        # same as the startup hook
python3 space_tags.py regroup
```

Set `SPACE_TAGS_DRY_RUN=1` to print the grouping socket request instead of
sending it.

## How it works

- Tags are reported as the `tag` display token with
  `herdr workspace report-metadata`; styling stays in `config.toml`.
- Grouping sends one atomic `workspace.move_block` request over
  `HERDR_SOCKET_PATH` with the complete desired order.
- Workspace tokens are display-only and are **not restored after a server
  restart**, so the `[[startup]]` hook re-reports every kept tag. The same hook
  re-adopts tags for projects that were closed while Herdr was running.
- Git-worktree checkouts are treated as one unit: they are moved together,
  parent first.

## Limits

- Herdr 0.9.x does not list plugin actions in the workspace or pane right-click
  menus: the manifest `contexts` field is not surfaced in the TUI yet, so
  tagging is keybinding and CLI only.
- The tag popup needs an interactive terminal; `herdr plugin action invoke`
  cannot pass a tag as an argument, hence the picker.
- Every tagged space renders its own `$tag` row. Herdr renders sidebar rows per
  space, so a single header for a whole group is not possible without
  order-dependent bookkeeping.
- Not supported on Windows (no Unix domain socket transport in this plugin).

## Development

```sh
tests/run.sh
```

The tests stub the Herdr CLI and run grouping in dry-run mode; they never touch
a live server.

## License

MIT
