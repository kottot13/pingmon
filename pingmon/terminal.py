"""Embedded live terminal widget backed by a pty + pyte VT emulator.

`TerminalPane` runs a child process (an SSH shell, or `ssh -t host htop`) in a
pseudo-terminal, feeds its output through a `pyte` screen buffer, renders that
buffer into the Textual layout, and forwards key presses back to the child. One
instance lives in the left panel (login shell) and one in the right (top viewer).
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import signal
import struct
import termios

import pyte
from rich.style import Style
from rich.text import Text
from textual import events
from textual.message import Message
from textual.widget import Widget

# pyte names colours by word; map them to the app's palette. 256-colour and
# truecolour cells arrive already as 6-hex strings and are used verbatim.
_COLORS = {
    "black": "#1a1a24", "red": "#ff3860", "green": "#3ddc84",
    "brown": "#f4bf4f", "blue": "#7aa2f7", "magenta": "#c9a0dc",
    "cyan": "#56c2d6", "white": "#e0e0f0",
}
_BRIGHT = {
    "black": "#6a6a7c", "red": "#ff6f91", "green": "#a6e22e",
    "brown": "#ffd479", "blue": "#9db4ff", "magenta": "#d9b8e8",
    "cyan": "#7ad6e6", "white": "#ffffff",
}

# Arrow keys: (CSI form, SS3 form). SS3 is sent when the remote enabled DECCKM.
_ARROWS = {
    "up": (b"\x1b[A", b"\x1bOA"), "down": (b"\x1b[B", b"\x1bOB"),
    "right": (b"\x1b[C", b"\x1bOC"), "left": (b"\x1b[D", b"\x1bOD"),
}

# Special keys → byte sequences a terminal program expects.
_KEYS = {
    "enter": b"\r", "backspace": b"\x7f", "tab": b"\t", "escape": b"\x1b",
    "space": b" ", "home": b"\x1b[H", "end": b"\x1b[F",
    "pageup": b"\x1b[5~", "pagedown": b"\x1b[6~", "insert": b"\x1b[2~",
    "delete": b"\x1b[3~",
    "f1": b"\x1bOP", "f2": b"\x1bOQ", "f3": b"\x1bOR", "f4": b"\x1bOS",
    "f5": b"\x1b[15~", "f6": b"\x1b[17~", "f7": b"\x1b[18~", "f8": b"\x1b[19~",
    "f9": b"\x1b[20~", "f10": b"\x1b[21~", "f11": b"\x1b[23~", "f12": b"\x1b[24~",
}


def _color(name: str, *, bold: bool) -> str | None:
    if name == "default":
        return None
    if name in _COLORS:
        return (_BRIGHT if bold else _COLORS)[name]
    if len(name) == 6:  # pyte 256/truecolour → bare hex
        try:
            int(name, 16)
            return "#" + name
        except ValueError:
            return None
    return None


class TerminalPane(Widget):
    """A focusable widget that mirrors a child process running in a pty."""

    can_focus = True
    DEFAULT_CSS = "TerminalPane { display: none; }"

    class Exited(Message):
        """Posted when the child process ends or the user detaches.

        ``exit_code`` is the child's exit status (None if it couldn't be read,
        e.g. on a forced detach). ssh reports 255 on connection/auth failure,
        which the app uses to reopen the SSH setup dialog.
        """

        def __init__(self, pane: "TerminalPane", exit_code: int | None) -> None:
            self.pane = pane
            self.exit_code = exit_code
            super().__init__()

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._proc: asyncio.subprocess.Process | None = None
        self._master_fd: int | None = None
        self._screen: pyte.Screen | None = None
        self._stream: pyte.ByteStream | None = None
        self._dirty = False
        self._refresher = None
        self._waiter = None

    # ---------- lifecycle ----------

    @property
    def running(self) -> bool:
        return self._proc is not None

    async def start(self, argv: list[str]) -> None:
        """Spawn `argv` in a fresh pty and begin mirroring it."""
        if self.running:
            return
        cols = max(20, self.size.width or 80)
        rows = max(5, self.size.height or 24)
        self._screen = pyte.Screen(cols, rows)
        self._stream = pyte.ByteStream(self._screen)

        master, slave = os.openpty()
        self._set_winsize(master, rows, cols)

        def _preexec() -> None:
            try:  # make the slave our controlling terminal so Ctrl-C works
                fcntl.ioctl(0, termios.TIOCSCTTY, 0)
            except OSError:
                pass

        self.styles.display = "block"
        env = dict(os.environ, TERM="xterm-256color")
        self._proc = await asyncio.create_subprocess_exec(
            *argv, stdin=slave, stdout=slave, stderr=slave,
            start_new_session=True, preexec_fn=_preexec, env=env,
        )
        os.close(slave)
        self._master_fd = master
        os.set_blocking(master, False)

        loop = asyncio.get_running_loop()
        loop.add_reader(master, self._on_readable)
        self._refresher = self.set_interval(1 / 30, self._tick)
        self._waiter = asyncio.ensure_future(self._wait_child())
        self.focus()

    def _set_winsize(self, fd: int, rows: int, cols: int) -> None:
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def _on_readable(self) -> None:
        assert self._master_fd is not None
        try:
            data = os.read(self._master_fd, 65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        if not data:  # EOF: child closed the pty
            self._teardown_io()
            return
        try:
            self._stream.feed(data)
        except Exception:
            pass
        self._dirty = True

    async def _wait_child(self) -> None:
        if self._proc is not None:
            try:
                await self._proc.wait()
            except Exception:
                pass
        self._finish()

    def _tick(self) -> None:
        if self._dirty:
            self._dirty = False
            self.refresh()

    def _teardown_io(self) -> None:
        if self._master_fd is not None:
            try:
                asyncio.get_running_loop().remove_reader(self._master_fd)
            except Exception:
                pass
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None

    def _finish(self) -> None:
        """Tear down everything and tell the app the session is over (once)."""
        if self._proc is None and self._master_fd is None:
            return
        exit_code = self._proc.returncode if self._proc is not None else None
        self._teardown_io()
        if self._refresher is not None:
            self._refresher.stop()
            self._refresher = None
        self._proc = None
        self._screen = None
        self._stream = None
        self.styles.display = "none"
        self.post_message(self.Exited(self, exit_code))

    def stop(self) -> None:
        """Terminate the child (used for ctrl+] detach / app shutdown)."""
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
        # _wait_child → _finish handles the rest once the process is reaped.
        self._finish()

    # ---------- input ----------

    def on_key(self, event: events.Key) -> None:
        # Shift+←/→ switch panels at the app level, even while this console is
        # live — let them bubble. Plain arrows go to the remote, so editors and
        # htop keep working normally.
        if event.key in ("shift+left", "shift+right"):
            return
        event.stop()
        event.prevent_default()
        if self._master_fd is None:
            return
        if event.key == "ctrl+right_square_bracket":  # universal detach
            self.stop()
            return
        data = self._encode(event)
        if data:
            try:
                os.write(self._master_fd, data)
            except OSError:
                pass

    def _encode(self, event: events.Key) -> bytes:
        key = event.key
        if key in _ARROWS:
            # honour application-cursor-keys mode (DECCKM): apps like vim/htop
            # switch arrows from CSI (ESC [ X) to SS3 (ESC O X).
            csi, ss3 = _ARROWS[key]
            return ss3 if self._app_cursor_keys() else csi
        if key in _KEYS:
            return _KEYS[key]
        if key.startswith("ctrl+") and len(key) == 6 and key[5].isalpha():
            return bytes([ord(key[5].upper()) & 0x1F])
        if event.character:
            return event.character.encode("utf-8", "replace")
        return b""

    def _app_cursor_keys(self) -> bool:
        screen = self._screen
        if screen is None:
            return False
        try:
            from pyte import modes
            return modes.DECCKM in screen.mode
        except Exception:
            return False

    # ---------- resize & render ----------

    def on_resize(self, event: events.Resize) -> None:
        if self._screen is None or self._master_fd is None:
            return
        cols = max(20, event.size.width)
        rows = max(5, event.size.height)
        try:
            self._screen.resize(rows, cols)
            self._set_winsize(self._master_fd, rows, cols)
        except Exception:
            return
        self._dirty = True

    def render(self) -> Text:
        try:
            return self._render_screen()
        except Exception:  # never let a render glitch crash the whole app
            return Text("")

    def _render_screen(self) -> Text:
        screen = self._screen
        if screen is None:
            return Text("")
        out = Text()
        buf = screen.buffer
        cur = screen.cursor
        show_cursor = not getattr(cur, "hidden", False)
        for y in range(screen.lines):
            line = buf[y]
            run = ""
            run_style: Style | None = None
            for x in range(screen.columns):
                ch = line[x]
                fg = _color(ch.fg, bold=ch.bold)
                bg = _color(ch.bg, bold=False)
                # draw the cursor as an inverted cell so you can see where you are
                is_cursor = show_cursor and x == cur.x and y == cur.y
                style = Style(
                    color=fg, bgcolor=bg, bold=ch.bold,
                    italic=ch.italics, underline=ch.underscore,
                    reverse=(ch.reverse != is_cursor),
                )
                data = ch.data or " "
                if style == run_style:
                    run += data
                else:
                    if run:
                        out.append(run, run_style)
                    run, run_style = data, style
            if run:
                out.append(run, run_style)
            if y != screen.lines - 1:
                out.append("\n")
        return out
