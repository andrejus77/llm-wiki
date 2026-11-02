"""Django settings. Application settings (endpoints, budgets, schedules...) are NOT here:
they live in the Pydantic model `llmwiki.conf.Settings` and are edited in the portal.

Everything the app stores goes into one SQLite file in the data folder (LLMWIKI_DATA, default ./data).
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("LLMWIKI_DATA", BASE_DIR / "data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
(DATA_DIR / "logs").mkdir(exist_ok=True)

def _secret() -> str:
    """LLMWIKI_SECRET_KEY, else a random key created once in data/secret.txt."""
    if os.environ.get("LLMWIKI_SECRET_KEY"):
        return os.environ["LLMWIKI_SECRET_KEY"]
    f = DATA_DIR / "secret.txt"
    if not f.exists():
        import secrets
        f.write_text(secrets.token_urlsafe(48), encoding="utf-8")
    return f.read_text(encoding="utf-8").strip()


SECRET_KEY = _secret()
DEBUG = os.environ.get("LLMWIKI_DEBUG", "0") == "1"
ALLOWED_HOSTS = ["*"]
CSRF_TRUSTED_ORIGINS = [o for o in os.environ.get("LLMWIKI_CSRF_ORIGINS", "").split(",") if o]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "llmwiki.apps.LlmWikiConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "llmwiki.middleware.LoginRequiredInServerMode",
]

ROOT_URLCONF = "llmwiki_site.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "llmwiki" / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]
WSGI_APPLICATION = "llmwiki_site.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("LLMWIKI_DB", str(DATA_DIR / "llmwiki.sqlite3")),
        "OPTIONS": {
            # WAL lets the web threads read while the worker threads write.
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA busy_timeout=30000; PRAGMA cache_size=-32000;",
            "transaction_mode": "IMMEDIATE",
            "timeout": 30,
        },
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = DATA_DIR / "static"
FRONTEND_DIST = BASE_DIR / "frontend" / "dist"
STATICFILES_DIRS = [FRONTEND_DIST / "assets"] if (FRONTEND_DIST / "assets").exists() else []

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
DATA_UPLOAD_MAX_MEMORY_SIZE = 50 * 1024 * 1024

from llmwiki.logs import django_logging_config  # noqa: E402

LOGGING = django_logging_config(DATA_DIR / "logs", os.environ.get("LLMWIKI_LOG_LEVEL", "INFO"))
