"""TLS для platform-api2.max.ru: системные корни плюс корень Минцифры."""
import os
import ssl
from functools import lru_cache
from pathlib import Path

_BUNDLED = Path(__file__).resolve().parents[3] / "certs" / "russian_trusted_root_ca.pem"


def root_ca_path() -> Path | None:
    env = os.getenv("MAX_ROOT_CA_PATH")
    if env:
        path = Path(env)
        return path if path.is_file() else None
    return _BUNDLED if _BUNDLED.is_file() else None


@lru_cache(maxsize=1)
def api_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    path = root_ca_path()
    if path is not None:
        ctx.load_verify_locations(cafile=str(path))
    return ctx
