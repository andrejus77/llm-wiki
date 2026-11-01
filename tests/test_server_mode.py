"""Server mode: every page and API call needs a Django login; local mode needs none."""
from django.contrib.auth.models import User
from django.test import Client

from llmwiki.conf import get_settings, invalidate, save_settings


def test_login_required_in_server_mode():
    s = get_settings().model_copy(deep=True)
    s.server.mode = "server"
    save_settings(s, user="test", note="server mode test")
    invalidate()
    c = Client()
    assert c.get("/api/meta").status_code == 401
    assert c.get("/wiki").status_code == 302                     # redirected to the login page
    assert c.get("/accounts/login/").status_code == 200
    assert c.get("/api/health").status_code == 200               # health stays open for monitoring
    User.objects.create_user("demo", password="pw-12345")
    assert c.login(username="demo", password="pw-12345")
    r = c.get("/api/meta")
    assert r.status_code == 200 and r.json()["user"] == "demo"
    s.server.mode = "local"
    save_settings(s, user="test", note="back to local")
    invalidate()
