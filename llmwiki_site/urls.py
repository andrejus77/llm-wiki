from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.contrib.staticfiles.views import serve as static_serve
from django.urls import path, re_path

from llmwiki.api import api
from llmwiki import views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/login/", auth_views.LoginView.as_view(template_name="login.html"), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("api/", api.urls),
    path("assets/<path:name>", views.asset),
    # Django admin CSS (intranet app, no separate web server)
    re_path(r"^static/(?P<path>.*)$", static_serve, {"insecure": True}),
    re_path(r"^(?!api/|admin/|accounts/|assets/|static/).*$", views.spa),
]
