from urllib.parse import urlencode

from django import template
from django.utils import timezone
from django.utils.dateformat import format as django_format

from newsroom.constants import STATUS_BADGE_CLASSES, STATUS_LABELS

register = template.Library()


@register.filter
def status_label(value):
    return STATUS_LABELS.get(value, value)


@register.filter
def status_class(value):
    return STATUS_BADGE_CLASSES.get(value, "badge-muted")


@register.filter
def initials(value: str):
    parts = [part[:1].upper() for part in (value or "").split() if part]
    return "".join(parts[:2]) or "NA"


@register.filter
def relative_time(value):
    if not value:
        return "—"
    diff = timezone.now() - value
    minutes = int(diff.total_seconds() // 60)
    if minutes < 1:
        return "щойно"
    if minutes < 60:
        return f"{minutes} хв тому"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} год тому"
    days = hours // 24
    return f"{days} дн тому"


@register.filter
def elapsed_time(value):
    if not value:
        return "менше хвилини"
    diff = timezone.now() - value
    minutes = int(diff.total_seconds() // 60)
    if minutes < 1:
        return "менше хвилини"
    if minutes < 60:
        return f"{minutes} хв"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} год"
    days = hours // 24
    return f"{days} дн"


@register.filter
def format_datetime(value):
    if not value:
        return "—"
    return django_format(timezone.localtime(value), "d.m.Y H:i")


@register.simple_tag
def querystring(request, **kwargs):
    params = request.GET.copy()
    for key, value in kwargs.items():
        if value in [None, ""]:
            params.pop(key, None)
        else:
            params[key] = value
    encoded = urlencode(params, doseq=True)
    # Для кнопки "Усі" треба явно повертати URL без query-параметрів,
    # інакше порожній href не скидає поточний фільтр у браузері.
    return f"{request.path}?{encoded}" if encoded else request.path


@register.simple_tag
def pagination_pages(page_obj, side_count=1):
    current_page = page_obj.number
    total_pages = page_obj.paginator.num_pages
    visible_pages = {1, total_pages}

    for page_number in range(current_page - side_count, current_page + side_count + 1):
        if 1 <= page_number <= total_pages:
            visible_pages.add(page_number)

    result = []
    previous_page = None
    for page_number in sorted(visible_pages):
        if previous_page and page_number - previous_page > 1:
            result.append(None)
        result.append(page_number)
        previous_page = page_number

    return result
