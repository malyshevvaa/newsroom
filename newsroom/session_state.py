from django.conf import settings

from .models import UserSettings


def get_current_member(request):
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return None
    return request.user


def get_actor_name(actor) -> str:
    if not actor:
        return ""
    return actor.first_name or actor.username


def get_user_settings(user):
    defaults = {
        "openai_model": getattr(settings, "OPENAI_MODEL", "gpt-4.1-mini"),
    }
    settings_obj, _ = UserSettings.objects.get_or_create(user=user, defaults=defaults)
    return settings_obj
