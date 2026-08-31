from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from ..forms import MonitoringTopicForm
from ..models import ActivityEvent, Article, MonitoringTopic, Source
from ..session_state import get_current_member, get_user_settings
from ..services.fetcher import get_topic_monitoring_sources
from ..services.monitoring_tasks import request_monitoring_stop, reset_stale_monitoring_runs, start_monitoring_in_background
from .helpers import filter_casefold_search, monitoring_topics_with_live_counts


def _prepare_monitoring_topics(topics, *, active_sources_count: int):
    prepared_topics = sorted(topics, key=lambda topic: (topic.topic or "").casefold())
    for topic in prepared_topics:
        active_topic_sources = list(topic.sources.filter(status=Source.Status.ACTIVE).order_by("name"))
        topic.display_sources = active_topic_sources[:5]
        topic.uses_all_active_sources = not active_topic_sources and active_sources_count > 0
        topic.has_effective_sources = get_topic_monitoring_sources(topic).exists()
    return prepared_topics


def _render_monitoring_page(
    request,
    *,
    monitoring_form: MonitoringTopicForm | None = None,
    edit_topic: MonitoringTopic | None = None,
    show_topic_modal: bool = False,
):
    # Перед рендерингом сторінки оновлюємо стан тем після фонових daemon-потоків.
    # Якщо потік завершився або був обірваний, тема не повинна назавжди
    # лишатися в стані виконання, інакше інтерфейс покаже некоректний статус.
    reset_stale_monitoring_runs(owner=request.user)
    user_settings = get_user_settings(request.user)
    search_query = request.GET.get("q", "").strip()
    status_filter = request.GET.get("status", "")
    topics_queryset = monitoring_topics_with_live_counts(request.user).prefetch_related("sources")
    topic_counts = {
        "all": topics_queryset.count(),
        "active": topics_queryset.filter(status=MonitoringTopic.Status.ACTIVE).count(),
        "archived": topics_queryset.filter(status=MonitoringTopic.Status.ARCHIVED).count(),
    }
    active_sources_count = Source.objects.filter(owner=request.user, status=Source.Status.ACTIVE).count()
    topics = topics_queryset
    if status_filter == MonitoringTopic.Status.ARCHIVED:
        topics = topics.filter(status=status_filter)
    else:
        status_filter = MonitoringTopic.Status.ACTIVE
        topics = topics.filter(status=MonitoringTopic.Status.ACTIVE)

    topics = list(topics)
    if search_query:
        topics = filter_casefold_search(topics, search_query, ("topic",))

    if monitoring_form is None:
        monitoring_form = MonitoringTopicForm(
            instance=edit_topic,
            owner=request.user,
            user_settings=user_settings,
        )

    context = {
        "page_title": "Теми моніторингу",
        "topics": _prepare_monitoring_topics(topics, active_sources_count=active_sources_count),
        "search_query": search_query,
        "status_filter": status_filter,
        "topic_counts": topic_counts,
        "active_sources_count": active_sources_count,
        "monitoring_form": monitoring_form,
        "selected_time_window": monitoring_form["time_window"].value() or monitoring_form.fields["time_window"].initial,
        "selected_status": monitoring_form["status"].value() or MonitoringTopic.Status.ACTIVE,
        "edit_topic": edit_topic,
        "show_topic_modal": show_topic_modal,
        "has_running_topics": MonitoringTopic.objects.filter(
            owner=request.user,
            run_state=MonitoringTopic.RunState.RUNNING,
        ).exists(),
    }
    return render(request, "newsroom/monitoring.html", context)


@login_required
def monitoring_page(request):
    edit_id = request.GET.get("edit")
    edit_topic = (
        MonitoringTopic.objects.filter(owner=request.user)
        .prefetch_related("sources")
        .filter(id=edit_id)
        .first()
        if edit_id
        else None
    )

    if edit_topic and edit_topic.is_running:
        messages.error(request, "Поки моніторинг виконується, тему не можна редагувати.")
        return redirect("newsroom:monitoring")

    return _render_monitoring_page(
        request,
        edit_topic=edit_topic,
        show_topic_modal=request.GET.get("modal") == "topic" or bool(edit_topic),
    )


