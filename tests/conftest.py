import os
import secrets

# Settings are frozen and cached, so the environment must be complete before
# the first import of anything that calls load_settings().
os.environ.setdefault("MMP_ENVIRONMENT", "dev")
os.environ.setdefault("MMP_SHUTDOWN_GRACE_SECONDS", "0")
os.environ.setdefault("MMP_DATABASE_URL", "postgresql+asyncpg://postgres@127.0.0.1:5432/mmp_test")
os.environ.setdefault("MMP_REDIS_URL", "redis://127.0.0.1:6379/1")
for var in ("MMP_API_KEY_PEPPER", "MMP_IP_HASH_PEPPER", "MMP_SESSION_SECRET"):
    os.environ.setdefault(var, secrets.token_hex(32))


# Database fixtures live in their own module for readability.
pytest_plugins = ["tests.conftest_db", "tests.conftest_api", "tests.conftest_ingest"]
