# SSH panels and honest availability — design

Date: 2026-06-30
Status: approved (pending spec review)
Component: pingmon (PyPI `pingmonitor`, command `pingmon`)

## Problem

Three connected gaps in the current TUI:

1. **False "available".** `pinger.tcp_ping` only completes a TCP connect
   (SYN → SYN/ACK → ACK). A server under a DDoS still returns SYN/ACK at the
   network edge, so pingmon reports it UP even though the real service (SSH) is
   dead and the user cannot log in. Example reported host: `203.0.113.10`.
2. **No way to act on a target.** The user wants to log in to their own server
   straight from the dashboard.
3. **No remote system view.** The user wants a live `htop`/`atop`/`top` for the
   selected server without leaving pingmon.

## Goals

- Availability of a user's own server reflects whether SSH is actually reachable,
  not just whether the TCP edge answers.
- `Enter` on a server row opens a live SSH shell embedded in the **left** panel.
- `l` opens a live `htop`/`atop`/`top` embedded in the **right** panel, with a
  first-run chooser and a way to switch tools.
- `traceroute` moves from `Enter` to `t`.
- Panel focus switches with `←` / `→`; live terminals exit with their own `q`
  (htop/top/atop) plus a universal `ctrl+]` detach.

## Non-goals

- Storing SSH passwords. Passwords are typed into ssh's own prompt inside the
  pane. No `sshpass` dependency.
- Pixel-perfect terminal emulation (mouse reporting, every box-drawing glyph).
- A custom VT parser. We use `pyte`.

## Decisions (from brainstorming)

- Availability for servers = TCP connect **plus** reading the SSH banner.
- SSH auth: dialog captures the username only; ssh prompts for the password
  inside the live pane when no agent is configured.
- Live terminals are embedded **inside** the panels (not full-screen suspend),
  for both the SSH shell (left) and the top viewer (right), using one reusable
  `TerminalPane` widget.
- Implementation uses `pyte` — accepted as a **second** runtime dependency,
  overriding the previous "single dependency" stance. CLAUDE.md/README updated.

## Data model — `config.py`

`Target` gains two optional fields:

- `ssh_user: str = ""` — the **marker for "my server"**: a non-empty value makes
  the target a server.
- `top_tool: str = ""` — remembered viewer (`htop` | `atop` | `top`); empty =
  ask on first `l`.

For a server, the existing `port` field is treated as the **SSH port**
(default 22). New property `Target.server` returns `bool(ssh_user)`.

`save_config` / `load_config` serialise and parse `ssh_user` and `top_tool`
(defaults preserve backward compatibility with existing config files).
`TargetFormScreen` gains an "SSH user (blank = plain monitor)" input; the port
placeholder hints "22 for SSH" so a server is added with one form.

## Honest availability — `pinger.py`

New coroutine `tcp_ping_banner(host, port, timeout)`:

1. `open_connection(host, port)` within `timeout`.
2. Read at least one byte (`reader.read(1)` / first line) within the remaining
   budget.
3. Return latency **to the first banner byte**, or `None` if the connect failed
   **or** no banner arrived in time.

`PingMonApp._ping_loop` selects the probe per target:
`tcp_ping_banner` when `mon.target.server` else `tcp_ping`. Regular speedtest
targets on ports 80/443 keep plain TCP-connect (they send nothing unprompted, so
a banner read would falsely mark them down). A DDoSed server that only completes
the handshake now reads DOWN, and the latency shown is the banner round-trip.

## Embedded terminal — new `pingmon/terminal.py`

`TerminalPane(Widget)`, one reusable widget for both panels:

- `start(argv: list[str])`: `os.openpty()`, spawn the subprocess with the slave
  end as stdin/stdout/stderr and `start_new_session=True`; create
  `pyte.Screen(cols, rows)` + `pyte.ByteStream`; register the master fd with
  `loop.add_reader`; feed bytes → stream → screen; refresh throttled (~20–30 fps).
- Render: convert the pyte screen buffer (chars + fg/bg/bold/reverse SGR) into a
  Rich `Text` block.
- `on_key`: translate Textual `Key` events to bytes — printable chars, Enter,
  Backspace, Tab, arrows (CSI), `ctrl+<letter>`, F-keys — and write to the master
  fd. `ctrl+]` is intercepted as a **universal detach** (stop the session).
