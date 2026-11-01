"""Start the portal + API (Waitress) and the scheduler in one process: `manage.py serve [--host 0.0.0.0] [--port 8000] [--no-worker]`"""
import logging
import webbrowser

from django.core.management.base import BaseCommand

from llmwiki.conf import get_settings

log = logging.getLogger("llmwiki")


class Command(BaseCommand):
    help = "Run the web server and the background scheduler"

    def add_arguments(self, p):
        p.add_argument("--host")
        p.add_argument("--port", type=int)
        p.add_argument("--no-worker", action="store_true", help="do not start the scheduler (web only)")
        p.add_argument("--open", action="store_true", help="open the browser")

    def handle(self, *a, **o):
        from waitress import serve
        from django.core.management import call_command
        from llmwiki.models import Job
        from llmwiki_site.wsgi import application
        call_command("migrate", verbosity=0)
        s = get_settings()
        host = o["host"] or s.server.host
        port = o["port"] or s.server.port
        n = Job.objects.filter(status="running").update(status="queued")
        if n:
            log.info("reset %s jobs left 'running' by a previous stop", n)
        if s.schedule.worker_enabled and not o["no_worker"]:
            from llmwiki.scheduler import start_scheduler
            start_scheduler()
        url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}/"
        self.stdout.write(f"LLM Wiki on {url}  (mode: {s.server.mode}; API docs {url}api/docs)")
        if o["open"]:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        serve(application, host=host, port=port, threads=s.server.threads, channel_timeout=300, ident="llmwiki")
