from __future__ import annotations

from pathlib import Path


_PKG_DIR = Path(__file__).resolve().parent.parent / "src" / "digital_professor"
__path__ = [str(_PKG_DIR)]
