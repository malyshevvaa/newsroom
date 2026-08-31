from __future__ import annotations

from django.db.models import Count, Q
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from ..models import ContentTemplate, MonitoringTopic

TEMPLATE_USAGE_FILTERS = [
    ("", "Усі шаблони"),
    ("used", "Використані"),
    ("unused", "Не використані"),
]


def prepare_content_template_card(template: ContentTemplate) -> ContentTemplate:
    examples = normalize_template_examples(template.example_texts)
    template.example_texts = examples
    template.examples_count = len(examples)
    template.first_example = examples[0] if examples else None
    template.is_archived = template.status == ContentTemplate.Status.ARCHIVED
    template.can_be_deleted = getattr(template, "usage_count", 0) == 0
    return template


def normalize_template_examples(raw_examples) -> list[str]:
    if isinstance(raw_examples, str):
        text = raw_examples.strip()
        return [text[:6000]] if text else []
    if not isinstance(raw_examples, list):
        return []

    examples: list[str] = []
    for item in raw_examples:
        if isinstance(item, dict):
            text = str(item.get("text", "")).strip()
        else:
            text = str(item).strip()
        if not text:
            continue
        examples.append(text[:6000])
    return examples


def parse_template_examples(post_data) -> list[str]:
    texts = post_data.getlist("example_text")
    examples: list[str] = []
    for text in texts:
        clean_text = text.strip()
        if not clean_text:
            continue
        examples.append(clean_text[:6000])
    return examples


def monitoring_topics_with_live_counts(owner):
    return MonitoringTopic.objects.filter(owner=owner).annotate(
        live_articles_count=Count(
            "articles",
            filter=Q(articles__owner=owner),
            distinct=True,
        )
    )


def get_request_next(request):
    redirect_to = request.POST.get("next") or request.GET.get("next")
    if redirect_to and url_has_allowed_host_and_scheme(
        redirect_to,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect_to
    return ""


def safe_redirect_target(request, fallback_name: str = "dashboard"):
    redirect_to = get_request_next(request)
    if redirect_to:
        return redirect_to
    return reverse(f"newsroom:{fallback_name}")


def _resolve_search_value(item, field_path: str):
    value = item
    for part in field_path.split("__"):
        if value is None:
            return ""
        value = getattr(value, part, None)
    return "" if value is None else str(value)


def filter_casefold_search(items, search_query: str, fields: tuple[str, ...] | list[str]):
    normalized_query = search_query.strip().casefold()
    if not normalized_query:
        return items

    # Для SQLite пошук по кирилиці через icontains може лишатися чутливим до регістру,
    # тому для текстового пошуку робимо явне порівняння через casefold() у Python.
    return [
        item
        for item in items
        if any(
            normalized_query in _resolve_search_value(item, field_name).casefold()
            for field_name in fields
        )
    ]
