from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from digital_professor.main import main  # noqa: E402


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
