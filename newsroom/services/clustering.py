from __future__ import annotations

import re
from collections import defaultdict

from django.contrib.auth.models import User
from django.utils import timezone
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from ..models import ActivityEvent, Article, Cluster, MonitoringTopic
from .text_tools import strip_html

# Поріг підібрано емпірично для навчального набору новин:
# менше значення надто агресивно зливає різні теми, а більше залишає
# занадто багато близьких матеріалів поза кластерами.
SIMILARITY_THRESHOLD = 0.10
TOKEN_PATTERN = re.compile(r"[0-9A-Za-zА-Яа-яІіЇїЄєҐґ']+")


def run_simple_clustering(
    *,
    owner: User,
    monitoring_topic: MonitoringTopic | None = None,
) -> dict[str, int | str]:
    dissolved_clusters = _release_single_article_clusters(
        owner=owner,
        monitoring_topic=monitoring_topic,
    )

    queryset = (
        Article.objects.filter(owner=owner, cluster__isnull=True)
        .exclude(status=Article.Status.REJECTED)
        .filter(status__in=[Article.Status.NEW, Article.Status.PROCESSED])
        .select_related("source", "monitoring_topic")
        .order_by("-fetched_at")
    )
    if monitoring_topic:
        queryset = queryset.filter(monitoring_topic=monitoring_topic)

    # Обробляємо всі доступні статті за один запуск без штучного
    # обмеження на розмір пакета, щоб кластеризація була цілісною.
    candidates = list(queryset)
    processed_articles = len(candidates)
    created_clusters = 0
    updated_cluster_ids: set[int] = set()

    clusters_queryset = Cluster.objects.filter(owner=owner).select_related("monitoring_topic").prefetch_related("articles")
    if monitoring_topic:
        clusters_queryset = clusters_queryset.filter(monitoring_topic=monitoring_topic)

    clusters_by_topic: dict[int | None, list[Cluster]] = defaultdict(list)
    for cluster in clusters_queryset:
        clusters_by_topic[cluster.monitoring_topic_id].append(cluster)

    articles_by_topic: dict[int | None, list[Article]] = defaultdict(list)
    for article in candidates:
        articles_by_topic[article.monitoring_topic_id].append(article)

    for topic_id, topic_articles in articles_by_topic.items():
        topic_clusters = clusters_by_topic.get(topic_id, [])
        created_count, touched_clusters = _cluster_topic_articles(
            owner=owner,
            articles=topic_articles,
            existing_clusters=topic_clusters,
        )
        created_clusters += created_count
        updated_cluster_ids.update(touched_clusters)

    updated_clusters = Cluster.objects.filter(owner=owner, id__in=updated_cluster_ids)
    for cluster in updated_clusters:
        sync_cluster_statistics(cluster)

    entity_title = monitoring_topic.topic if monitoring_topic else "Усі теми"
    ActivityEvent.log(
        owner=owner,
        description=(
            f"Кластеризація завершена для теми «{entity_title}»: "
            f"оброблено {processed_articles} статей, створено {created_clusters}, "
            f"оновлено {len(updated_cluster_ids)}, розформовано {dissolved_clusters} одиночних кластерів."
        ),
    )

    return {
        "articles_processed": processed_articles,
        "clusters_created": created_clusters,
        "clusters_updated": len(updated_cluster_ids),
        "single_clusters_dissolved": dissolved_clusters,
        "message": (
            f"Оброблено {processed_articles} статей. Створено {created_clusters} кластерів, "
            f"оновлено {len(updated_cluster_ids)}, розформовано {dissolved_clusters} кластерів з однією статтею."
        ),
    }


def sync_cluster_statistics(cluster: Cluster) -> None:
    articles = list(cluster.articles.filter(owner=cluster.owner).order_by("-fetched_at"))
    if articles:
        cluster.summary = _build_cluster_summary(articles[0])
        cluster.save(update_fields=["summary", "last_updated_at"])
        return

    cluster.last_updated_at = timezone.now()
    cluster.save(update_fields=["last_updated_at"])


