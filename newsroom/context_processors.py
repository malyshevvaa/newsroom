from .constants import NAV_ITEMS
from .session_state import get_actor_name, get_current_member


def _is_draft_detail_path(path: str) -> bool:
    parts = path.strip("/").split("/")
    return len(parts) == 2 and parts[0] == "drafts" and parts[1].isdigit()


def _build_navigation_items(path: str) -> list[dict]:
    navigation = []
    for item in NAV_ITEMS:
        active_exact = item.get("active_exact")
        active_prefixes = item.get("active_prefixes")

        if active_exact is not None or active_prefixes is not None:
            is_active = path in (active_exact or []) or any(path.startswith(prefix) for prefix in active_prefixes or [])
        elif item["href"] == "/":
            is_active = path == "/"
        else:
            is_active = path == item["href"] or path.startswith(item["href"])

        # Сторінку редагування конкретної чернетки теж відносимо до розділу згенерованих матеріалів.
        if item.get("active_draft_detail") and _is_draft_detail_path(path):
            is_active = True

        navigation.append({**item, "is_active": is_active})
    return navigation


def layout_context(request) -> dict:
    current_user = get_current_member(request)
    return {
        "current_user": current_user,
        "current_user_name": get_actor_name(current_user),
        "nav_items": _build_navigation_items(request.path) if request.user.is_authenticated else [],
    }
