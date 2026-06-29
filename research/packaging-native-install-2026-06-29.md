# Making pingmon a native command (like htop) — 2026-06-29

## Context

The user wants `pingmon` to be usable the way `htop` is: a real command you
install once and run from anywhere on Linux or macOS — not a `./run.sh` venv
dance. Question: how to package and distribute it.

## Method

pingmon is a pure-Python Textual app with one runtime dependency (`textual`,
which pulls `rich`, etc.). Reviewed the standard distribution channels for a
Python TUI and validated the primary path locally (build wheel → install into a
clean venv → run the entry point from outside the repo).

## Findings

### What "native like htop" means here
htop is a compiled C binary in your package manager + a config in
`~/.config/htop/`. For a Python app there are three realistic equivalents,
trading off "nativeness" vs. packaging effort:

| Approach | User runs | Needs Python on target? | Effort |
| -------- | --------- | ----------------------- | ------ |
| **pipx / uv tool** | `pipx install pingmon` → `pingmon` | yes (managed, isolated) | tiny — already works |
| **Homebrew formula** | `brew install …/pingmon` | no (brew vendors Python) | medium — needs PyPI release + resources |
| **PyInstaller binary** | download `pingmon`, run it | **no** | medium — build per OS/arch |

### The project was already 90% there
`pyproject.toml` declares `[project.scripts] pingmon = "pingmon.app:main"`, so a
plain `pip install .` already creates a `pingmon` command. Validated:
`pip install .` into a fresh venv → `pingmon --version` works, `--help` works,
`app.tcss` is packaged, and the app launches **from outside the repo** with the
stylesheet loaded from the installed package (CSS_PATH resolves relative to the
module, not the cwd).

### Gaps fixed this session
1. **CLI surface** — added `argparse` (`-V/--version`, `-c/--config`, `-h`) so
   the command behaves like a real tool (`pingmon/app.py:main`).
2. **Config location** — was `./config.toml` in the cwd, which is wrong for a
   run-from-anywhere binary (drops a file wherever you happen to be). Changed
   `config_path()` to htop-style: `$PINGMON_CONFIG` → local `./config.toml` if
   present (dev) → `$XDG_CONFIG_HOME/pingmon/config.toml` (default).
3. **Packaging metadata** — readme, license (MIT + LICENSE file), classifiers,
   keywords, urls; forced inclusion of `app.tcss` in the wheel
   (`tool.hatch.build.targets.wheel.force-include`).

## Recommendations (by effort ÷ payoff)

1. **Ship via pipx (recommended default).** Cross-platform, isolated, updates
   cleanly. Publish to PyPI (`python -m build` → `twine upload`), then users do
   `pipx install pingmon`. Today already works from a checkout:
   `pipx install .` or `pipx install git+https://github.com/…/pingmon`.
   `uv tool install pingmon` / `uvx pingmon` work the same way.
2. **Homebrew tap** for the "brew install" muscle-memory on macOS/Linuxbrew.
   Formula skeleton in `packaging/pingmon.rb`; needs a PyPI sdist URL + sha256,
   then `brew update-python-resources` to vendor deps. Best as a personal tap
   (`brew install kottot13/tap/pingmon`) before attempting homebrew-core.
3. **PyInstaller single binary** for the most htop-like, Python-free install.
   Spec in `packaging/pingmon.spec` (`--collect-all textual` + bundle
   `app.tcss`). Build per OS/arch (no cross-compile); attach binaries to GitHub
   Releases. Optionally automate with a GitHub Actions matrix.
4. **Linux distro packages** (apt/dnf/AUR) — highest effort, lowest marginal
   value while pipx exists. An AUR `pingmon` PKGBUILD wrapping the PyPI release
   is the cheapest of these; defer unless requested.

## Actions

- Implemented: argparse CLI, XDG config default, full pyproject metadata,
  LICENSE, wheel data-file inclusion. Verified `pip install .` → `pingmon`
  runs from outside the repo with CSS loaded.
- Provided templates: `packaging/pingmon.rb` (Homebrew), `packaging/pingmon.spec`
  (PyInstaller). README gained an "Install as a system command" section.
- Deferred (need a public repo / PyPI account from the user): actually
  publishing to PyPI, the Homebrew tap repo, and a Releases CI matrix. Replace
  the `kottot13` / sdist-sha256 placeholders once the repo + PyPI name exist.

### Update — published 2026-06-29
- The PyPI name `pingmon` was already taken (an unrelated "Monitor, record, and
  display ping results" package). PyPI has no namespaces, so we shipped under
  the distribution name **`pingmonitor`** while keeping the import package and
  the CLI command as `pingmon` (allowed: dist name ≠ import name).
- Published **pingmonitor 1.0.0** (sdist + wheel) to PyPI:
  https://pypi.org/project/pingmonitor/1.0.0/ . Verified `pip install pingmonitor`
  in a clean venv → `pingmon --version` and the TUI launch both work.
- Homebrew formula filled in with the real sdist URL + sha256
  (`702643a16ab4b012cdc65950caa98261f58ea0cd0e9630e7e23683fcb228be85`).
- Install line for users: `pipx install pingmonitor` → run `pingmon`.
