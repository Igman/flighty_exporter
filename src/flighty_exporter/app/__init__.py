"""The full-screen terminal application the bare command opens.

Built with Textual; it contains no pipeline, decision or export logic. Its
screens run the engine (``runner``) and draw what it reports. ``shell``
holds the application and the moves between screens; ``screens/`` holds
one module per screen. Only the command line imports this package, and only
when the application opens, so the single commands never load Textual.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Config
from .shell import FlightyApp


def run_app(config: Config, config_path: Path) -> int:
    """Run the application until the user quits; the process exit status."""
    app = FlightyApp(config, config_path)
    app.run()
    return app.return_code or 0
