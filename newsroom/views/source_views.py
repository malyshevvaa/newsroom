from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from ..forms import SourceForm
from ..models import ActivityEvent, Article, Source
from .helpers import filter_casefold_search


def _render_sources_page(
    request,
    *,
    source_form: SourceForm | None = None,
    edit_source: Source | None = None,
    show_add_modal: bool = False,
):
    search_query = request.GET.get("q", "").strip()
    type_filter = request.GET.get("type", "")
    status_filter = request.GET.get("status", "")
    sources_queryset = (
        Source.objects.filter(owner=request.user)
        .annotate(live_articles_count=Count("articles", distinct=True))
        .prefetch_related("monitoring_topics")
    )
    source_counts = {
        "active": sources_queryset.filter(status=Source.Status.ACTIVE).count(),
        "archived": sources_queryset.filter(status=Source.Status.ARCHIVED).count(),
    }
    sources = sources_queryset
    if type_filter in {Source.SourceType.SITE, Source.SourceType.TELEGRAM}:
        sources = sources.filter(type=type_filter)
    if status_filter == Source.Status.ARCHIVED:
        sources = sources.filter(status=Source.Status.ARCHIVED)
    else:
        status_filter = Source.Status.ACTIVE
        sources = sources.filter(status=Source.Status.ACTIVE)

    sources = list(sources.order_by("name"))
    if search_query:
        sources = filter_casefold_search(
            sources,
            search_query,
            ("name", "url", "category"),
        )

    paginator = Paginator(sources, 15)
    context = {
        "page_title": "Джерела",
        "page_obj": paginator.get_page(request.GET.get("page", 1)),
        "search_query": search_query,
        "type_filter": type_filter,
        "source_status_filter": status_filter,
        "source_counts": source_counts,
        "source_type_filters": [
            ("", "Усі типи"),
            (Source.SourceType.SITE, "Сайт"),
            (Source.SourceType.TELEGRAM, "Telegram"),
        ],
        "source_form": source_form or SourceForm(instance=edit_source, owner=request.user),
        "edit_source": edit_source,
        "show_add_modal": show_add_modal,
    }
    return render(request, "newsroom/sources.html", context)


@login_required
def sources_page(request):
    edit_id = request.GET.get("edit")
    edit_source = Source.objects.filter(owner=request.user, id=edit_id).first() if edit_id else None
    return _render_sources_page(
        request,
        edit_source=edit_source,
        show_add_modal=request.GET.get("modal") == "source" or bool(edit_source),
    )


@login_required
@require_POST
def save_source(request):
    source_id = request.POST.get("source_id")
    instance = Source.objects.filter(owner=request.user, id=source_id).first() if source_id else None
    form = SourceForm(request.POST, instance=instance, owner=request.user)

    if form.is_valid():
        source = form.save(commit=False)
        source.owner = request.user
        source.save()
        ActivityEvent.log(
            owner=request.user,
            description=f"Збережено джерело: {source.name}",
        )
        messages.success(request, "Джерело успішно збережено.")
        return redirect("newsroom:sources")

    return _render_sources_page(
        request,
        source_form=form,
        edit_source=instance,
        show_add_modal=True,
    )


@login_required
@require_POST
def delete_source(request, source_id: int):
    source = get_object_or_404(Source, owner=request.user, id=source_id)
    if source.status != Source.Status.ARCHIVED:
        messages.error(request, "Спочатку перенесіть джерело в архів.")
        return redirect("newsroom:sources")

    # Якщо джерело вже пов'язане з новинами, не даємо видалити його остаточно.
    # Так зберігається історія походження матеріалів у системі.
    if Article.objects.filter(owner=request.user, source=source).exists():
        messages.error(request, "Джерело вже використовується в новинах, тому його не можна видалити.")
        return redirect("newsroom:sources")

    source_name = source.name
    source.delete()
    ActivityEvent.log(
        owner=request.user,
        description=f"Видалено джерело: {source_name}",
    )
    messages.success(request, "Джерело видалено.")
    return redirect("newsroom:sources")


@login_required
@require_POST
def archive_source(request, source_id: int):
    source = get_object_or_404(Source, owner=request.user, id=source_id)
    source.status = Source.Status.ARCHIVED
    source.save(update_fields=["status"])
    ActivityEvent.log(
        owner=request.user,
        description=f"Джерело перенесено в архів: {source.name}",
    )
    messages.success(request, "Джерело перенесено в архів.")
    return redirect("newsroom:sources")


@login_required
@require_POST
def restore_source(request, source_id: int):
    source = get_object_or_404(Source, owner=request.user, id=source_id)
    source.status = Source.Status.ACTIVE
    source.save(update_fields=["status"])
    ActivityEvent.log(
        owner=request.user,
        description=f"Джерело відновлено з архіву: {source.name}",
    )
    messages.success(request, "Джерело відновлено з архіву.")
    return redirect("newsroom:sources")
