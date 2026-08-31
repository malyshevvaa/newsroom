from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import login, logout
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from ..forms import LoginForm, RegistrationForm
from .helpers import safe_redirect_target


@require_http_methods(["GET", "POST"])
def login_page(request):
    if request.user.is_authenticated:
        return redirect("newsroom:dashboard")

    form = LoginForm(request.POST or None, request=request)
    if request.method == "POST" and form.is_valid():
        login(request, form.user)
        messages.success(request, "Вхід виконано успішно.")
        return redirect(safe_redirect_target(request))

    context = {
        "page_title": "Вхід",
        "auth_form": form,
        "next_url": request.GET.get("next", ""),
        "hide_app_sidebar": True,
    }
    return render(request, "newsroom/login.html", context)


@require_http_methods(["GET", "POST"])
def register_page(request):
    if request.user.is_authenticated:
        return redirect("newsroom:dashboard")

    form = RegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        messages.success(request, "Акаунт створено. Тепер усі ваші дані зберігатимуться окремо.")
        return redirect(safe_redirect_target(request))

    context = {
        "page_title": "Реєстрація",
        "auth_form": form,
        "next_url": request.GET.get("next", ""),
        "hide_app_sidebar": True,
    }
    return render(request, "newsroom/register.html", context)


@require_POST
def logout_view(request):
    logout(request)
    return redirect("newsroom:login")
