"""Modal screens must be dismissible and cheap to update.

The diagnostic screen is reachable with Ctrl+D, which is also a terminal's end-of-input
key, so it can be opened by a stray byte rather than a deliberate press. A modal that
swallows every key and offers no visible way out looks exactly like a broken application.
"""

from __future__ import annotations

from pathlib import Path


async def test_mouse_diagnostic_cannot_trap_the_app(tmp_path: Path) -> None:
    """Any key closes the diagnostic screen.

    Ctrl+D is also a terminal's end-of-input key, so a stray byte can open this modal. It
    must not be possible to be stuck on it: every key except r returns to the dashboard,
    and the screen says so.
    """
    from textual.app import App

    from sideboard.ui.screens import MouseDiagnosticScreen

    class Host(App[None]):
        def on_mount(self) -> None:
            self.push_screen(MouseDiagnosticScreen(terminal="test"))

    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, MouseDiagnosticScreen)
        await pilot.press("x")
        await pilot.pause()
        assert not isinstance(app.screen, MouseDiagnosticScreen), "x must close the screen"
        app.push_screen(MouseDiagnosticScreen(terminal="test"))
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, MouseDiagnosticScreen), "escape must close it"
        app.push_screen(MouseDiagnosticScreen(terminal="test"))
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        assert isinstance(app.screen, MouseDiagnosticScreen), "r releases holds, it closes nothing"
