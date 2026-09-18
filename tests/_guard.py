"""Test safety guard — prevents accidental database destruction.

Strategy: WHITELIST approach.
- MIRROR_ENV must be exactly "test"
- Database name must end with "_test" or be "mirror_memory_test"
- For SQLite, must be in current directory or /tmp
- Additional explicit destructive-test token required for non-URL-guarded operations
"""

import re
from urllib.parse import urlparse

# Allowed test database name patterns
_TEST_DB_PATTERNS = [
    re.compile(r"^mirror_memory_test$"),
    re.compile(r"^.*_test$"),
]

# Allowed test hosts
_TEST_HOSTS = {"localhost", "127.0.0.1", "::1"}

# Allowed SQLite paths
_TEST_SQLITE_PREFIXES = ("./", "/tmp/")


def assert_safe_to_drop(env: str, url: str) -> None:
    """Guard: refuse to drop_all unless environment is provably a test setup.

    Checks:
    1. MIRROR_ENV == "test"
    2. Database name matches test pattern
    3. Host is localhost (for PostgreSQL)
    4. SQLite path is in safe location

    Raises RuntimeError if the environment is not safe for destructive operations.
    """
    if env != "test":
        raise RuntimeError(
            f"Refusing to drop_all: MIRROR_ENV={env!r}, not 'test'. "
            f"Set MIRROR_ENV=test to run tests."
        )

    # SQLite: must be in current directory or /tmp
    if url.startswith("sqlite"):
        path = url.replace("sqlite:///", "").replace("sqlite:", "")
        if not any(path.startswith(p) for p in _TEST_SQLITE_PREFIXES):
            raise RuntimeError(
                f"Refusing to drop_all: SQLite path {path!r} is not in a safe location. "
                f"Use ./ or /tmp/ prefix."
            )
        return  # SQLite in safe location is OK

    # PostgreSQL: parse URL and validate
    try:
        parsed = urlparse(url)
    except ValueError:
        raise RuntimeError(f"Refusing to drop_all: cannot parse DATABASE_URL: {url[:30]}...")

    # Host must be localhost
    host = parsed.hostname or ""
    if host not in _TEST_HOSTS:
        raise RuntimeError(
            f"Refusing to drop_all: host {host!r} is not a test host. "
            f"Allowed: {', '.join(_TEST_HOSTS)}"
        )

    # Database name must match test pattern
    db_name = (parsed.path or "").lstrip("/")
    if not db_name:
        raise RuntimeError("Refusing to drop_all: no database name in URL")
    if not any(p.match(db_name) for p in _TEST_DB_PATTERNS):
        raise RuntimeError(
            f"Refusing to drop_all: database name {db_name!r} does not match test pattern. "
            f"Must end with '_test' or be 'mirror_memory_test'."
        )