def _release_single_article_clusters(
    *,
    owner: User,
    monitoring_topic: MonitoringTopic | None,
) -> int:
    queryset = Cluster.objects.filter(owner=owner).prefetch_related("articles")
    if monitoring_topic:
        queryset = queryset.filter(monitoring_topic=monitoring_topic)

    dissolved_clusters = 0

    for cluster in queryset:
        articles = list(cluster.articles.filter(owner=owner))
        if len(articles) > 1:
            continue

        for article in articles:
            article.cluster = None
            article.status = Article.Status.NEW
            article.save(update_fields=["cluster", "status"])

        cluster.delete()
        dissolved_clusters += 1

    return dissolved_clusters


def _cluster_topic_articles(
    *,
    owner: User,
    articles: list[Article],
    existing_clusters: list[Cluster],
) -> tuple[int, set[int]]:
    article_vectors, cluster_vectors = _build_topic_vectors(
        articles=articles,
        existing_clusters=existing_clusters,
    )
    updated_cluster_ids: set[int] = set()
    created_clusters = 0
    remaining_articles: list[Article] = []

    for article in articles:
        vector = article_vectors.get(article.id)
        if vector is None:
            _mark_article_as_processed(article)
            continue

        cluster, similarity = _find_best_cluster_match(vector=vector, cluster_vectors=cluster_vectors)
        if cluster and similarity >= SIMILARITY_THRESHOLD:
            article.cluster = cluster
            article.status = Article.Status.CLUSTERED
            article.save(update_fields=["cluster", "status"])
            updated_cluster_ids.add(cluster.id)
            continue

        remaining_articles.append(article)

    created_count, created_ids = _create_clusters_from_remaining_articles(
        owner=owner,
        articles=remaining_articles,
        article_vectors=article_vectors,
    )
    created_clusters += created_count
    updated_cluster_ids.update(created_ids)
    return created_clusters, updated_cluster_ids


def _build_topic_vectors(
    *,
    articles: list[Article],
    existing_clusters: list[Cluster],
) -> tuple[dict[int, object], dict[int, dict[str, object]]]:
    if articles:
        topic = articles[0].monitoring_topic
    elif existing_clusters:
        topic = existing_clusters[0].monitoring_topic
    else:
        topic = None

    ignored_terms = _topic_terms(topic)
    article_documents = {article.id: _build_article_document(article) for article in articles}
    cluster_documents: dict[int, str] = {}

    for cluster in existing_clusters:
        cluster_articles = list(cluster.articles.filter(owner=cluster.owner).order_by("-fetched_at"))
        if not cluster_articles:
            continue
        cluster_documents[cluster.id] = " ".join(_build_article_document(article) for article in cluster_articles)

    documents = [*article_documents.values(), *cluster_documents.values()]
    if not documents:
        return {}, {}

    vectorizer = TfidfVectorizer(
        tokenizer=lambda text: _tokenize_text(text, ignored_terms=ignored_terms),
        lowercase=True,
        token_pattern=None,
        ngram_range=(1, 2),
    )
    matrix = vectorizer.fit_transform(documents)

    article_vectors: dict[int, object] = {}
    cluster_vectors: dict[int, dict[str, object]] = {}

    current_index = 0
    for article in articles:
        article_vectors[article.id] = matrix[current_index]
        current_index += 1

    for cluster in existing_clusters:
        if cluster.id not in cluster_documents:
            continue
        cluster_vectors[cluster.id] = {
            "cluster": cluster,
            "matrix": matrix[current_index],
        }
        current_index += 1

    return article_vectors, cluster_vectors


def _find_best_cluster_match(
    *,
    vector,
    cluster_vectors: dict[int, dict[str, object]],
) -> tuple[Cluster | None, float]:
    best_cluster = None
    best_similarity = 0.0

    for cluster_state in cluster_vectors.values():
        similarity = cosine_similarity(vector, cluster_state["matrix"])[0][0]
        if similarity > best_similarity:
            best_similarity = similarity
            best_cluster = cluster_state["cluster"]

    return best_cluster, best_similarity


