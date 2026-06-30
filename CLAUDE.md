# CLAUDE.md

Project guidance for Claude Code working on **pingmon** (PyPI package
`pingmonitor`, command `pingmon`).

## What this is

A Textual TUI that TCP-pings reachable hosts per country and shows a live,
colour-coded latency / availability dashboard: sortable table, detail panel with
graphs and MOS, Region Advisor, threshold alerts, a traceroute drill-down, and
SSH actions for your own servers (embedded login shell + remote htop/atop/top).
Pure Python (3.11+), two runtime dependencies: `textual` and `pyte` (`pyte`
backs the embedded terminals). Layout:

```
pingmon/
  app.py      # Textual app: table, detail panel (+live Server load), advisor, alerts, traceroute, SSH, diagnostics menu, CLI main()
  config.py   # TOML load/save; Target (.code badge, .server/ssh_user/top_tool); deterministic config_path()
  netutil.py  # traceroute (TCP-to-port), GeoIP, flags, desktop notify, ssh helpers, fetch_server_load
  pinger.py   # TCP-connect timing + tcp_ping_banner (SSH-banner liveness)
  terminal.py # TerminalPane: embedded live terminal (pty + pyte) reused by both panels
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
- **Servers vs plain targets.** A target with a non-empty `ssh_user` is a
  *server* (`Target.server`): for it, `port` is the SSH port and availability is
  measured by `tcp_ping_banner` (connect + read the SSH banner) so a DDoSed box
  that only completes the TCP handshake reads DOWN, not a false UP. The reported
  latency is the *connect* RTT, not time-to-banner (sshd can delay the banner via
  reverse DNS / GSSAPI, which would otherwise look like huge latency). Plain targets
  keep `tcp_ping` (connect-only) — do not switch them to the banner probe, since
  HTTP(S) hosts send nothing unprompted and would read DOWN.
- **Embedded terminals.** `terminal.py` runs ssh/htop in a real pty and renders
  it via `pyte` (cursor drawn as a reverse cell; arrows honour DECCKM). One
  `TerminalPane` lives in each panel (`#left-term` shell, `#right-term` top).
  **Both panes can be live at once** (SSH left + top right). A focused live pane
  forwards every key to the remote **except `←/→`**, which it lets bubble so the
  app switches panels mid-session (`↑/↓` still reach the remote — htop/shell
  history work). Hence `action_focus_left/right` have no "is a pane live" guard
  and `_update_focus` focuses the live console on the chosen side; the per-side
  launch guards live in `action_login`/`action_top`. The guaranteed way out is
  `Ctrl-]`, a **priority** app binding (`action_detach`, exits the focused
  panel's console) so it fires even though the pane swallows other keys — a
  `.term-hint` bar advertises it. On the dashboard, `←/→` bubble past
  `NavDataTable`/`NavScroll` (their `cursor_left/right` / `scroll_left/right`
  disabled via `check_action`). `_start_terminal` runs via `_run_terminal`,
  which surfaces a spawn failure as a notification instead of crashing.
- **Promote-on-login.** `Enter`/`l` on a target that isn't a server yet opens
  `SshSetupScreen` (asks SSH user + port, default 22), which calls
  `_promote_to_server` — saves `ssh_user`/`port`, re-keys the row and restarts
  the probe on the SSH port — then proceeds. So any target is loggable without a
  separate edit step; don't reintroduce a hard "not a server" refusal.
- **Diagnostics menu (`l`).** `DIAG_MENU` holds the built-in `{key,label,tui,cmd}`
  entries; `_diag_entries()` appends one `tool:<name>` entry per
  `cfg.custom_tools` and a final `add_tool` action, and `DiagnosticsScreen`
  (`OptionList`) renders the lot. `tui` entries exec a full-screen program; the
  rest are one-shot reports `_diag_command` wraps in `( … ) 2>&1 | less -R`. Every
  built-in `cmd` must use near-universal tools (coreutils/util-linux/procps/
  iproute2/systemd) with graceful fallbacks — servers are mostly minimal Linux.
  `add_tool` opens `CustomToolScreen` (type a tool + Install&run / Run only);
  `_custom_tool_command` shell-quotes it and, when installing, picks the server's
  package manager (apt/dnf/yum/apk/pacman/zypper, sudo). Added tools persist in
  `cfg.custom_tools` so they stay in the menu; selecting a saved one re-installs
  if missing. Don't hard-code htop/atop back in — that was deliberately replaced
  by the user-driven tool list.
- **Live Server load.** For a selected server, `_refresh_selected_load` polls
  `fetch_server_load` (SSH `BatchMode=yes`, key/agent only) every ~8s and
  `_server_load_block` renders load-vs-cores, top CPU/MEM procs, disk-wait, fs &
  mem into `#detail-load`. Only the selected server is polled; no agent → a hint,
  not an error. Keep the remote snapshot command (`netutil._LOAD_CMD`) portable.
- **traceroute is platform-aware.** macOS uses TCP probes to the open port
  (setuid root, no sudo); Linux uses TCP only when run as root, else unprivileged
  UDP; the port comes from the target. Don't make Linux default to TCP/ICMP — it
  needs privilege there.
- **Key map:** `Enter` SSH login (left panel), `l` remote diagnostics menu (right
  panel), `t` traceroute, `Shift+←/→` switch panel focus, `Ctrl-]` exit a console
  (priority binding), `q` quit (only when no console is focused). Keep these in
  sync with the README and the detail-panel hint text.

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
