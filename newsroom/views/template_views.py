from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST, require_http_methods

from ..forms import ContentTemplateForm
from ..models import ActivityEvent, ContentTemplate, Draft
from .helpers import (
    TEMPLATE_USAGE_FILTERS,
    filter_casefold_search,
    normalize_template_examples,
    parse_template_examples,
    prepare_content_template_card,
    safe_redirect_target,
)


@login_required
@require_http_methods(["GET", "POST"])
def content_templates_page(request):
    search_query = request.GET.get("q", "").strip()
    current_scope = request.GET.get("scope", ContentTemplate.Status.ACTIVE)
    usage_filter = request.GET.get("usage", "")
    edit_template_id = request.GET.get("edit", "")
    page_number = request.GET.get("page", 1)

    templates_queryset = ContentTemplate.objects.filter(owner=request.user).annotate(
        usage_count=Count(
            "drafts",
            filter=Q(drafts__owner=request.user),
            distinct=True,
        )
    )
    template_stats = {
        "total": templates_queryset.count(),
        "active": templates_queryset.filter(status=ContentTemplate.Status.ACTIVE).count(),
        "archived": templates_queryset.filter(status=ContentTemplate.Status.ARCHIVED).count(),
        "used": templates_queryset.filter(usage_count__gt=0).count(),
    }

    edit_template = templates_queryset.filter(id=edit_template_id).first() if edit_template_id else None
    template_form = ContentTemplateForm(instance=edit_template, owner=request.user)
    template_examples = normalize_template_examples(edit_template.example_texts) if edit_template else []
    show_template_modal = request.GET.get("modal") == "template" or bool(edit_template)

    if request.method == "POST":
        template_id = request.POST.get("template_id")
        template_instance = templates_queryset.filter(id=template_id).first() if template_id else None
        template_form = ContentTemplateForm(request.POST, instance=template_instance, owner=request.user)
        template_examples = parse_template_examples(request.POST)
        show_template_modal = True

        if template_form.is_valid():
            content_template = template_form.save(commit=False)
            content_template.owner = request.user
            content_template.example_texts = template_examples
            if not content_template.status:
                content_template.status = ContentTemplate.Status.ACTIVE
            content_template.save()
            ActivityEvent.log(
                owner=request.user,
                description=f"Збережено шаблон контенту: {content_template.name}",
            )
            messages.success(
                request,
                "Шаблон контенту збережено." if template_instance else "Новий шаблон контенту додано.",
            )
            return redirect("newsroom:content_templates")

    filtered_templates = templates_queryset
    if current_scope == ContentTemplate.Status.ARCHIVED:
        filtered_templates = filtered_templates.filter(status=ContentTemplate.Status.ARCHIVED)
    else:
        current_scope = ContentTemplate.Status.ACTIVE
        filtered_templates = filtered_templates.filter(status=ContentTemplate.Status.ACTIVE)
    if usage_filter == "used":
        filtered_templates = filtered_templates.filter(usage_count__gt=0)
    elif usage_filter == "unused":
        filtered_templates = filtered_templates.filter(usage_count=0)

    filtered_templates = list(filtered_templates.order_by("name"))
    if search_query:
        filtered_templates = filter_casefold_search(
            filtered_templates,
            search_query,
            ("name", "description", "prompt_text"),
        )

    template_list = [
        prepare_content_template_card(template)
        for template in filtered_templates
    ]

    paginator = Paginator(template_list, 8)
    page_obj = paginator.get_page(page_number)

    context = {
        "page_title": "Шаблони контенту",
        "template_form": template_form,
        "template_examples": template_examples,
        "edit_template": edit_template,
        "show_template_modal": show_template_modal,
        "page_obj": page_obj,
        "search_query": search_query,
        "current_scope": current_scope,
        "usage_filter": usage_filter,
        "template_stats": template_stats,
        "template_usage_filters": TEMPLATE_USAGE_FILTERS,
    }
    return render(request, "newsroom/content_templates.html", context)


@login_required
@require_POST
def archive_content_template(request, template_id: int):
    content_template = get_object_or_404(ContentTemplate, owner=request.user, id=template_id)
    # Архівування прибирає шаблон із нових генерацій, але не розриває зв'язок
    # з уже створеними матеріалами.
    content_template.status = ContentTemplate.Status.ARCHIVED
    content_template.save(update_fields=["status"])
    ActivityEvent.log(
        owner=request.user,
        description=f"Шаблон контенту перенесено в архів: {content_template.name}",
    )
    messages.success(request, f"Шаблон «{content_template.name}» перенесено в архів.")
    return redirect(safe_redirect_target(request, fallback_name="content_templates"))


@login_required
@require_POST
def restore_content_template(request, template_id: int):
    content_template = get_object_or_404(ContentTemplate, owner=request.user, id=template_id)
    content_template.status = ContentTemplate.Status.ACTIVE
    content_template.save(update_fields=["status"])
    ActivityEvent.log(
        owner=request.user,
        description=f"Шаблон контенту відновлено з архіву: {content_template.name}",
    )
    messages.success(request, f"Шаблон «{content_template.name}» відновлено з архіву.")
    return redirect(safe_redirect_target(request, fallback_name="content_templates"))


@login_required
@require_POST
def delete_content_template(request, template_id: int):
    content_template = get_object_or_404(ContentTemplate, owner=request.user, id=template_id)
    if content_template.status != ContentTemplate.Status.ARCHIVED:
        messages.error(request, "Остаточно видаляти можна лише архівні шаблони.")
        return redirect(safe_redirect_target(request, fallback_name="content_templates"))

    if Draft.objects.filter(owner=request.user, content_template=content_template).exists():
        messages.error(request, "Шаблон уже використовувався в генерації, тому його не можна видалити.")
        return redirect(safe_redirect_target(request, fallback_name="content_templates"))

    template_name = content_template.name
    content_template.delete()
    ActivityEvent.log(
        owner=request.user,
        description=f"Видалено шаблон контенту: {template_name}",
    )
    messages.success(request, f"Шаблон «{template_name}» видалено.")
    return redirect(safe_redirect_target(request, fallback_name="content_templates"))
