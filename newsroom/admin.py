from django.contrib import admin

from .models import ActivityEvent, Article, Cluster, ContentTemplate, Draft, MonitoringTopic, Source, UserSettings


@admin.register(UserSettings)
class UserSettingsAdmin(admin.ModelAdmin):
    list_display = ("user", "default_time_window", "default_draft_length", "openai_model")
    search_fields = ("user__username", "user__email", "openai_model")


@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "owner",
        "type",
        "status",
        "category",
        "last_fetched_at",
    )
    list_filter = ("owner", "type", "status")
    search_fields = ("name", "url", "owner__username")


@admin.register(MonitoringTopic)
class MonitoringTopicAdmin(admin.ModelAdmin):
    list_display = (
        "topic",
        "owner",
        "status",
        "time_window",
        "last_run_at",
    )
    list_filter = ("owner", "status", "time_window")
    search_fields = ("topic", "keywords", "owner__username")
    filter_horizontal = ("sources",)


@admin.register(Article)
class ArticleAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "owner",
        "monitoring_topic",
        "source",
        "status",
        "is_favorite",
        "relevance_score",
        "cluster",
        "fetched_at",
    )
    list_filter = ("status", "is_favorite", "source", "monitoring_topic")
    search_fields = ("title", "url", "owner__username")
    autocomplete_fields = ("source", "monitoring_topic", "cluster")
    readonly_fields = ("relevance_score", "relevance_reason")


@admin.register(Cluster)
class ClusterAdmin(admin.ModelAdmin):
    list_display = ("title", "owner", "monitoring_topic", "current_article_count", "last_updated_at")
    list_filter = ("monitoring_topic",)
    search_fields = ("title", "owner__username")
    autocomplete_fields = ("monitoring_topic",)


@admin.register(Draft)
class DraftAdmin(admin.ModelAdmin):
    list_display = ("title", "owner", "source_article", "content_template", "updated_at")
    list_filter = ("updated_at",)
    search_fields = ("title", "owner__username")
    autocomplete_fields = ("source_article", "content_template")


@admin.register(ContentTemplate)
class ContentTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "status")
    list_filter = ("status",)
    search_fields = ("name", "description", "owner__username")


@admin.register(ActivityEvent)
class ActivityEventAdmin(admin.ModelAdmin):
    list_display = ("description", "owner", "created_at")
    list_filter = ("created_at",)
    search_fields = ("description", "owner__username")
    ordering = ("-created_at",)