def _create_clusters_from_remaining_articles(
    *,
    owner: User,
    articles: list[Article],
    article_vectors: dict[int, object],
) -> tuple[int, set[int]]:
    if not articles:
        return 0, set()

    graph: dict[int, set[int]] = {article.id: set() for article in articles}

    for left_index, left_article in enumerate(articles):
        left_vector = article_vectors.get(left_article.id)
        if left_vector is None:
            continue
        for right_article in articles[left_index + 1 :]:
            right_vector = article_vectors.get(right_article.id)
            if right_vector is None:
                continue
            similarity = cosine_similarity(left_vector, right_vector)[0][0]
            if similarity >= SIMILARITY_THRESHOLD:
                graph[left_article.id].add(right_article.id)
                graph[right_article.id].add(left_article.id)

    article_by_id = {article.id: article for article in articles}
    visited: set[int] = set()
    created_clusters = 0
    created_cluster_ids: set[int] = set()

    for article in articles:
        if article.id in visited:
            continue

        component_ids = _collect_connected_component(start_id=article.id, graph=graph, visited=visited)
        component_articles = [article_by_id[article_id] for article_id in component_ids]

        if len(component_articles) < 2:
            _mark_article_as_processed(component_articles[0])
            continue

        representative = sorted(component_articles, key=lambda item: item.fetched_at, reverse=True)[0]
        cluster = Cluster.objects.create(
            owner=owner,
            monitoring_topic=representative.monitoring_topic,
            title=representative.title,
            summary=_build_cluster_summary(representative),
        )
        created_cluster_ids.add(cluster.id)
        created_clusters += 1

        for component_article in component_articles:
            component_article.cluster = cluster
            component_article.status = Article.Status.CLUSTERED
            component_article.save(update_fields=["cluster", "status"])

    return created_clusters, created_cluster_ids


def _collect_connected_component(
    *,
    start_id: int,
    graph: dict[int, set[int]],
    visited: set[int],
) -> list[int]:
    stack = [start_id]
    component: list[int] = []

    while stack:
        current_id = stack.pop()
        if current_id in visited:
            continue
        visited.add(current_id)
        component.append(current_id)
        stack.extend(neighbour_id for neighbour_id in graph[current_id] if neighbour_id not in visited)

    return component


def _build_article_document(article: Article) -> str:
    title_text = strip_html(article.title or "")
    content_text = strip_html(article.content or "")[:1600]
    return f"{title_text} {content_text}".strip()


def _tokenize_text(text: str, *, ignored_terms: set[str]) -> list[str]:
    tokens: list[str] = []
    for raw_token in TOKEN_PATTERN.findall((text or "").lower()):
        token = raw_token.strip("'")
        if len(token) < 2:
            continue
        if token.isdigit():
            continue
        if not any(char.isalpha() for char in token):
            continue
        if token in ignored_terms:
            continue
        tokens.append(token)
    return tokens


def _topic_terms(topic: MonitoringTopic | None) -> set[str]:
    if not topic:
        return set()

    raw_text = " ".join([topic.topic or "", *topic.keywords_list])
    return set(_tokenize_text(raw_text, ignored_terms=set()))


def _build_cluster_summary(article: Article, limit: int = 320) -> str:
    # Для опису кластера беремо короткий уривок зі статті,
    # а не весь текст, щоб картки і сторінка деталей залишалися читабельними.
    text = " ".join(strip_html(article.content or "").split())
    if not text:
        return article.title or ""
    if len(text) <= limit:
        return text
    shortened = text[:limit].rsplit(" ", 1)[0].strip()
    return f"{shortened}..."


def _mark_article_as_processed(article: Article) -> None:
    article.cluster = None
    article.status = Article.Status.PROCESSED
    article.save(update_fields=["cluster", "status"])