- Resize: on widget resize set the pty window size via
  `termios.TIOCSWINSZ` and `pyte.Screen.resize`.
- Posts `TerminalPane.Exited` when the child process exits or on detach.
- `stop()`: terminate the child, remove the reader, close fds.

Bounded unit: bytes in → screen out → one `Exited` message. No app logic inside.

## SSH helpers — `netutil.py`

- `ssh_agent_has_keys() -> bool`: true when `SSH_AUTH_SOCK` is set and
  `ssh-add -l` exits 0 with at least one key.
- `build_ssh_argv(user, host, port, remote_cmd=None) -> list[str]`:
  `["ssh", "-t", "-p", str(port), f"{user}@{host}"]` plus `[remote_cmd]` when
  given (the top tool). No remote_cmd → interactive login shell.
- Username comes from `target.ssh_user`; if empty, a small modal asks for it and
  writes it back to config. With an agent, login is non-interactive; without one,
  ssh prompts for the password inside the live pane (a real pty).

## App integration — `app.py`

**Layout.** `#left` holds `#table` (DataTable) and a hidden
`TerminalPane#left-term`; `#detail` (right) holds the detail statics and a hidden
`TerminalPane#right-term`. Only one child per panel is visible at a time.

**Panel focus.** `focused_panel ∈ {"left", "right"}`, default `"left"`. `←` / `→`
move focus between panels **when no live terminal is capturing input**; `↑` / `↓`
navigate within the focused panel. The focused panel shows a highlighted border
(tcss rule).

**Key bindings.**

| Key | Context | Action |
| --- | --- | --- |
| `enter` | left focused, server row | start SSH shell in left-term |
| `enter` | left focused, non-server | notify "add an SSH user to log in" |
| `t` | any | traceroute modal (moved off Enter) |
| `l` | server selected | tool chooser → start `ssh -t host <tool>` in right-term; remembers `top_tool`. Re-press = switch tool |
| `← / →` | no live pane | switch focused panel |
| `q` | live pane focused | forwarded to the remote tool (htop/top/atop quit on `q`) → child exits → pane restored |
| `ctrl+]` | live pane focused | universal detach (stop session) |
| `q` | no live pane | quit app (unchanged) |

When a `TerminalPane.Exited` message arrives, the pane is hidden, the
table/detail are restored, focus is returned, and polling continues. Background
pings keep running during a live session (cheap).

**Tool chooser.** A small `ModalScreen` listing `htop` / `atop` / `top`,
pre-selecting the remembered `top_tool`. Selection starts the right-term and is
saved to config. Re-pressing `l` reopens the chooser — this is the "switch
tool" function.

## Docs & release

- `pyproject.toml` deps → `["textual>=0.80", "pyte>=0.8"]`.
- `run.sh` installs `pyte` alongside textual.
- Homebrew formula in `packaging/` gains a `pyte` resource.
- README: feature list, key map, config example with `ssh_user`/`top_tool`,
  install/deps.
- CLAUDE.md: update the "single runtime dependency" statement to two, document
  `pingmon/terminal.py`, the SSH banner availability semantics, and the new key
  map.
- Version bump 1.1.0 → **1.2.0** in both `pyproject.toml` and
  `pingmon/__init__.py` (minor: new features).
- Regenerate `docs/*.svg` screenshots; verify zero flag-emoji characters.

## Risks

- `pyte` renders htop well but not perfectly (mouse, some glyphs) — acceptable.
- Local pty on macOS + `ssh -t` to a Linux remote is standard and supported.
- Key routing while a pane is live must be unambiguous: live pane swallows all
  keys except `ctrl+]`; panel-switch arrows only act when no pane is live.

## Testing

- `tcp_ping_banner`: unit test against a local server that completes the
  handshake but sends no banner (expect `None`) and one that sends a banner
  (expect a latency). 
- `Target` serialisation round-trip with `ssh_user`/`top_tool`.
- `build_ssh_argv` shape and `ssh_agent_has_keys` env handling.
- TerminalPane: smoke test that a short command (`printf hi`) renders and emits
  `Exited`.
- Manual: `py_compile`, `bash run.sh`, log in to a real server, run each tool,
  switch tools, detach with `ctrl+]`, confirm a DDoSed/closed-SSH host reads DOWN.