@login_required
@require_POST
def save_monitoring_topic(request):
    topic_id = request.POST.get("topic_id")
    instance = MonitoringTopic.objects.filter(owner=request.user, id=topic_id).first() if topic_id else None

    if instance and instance.is_running:
        messages.error(request, "Поки моніторинг виконується, тему не можна редагувати.")
        return redirect("newsroom:monitoring")

    form = MonitoringTopicForm(
        request.POST,
        instance=instance,
        owner=request.user,
        user_settings=get_user_settings(request.user),
    )

    if form.is_valid():
        topic = form.save(commit=False)
        topic.owner = request.user
        topic.save()
        form.save_m2m()
        ActivityEvent.log(
            owner=request.user,
            description=f"Збережено тему моніторингу: {topic.topic}",
        )
        messages.success(request, "Налаштування теми моніторингу збережено.")
        return redirect("newsroom:monitoring")

    return _render_monitoring_page(
        request,
        monitoring_form=form,
        edit_topic=instance,
        show_topic_modal=True,
    )


@login_required
@require_POST
def run_monitoring(request, topic_id: int):
    topic = get_object_or_404(MonitoringTopic, owner=request.user, id=topic_id)
    actor = get_current_member(request)
    if topic.status != MonitoringTopic.Status.ACTIVE:
        messages.error(request, "Запуск доступний лише для активної теми моніторингу.")
        return redirect("newsroom:monitoring")

    if topic.is_running:
        return redirect("newsroom:monitoring")

    if not get_topic_monitoring_sources(topic).exists():
        messages.error(request, "Спочатку додайте хоча б одне активне джерело для моніторингу.")
        return redirect("newsroom:monitoring")

    started = start_monitoring_in_background(topic, actor=actor)
    if not started:
        messages.error(request, "Не вдалося запустити моніторинг. Спробуйте ще раз.")
        return redirect("newsroom:monitoring")

    messages.success(request, "Моніторинг запущено у фоновому режимі. Можна далі працювати в системі.")
    return redirect("newsroom:monitoring")


@login_required
@require_POST
def stop_monitoring(request, topic_id: int):
    topic = get_object_or_404(MonitoringTopic, owner=request.user, id=topic_id)

    if not topic.is_running:
        return redirect("newsroom:monitoring")

    if request_monitoring_stop(topic):
        messages.success(request, "Моніторинг буде зупинено після завершення поточного кроку.")
    return redirect("newsroom:monitoring")


@login_required
@require_POST
def delete_monitoring_topic(request, topic_id: int):
    topic = get_object_or_404(MonitoringTopic, owner=request.user, id=topic_id)
    if topic.status != MonitoringTopic.Status.ARCHIVED:
        messages.error(request, "Остаточно видаляти можна лише архівні теми.")
        return redirect("newsroom:monitoring")

    # Якщо тема вже має пов'язані матеріали, остаточно видаляти її не дозволяємо,
    # щоб не залишити новини без прив'язки до теми.
    if Article.objects.filter(owner=request.user, monitoring_topic=topic).exists():
        messages.error(request, "Тема вже містить матеріали, тому її не можна видалити.")
        return redirect("newsroom:monitoring")

    topic_name = topic.topic
    topic.delete()
    ActivityEvent.log(
        owner=request.user,
        description=f"Видалено тему моніторингу: {topic_name}",
    )
    messages.success(request, "Архівну тему моніторингу видалено назавжди.")
    return redirect("newsroom:monitoring")


@login_required
@require_POST
def archive_monitoring_topic(request, topic_id: int):
    topic = get_object_or_404(MonitoringTopic, owner=request.user, id=topic_id)
    if topic.is_running:
        messages.error(request, "Спочатку зупиніть моніторинг цієї теми.")
        return redirect("newsroom:monitoring")

    # Архівація зберігає тему та всі її прив'язки в системі, але прибирає її з активного списку.
    topic.status = MonitoringTopic.Status.ARCHIVED
    topic.save(update_fields=["status"])
    ActivityEvent.log(
        owner=request.user,
        description=f"Тему моніторингу перенесено в архів: {topic.topic}",
    )
    messages.success(request, "Тему перенесено в архів.")
    return redirect("newsroom:monitoring")


@login_required
@require_POST
def restore_monitoring_topic(request, topic_id: int):
    topic = get_object_or_404(MonitoringTopic, owner=request.user, id=topic_id)
    topic.status = MonitoringTopic.Status.ACTIVE
    topic.save(update_fields=["status"])
    ActivityEvent.log(
        owner=request.user,
        description=f"Тему моніторингу відновлено з архіву: {topic.topic}",
    )
    messages.success(request, "Тему відновлено з архіву.")
    return redirect("newsroom:monitoring")
