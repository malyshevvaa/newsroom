from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from ..forms import ProfileForm, UserPasswordChangeForm, UserSettingsForm
from ..session_state import get_current_member, get_user_settings


@login_required
@require_http_methods(["GET", "POST"])
def settings_page(request):
    current_member = get_current_member(request)
    user_settings = get_user_settings(request.user)

    profile_form = ProfileForm(instance=current_member)
    preferences_form = UserSettingsForm(instance=user_settings)

    if request.method == "POST":
        form_type = request.POST.get("form_type")

        if form_type == "profile":
            profile_form = ProfileForm(request.POST, instance=current_member)
            if profile_form.is_valid():
                profile_form.save()
                messages.success(request, "Профіль оновлено.")
                return redirect("newsroom:settings")
            messages.error(request, "Не вдалося оновити профіль.")

        if form_type == "preferences":
            preferences_form = UserSettingsForm(request.POST, instance=user_settings)
            if preferences_form.is_valid():
                preferences_form.save()
                messages.success(request, "Персональні налаштування збережено.")
                return redirect("newsroom:settings")
            messages.error(request, "Не вдалося зберегти персональні налаштування.")

    context = {
        "page_title": "Налаштування",
        "profile_form": profile_form,
        "preferences_form": preferences_form,
        "openai_api_configured": bool(settings.OPENAI_API_KEY),
    }
    return render(request, "newsroom/settings.html", context)


@login_required
def change_password_page(request):
    password_form = UserPasswordChangeForm(request.user, request.POST or None)

    if request.method == "POST":
        if password_form.is_valid():
            user = password_form.save()
            update_session_auth_hash(request, user)
            messages.success(request, "Пароль успішно змінено.")
            return redirect("newsroom:settings")
        messages.error(request, "Не вдалося змінити пароль. Перевірте введені дані.")

    context = {
        "page_title": "Зміна пароля",
        "password_form": password_form,
    }
    return render(request, "newsroom/change_password.html", context)
