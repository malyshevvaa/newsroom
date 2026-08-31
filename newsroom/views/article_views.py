from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import DateTimeField
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from ..models import ActivityEvent, Article, Cluster, MonitoringTopic
from ..services.clustering import sync_cluster_statistics
from .helpers import filter_casefold_search, get_request_next, safe_redirect_target


def _get_article_scope_counts(user):
    user_articles = Article.objects.filter(owner=user)
    return {
        "all": user_articles.count(),
        "favorites": user_articles.filter(is_favorite=True).count(),
    }


@login_required
@require_GET
def articles_page(request):
    scope = request.GET.get("scope", "")
    status = request.GET.get("status", "")
    topic_id = request.GET.get("topic", "")
    favorite = request.GET.get("favorite", "")
    sort = request.GET.get("sort", "")
    search_query = request.GET.get("q", "").strip()
    page_number = request.GET.get("page", 1)
    base_queryset = Article.objects.filter(owner=request.user).select_related("source", "cluster", "monitoring_topic")
    queryset = base_queryset

    # Старий параметр favorite залишаємо для сумісності з уже збереженими посиланнями.
    if favorite == "1" and not scope:
        scope = "favorites"

    if status:
        queryset = queryset.filter(status=status)
    if topic_id:
        queryset = queryset.filter(monitoring_topic_id=topic_id)
    if scope == "favorites":
        queryset = queryset.filter(is_favorite=True)
    if sort in {"newest", "oldest"}:
        # Для ручного сортування беремо дату публікації, а якщо її немає — дату отримання новини.
        queryset = queryset.annotate(
            display_date=Coalesce("published_at", "fetched_at", output_field=DateTimeField())
        )
        if sort == "oldest":
            queryset = queryset.order_by("display_date", "id")
        else:
            queryset = queryset.order_by("-display_date", "-id")
    else:
        # Без ручного сортування показуємо останні додані новини зверху,
        # щоб робочий список починався з найсвіжіших записів у системі.
        sort = ""
        queryset = queryset.order_by("-id")
    articles = list(queryset)
    if search_query:
        articles = filter_casefold_search(
            articles,
            search_query,
            ("title", "content", "source__name", "monitoring_topic__topic"),
        )

    paginator = Paginator(articles, 10)
    context = {
        "page_title": "Новини",
        "current_scope": scope or "all",
        "article_scope_counts": _get_article_scope_counts(request.user),
        "status_filter": status,
        "status_filters": ["", "new", "processed", "clustered", "rejected"],
        "search_query": search_query,
        "topic_filter": topic_id,
        "topic_filters": MonitoringTopic.objects.filter(owner=request.user).order_by("topic"),
        "sort_filter": sort,
        "page_obj": paginator.get_page(page_number),
    }
    return render(request, "newsroom/articles.html", context)


@login_required
@require_POST
def toggle_article_favorite(request, article_id: int):
    article = get_object_or_404(Article, owner=request.user, id=article_id)
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"
    article.is_favorite = not article.is_favorite
    article.save(update_fields=["is_favorite"])

    # Для AJAX-запиту повертаємо дані без redirect, щоб сторінка не прокручувалась догори.
    if is_ajax:
        article_scope_counts = _get_article_scope_counts(request.user)
        return JsonResponse(
            {
                "ok": True,
                "is_favorite": article.is_favorite,
                "button_text": "В обраному" if article.is_favorite else "В обране",
                "article_scope_counts": article_scope_counts,
                "favorite_count": article_scope_counts["favorites"],
                "message": (
                    "Новину додано в обране."
                    if article.is_favorite
                    else "Новину прибрано з обраного."
                ),
            }
        )

    messages.success(
        request,
        "Новину додано в обране." if article.is_favorite else "Новину прибрано з обраного.",
    )
    return redirect(safe_redirect_target(request, fallback_name="articles"))


@login_required
@require_POST
def approve_article_relevance(request, article_id: int):
    article = get_object_or_404(
        Article.objects.filter(owner=request.user).select_related("monitoring_topic"),
        id=article_id,
    )
    if article.status != Article.Status.REJECTED:
        return redirect(safe_redirect_target(request, fallback_name="articles"))

    # Редактор може вручну виправити рішення ШІ, якщо новина справді підходить темі.
    article.status = Article.Status.NEW
    article.relevance_score = max(article.relevance_score, 60)
    article.relevance_reason = "Схвалено редактором після ручної перевірки."
    article.manual_relevance_approved = True
    article.save(
        update_fields=[
            "status",
            "relevance_score",
            "relevance_reason",
            "manual_relevance_approved",
        ]
    )

    ActivityEvent.log(
        owner=request.user,
        description=f"Редактор схвалив новину після ШІ-відхилення: {article.title}",
    )
    messages.success(request, "Новину схвалено.")
    return redirect(safe_redirect_target(request, fallback_name="articles"))


@login_required
@require_POST
def delete_article(request, article_id: int):
    article = get_object_or_404(
        Article.objects.filter(owner=request.user).select_related("cluster", "monitoring_topic"),
        id=article_id,
    )
    article_title = article.title
    cluster_id = article.cluster_id
    topic_id = article.monitoring_topic_id
    next_target = get_request_next(request)

    article.delete()
    cluster_deleted = False
    if cluster_id:
        cluster = Cluster.objects.filter(owner=request.user, id=cluster_id).first()
        if cluster:
            if cluster.articles.filter(owner=request.user).exists():
                sync_cluster_statistics(cluster)
            else:
                cluster.delete()
                cluster_deleted = True

    ActivityEvent.log(
        owner=request.user,
        description=f"Видалено новину: {article_title}",
    )
    messages.success(request, "Новину видалено.")

    if cluster_deleted:
        detail_path = reverse("newsroom:cluster_detail", args=[cluster_id])
        if next_target and detail_path not in next_target:
            return redirect(next_target)
        if topic_id:
            return redirect(f"{reverse('newsroom:clusters')}?topic={topic_id}")
        return redirect("newsroom:clusters")
    return redirect(safe_redirect_target(request, fallback_name="articles"))
