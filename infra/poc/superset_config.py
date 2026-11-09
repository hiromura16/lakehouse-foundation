"""Use a dedicated durable Superset database and private signing key."""

import os
from urllib.parse import quote

SECRET_KEY = os.environ.get("SUPERSET_SECRET_KEY", "")
if len(SECRET_KEY) < 32:
    raise ValueError("Generate a private Superset secret key before starting.")
password = os.environ.get("SUPERSET_DB_PASSWORD", "")
redis_password = os.environ.get("SUPERSET_REDIS_PASSWORD", "")
if not password or not redis_password:
    raise ValueError("Configure the separate Superset database/cache secrets.")
SQLALCHEMY_DATABASE_URI = (
    "postgresql+psycopg2://superset:"
    + quote(password, safe="")
    + "@superset-db:5432/superset"
)
CACHE_CONFIG = {
    "CACHE_TYPE": "RedisCache",
    "CACHE_DEFAULT_TIMEOUT": 300,
    "CACHE_KEY_PREFIX": "superset_",
    "CACHE_REDIS_URL": (
        "redis://:" + quote(redis_password, safe="") + "@superset-redis:6379/0"
    ),
}
DATA_CACHE_CONFIG = CACHE_CONFIG
SQLLAB_TIMEOUT = 180
SQL_MAX_ROW = 10000
ROW_LIMIT = 10000
WTF_CSRF_ENABLED = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
# HTTPS deployments should set secure cookies at their authenticated ingress.
SESSION_COOKIE_SECURE = False
TALISMAN_ENABLED = False
ENABLE_PROXY_FIX = False
FEATURE_FLAGS = {"ENABLE_TEMPLATE_PROCESSING": False}
