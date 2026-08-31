from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from ..models import ActivityEvent, Article, Cluster, MonitoringTopic
from ..services.clustering import run_simple_clustering
from .helpers import get_request_next


@login_required
@require_GET
def clusters_page(request):
    topic_id = request.GET.get("topic", "")
    page_number = request.GET.get("page", 1)
    queryset = (
        Cluster.objects.filter(owner=request.user)
        .select_related("monitoring_topic")
        .annotate(live_article_count=Count("articles", distinct=True))
        .order_by("-last_updated_at")
    )
    if topic_id:
        queryset = queryset.filter(monitoring_topic_id=topic_id)

    paginator = Paginator(queryset, 8)
    context = {
        "page_title": "Кластери",
        "topic_filter": topic_id,
        "topic_filters": MonitoringTopic.objects.filter(owner=request.user).order_by("topic"),
        "current_topic": MonitoringTopic.objects.filter(owner=request.user, id=topic_id).first() if topic_id else None,
        "page_obj": paginator.get_page(page_number),
    }
    return render(request, "newsroom/clusters.html", context)


@login_required
@require_POST
def run_deduplication(request):
    topic_id = request.POST.get("topic", "")
    monitoring_topic = MonitoringTopic.objects.filter(owner=request.user, id=topic_id).first() if topic_id else None
    result = run_simple_clustering(owner=request.user, monitoring_topic=monitoring_topic)
    messages.success(request, result["message"])

    if monitoring_topic:
        return redirect(f"{reverse('newsroom:clusters')}?topic={monitoring_topic.id}")
    return redirect("newsroom:clusters")


@login_required
@require_POST
def delete_cluster(request, cluster_id: int):
    cluster = get_object_or_404(
        Cluster.objects.filter(owner=request.user).prefetch_related("articles"),
        id=cluster_id,
    )
    cluster_title = cluster.title
    topic_id = cluster.monitoring_topic_id
    reassigned_articles = Article.objects.filter(owner=request.user, cluster=cluster)
    affected_count = reassigned_articles.count()

    # Після видалення кластера новини залишаються в системі й можуть бути
    # повторно кластеризовані, тому повертаємо їх у стан "Нове".
    reassigned_articles.update(cluster=None, status=Article.Status.NEW)
    cluster.delete()

    ActivityEvent.log(
        owner=request.user,
        description=f"Видалено кластер: {cluster_title}",
    )
    messages.success(request, f"Кластер видалено. Статей повернуто в список: {affected_count}.")

    next_target = get_request_next(request)
    detail_path = reverse("newsroom:cluster_detail", args=[cluster_id])
    if next_target and detail_path not in next_target:
        return redirect(next_target)
    if topic_id:
        return redirect(f"{reverse('newsroom:clusters')}?topic={topic_id}")
    return redirect("newsroom:clusters")


@login_required
@require_GET
def cluster_detail(request, cluster_id: int):
    cluster = get_object_or_404(
        Cluster.objects.filter(owner=request.user)
        .annotate(live_article_count=Count("articles", distinct=True))
        .select_related("monitoring_topic")
        .prefetch_related("articles__source", "articles__monitoring_topic"),
        id=cluster_id,
    )
    context = {
        "page_title": cluster.title,
        "cluster": cluster,
    }
    return render(request, "newsroom/cluster_detail.html", context)
