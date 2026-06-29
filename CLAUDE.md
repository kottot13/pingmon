# CLAUDE.md

Project guidance for Claude Code working on **pingmon** (PyPI package
`pingmonitor`, command `pingmon`).

## What this is

A Textual TUI that TCP-pings reachable hosts per country and shows a live,
colour-coded latency / availability dashboard: sortable table, detail panel with
graphs and MOS, Region Advisor, threshold alerts, and a traceroute drill-down.
Pure Python (3.11+), single runtime dependency `textual`. Layout:

```
pingmon/
  app.py      # Textual app: table, detail panel, advisor, alerts, traceroute, CLI main()
  config.py   # TOML load/save; Target (with .code badge); deterministic config_path()
  netutil.py  # async traceroute, GeoIP, flag_emoji / iso_from_flag, desktop notify
  pinger.py   # TCP-connect timing
  render.py   # colours, sparklines, charts, country_label()
  scoring.py  # Region Advisor scoring + profiles
  stats.py    # rolling per-target stats (latency, jitter, loss, MOS)
tools/screenshots.py  # headless regen of docs/*.svg
```

## Conventions

- **Country is shown as a two-letter ISO code, never a flag emoji.** Many
  terminals (iTerm2) can't render regional-indicator flags. The flag is still
  stored in the config for compatibility; `Target.code` derives the badge from it
  (`iso_from_flag`), falling back to the country name's first letters. Render
  country cells with `render.country_label()`. Do not reintroduce raw
  `target.flag` into any user-visible string.
- **Config persistence must be deterministic.** `config_path()` resolves only
  `$PINGMON_CONFIG` → `$XDG_CONFIG_HOME/pingmon/config.toml`. Never make it depend
  on the current working directory — that silently loses user-added targets.
  `save_config()` writes atomically (temp + `os.replace`).

## Release workflow — do ALL of this after any user-facing change

Treat these as one unit; don't stop at "code works". Run from the repo root.

1. **Code + sanity check.** `python -m py_compile pingmon/*.py`; smoke-test in the
   local venv (`bash run.sh`).
2. **Screenshots.** If the change touches anything visible (table, detail panel,
   advisor, traceroute, colours, columns), regenerate them:
   `.venv/bin/python tools/screenshots.py docs` — then eyeball the SVGs
   (`qlmanage -t -s 1200 -o /tmp docs/*.svg` on macOS). They must contain **zero**
   flag-emoji characters.
3. **Docs.** Update `README.md` (features, config example, install) and this file
   so they match the new behaviour. Keep the feature list and screenshots in sync.
4. **Version bump.** Bump BOTH `pyproject.toml` `version` and
   `pingmon/__init__.py __version__` (same value). SemVer: behaviour change or new
   feature → minor; fix only → patch.
5. **Build + verify.** `python -m build` then `twine check dist/*`.
6. **Publish to PyPI.** `twine upload dist/<new-version files>` (project
   `pingmonitor`). Confirm the new version appears on PyPI and
   `uvx --from pingmonitor pingmon -V` reports it.
7. **GitHub.** Commit, push to `origin` (`github.com/kottot13/pingmon`), and tag
   the release (`git tag vX.Y.Z && git push --tags`). Note user-facing changes in
   the commit/release notes.
8. **Install instructions.** If install steps changed (new dependency, new entry
   point, Homebrew resources), update the README install section and the Homebrew
   formula in `packaging/`.

Skip a step only when it genuinely doesn't apply (e.g. no visible change → no
screenshots) and say so.
