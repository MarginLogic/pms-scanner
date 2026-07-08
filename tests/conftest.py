"""Shared test configuration.

Loads the scanner package via the project root so its relative imports
(``from .config import …``) resolve, then aliases each submodule under
its bare name so tests can keep using ``from batch import …`` and
``patch("batch.foo")`` without churn.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import scanner  # noqa: E402,F401
import scanner.batch  # noqa: E402,F401
import scanner.config  # noqa: E402,F401
import scanner.dashboard  # noqa: E402,F401
import scanner.machine  # noqa: E402,F401
import scanner.notify  # noqa: E402,F401
import scanner.ntp  # noqa: E402,F401
import scanner.pdf_processor  # noqa: E402,F401
import scanner.scheduler  # noqa: E402,F401
import scanner.state  # noqa: E402,F401
import scanner.uploader  # noqa: E402,F401

for _name in (
    "batch",
    "config",
    "dashboard",
    "machine",
    "notify",
    "ntp",
    "pdf_processor",
    "scheduler",
    "state",
    "uploader",
):
    sys.modules[_name] = sys.modules[f"scanner.{_name}"]
