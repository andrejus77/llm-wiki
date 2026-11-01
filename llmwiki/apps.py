from django.apps import AppConfig


class LlmWikiConfig(AppConfig):
    name = "llmwiki"
    verbose_name = "LLM Wiki"
    default_auto_field = "django.db.models.BigAutoField"
    # The scheduler/worker is started explicitly by `manage.py serve` (see llmwiki/scheduler.py),
    # never on import, so management commands and tests don't spawn background threads.
