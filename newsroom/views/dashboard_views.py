from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET

from ..models import ActivityEvent, Article, Cluster, Draft, MonitoringTopic, Source
from .helpers import monitoring_topics_with_live_counts


@login_required
@require_GET
def dashboard(request):
    yesterday = timezone.now() - timedelta(hours=24)
    topics_summary = MonitoringTopic.objects.filter(owner=request.user).aggregate(
        total_topics=Count("id"),
        active_topics=Count("id", filter=Q(status=MonitoringTopic.Status.ACTIVE)),
    )

    sources_summary = Source.objects.filter(owner=request.user).aggregate(
        total_sources=Count("id"),
        active_sources=Count("id", filter=Q(status=Source.Status.ACTIVE)),
    )

    articles_summary = Article.objects.filter(owner=request.user).aggregate(
        total_articles=Count("id"),
        articles_last_24h=Count("id", filter=Q(fetched_at__gte=yesterday)),
    )

    drafts_summary = Draft.objects.filter(owner=request.user).aggregate(
        total_drafts=Count("id"),
        generated_today=Count("id", filter=Q(generated_at__gte=yesterday)),
    )

    summary = {
        **topics_summary,
        **sources_summary,
        **articles_summary,
        "total_clusters": Cluster.objects.filter(owner=request.user).count(),
        **drafts_summary,
    }
    context = {
        "page_title": "Панель керування",
        "summary": summary,
        "monitoring_topics": monitoring_topics_with_live_counts(request.user)
        .prefetch_related("sources")
        .order_by("-last_run_at", "topic")[:5],
        # Для дашборда достатньо показувати лише останні 7 дій,
        # щоб стрічка активності лишалася короткою і читабельною.
        "activity_events": ActivityEvent.objects.filter(owner=request.user)
        .order_by("-created_at")[:7],
    }
    return render(request, "newsroom/dashboard.html", context)
