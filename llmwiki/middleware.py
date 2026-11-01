"""In server mode every page and API call needs a Django login; in local mode (127.0.0.1) nothing does."""
from django.http import JsonResponse
from django.shortcuts import redirect

OPEN_PREFIXES = ("/accounts/", "/admin/", "/static/", "/assets/", "/api/health")


class LoginRequiredInServerMode:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from llmwiki.conf import get_settings
        try:
            server_mode = get_settings().server.mode == "server"
        except Exception:
            server_mode = False
        if server_mode and not request.user.is_authenticated and not request.path.startswith(OPEN_PREFIXES):
            if request.path.startswith("/api/"):
                return JsonResponse({"detail": "login required"}, status=401)
            return redirect(f"/accounts/login/?next={request.path}")
        return self.get_response(request)
