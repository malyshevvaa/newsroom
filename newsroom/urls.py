from django.urls import path
from django.views.generic import RedirectView

from . import views

app_name = "newsroom"

urlpatterns = [
    # Auth
    path("login/", views.login_page, name="login"),
    path("register/", views.register_page, name="register"),
    path("logout/", views.logout_view, name="logout"),

    # Dashboard
    path("", views.dashboard, name="dashboard"),

    # Monitoring
    path("monitoring/", views.monitoring_page, name="monitoring"),
    path("monitoring/save/", views.save_monitoring_topic, name="save_monitoring_topic"),
    path("monitoring/<int:topic_id>/run/", views.run_monitoring, name="run_monitoring"),
    path("monitoring/<int:topic_id>/stop/", views.stop_monitoring, name="stop_monitoring"),
    path("monitoring/<int:topic_id>/archive/", views.archive_monitoring_topic, name="archive_monitoring_topic"),
    path("monitoring/<int:topic_id>/restore/", views.restore_monitoring_topic, name="restore_monitoring_topic"),
    path("monitoring/<int:topic_id>/delete/", views.delete_monitoring_topic, name="delete_monitoring_topic"),

    # Sources
    path("sources/", views.sources_page, name="sources"),
    path("sources/save/", views.save_source, name="save_source"),
    path("sources/<int:source_id>/archive/", views.archive_source, name="archive_source"),
    path("sources/<int:source_id>/restore/", views.restore_source, name="restore_source"),
    path("sources/<int:source_id>/delete/", views.delete_source, name="delete_source"),

    # Articles
    path("articles/", views.articles_page, name="articles"),
    path("articles/<int:article_id>/favorite/", views.toggle_article_favorite, name="toggle_article_favorite"),
    path("articles/<int:article_id>/approve/", views.approve_article_relevance, name="approve_article_relevance"),
    path("articles/<int:article_id>/delete/", views.delete_article, name="delete_article"),

    # Clusters
    path("clusters/", views.clusters_page, name="clusters"),
    path("clusters/run-deduplication/", views.run_deduplication, name="run_deduplication"),
    path("clusters/<int:cluster_id>/delete/", views.delete_cluster, name="delete_cluster"),
    path("clusters/<int:cluster_id>/", views.cluster_detail, name="cluster_detail"),

    # Drafts
    path("drafts/", views.drafts_page, name="drafts"),
    path("generated/", views.generated_materials_page, name="generated_materials"),
    path(
        "drafts/generated/",
        RedirectView.as_view(pattern_name="newsroom:generated_materials", permanent=False, query_string=True),
    ),
    path("drafts/<int:draft_id>/export/docx/", views.export_draft_docx, name="export_draft_docx"),
    path("drafts/<int:draft_id>/delete/", views.delete_draft, name="delete_draft"),
    path("drafts/<int:draft_id>/", views.draft_detail, name="draft_detail"),

    # Templates
    path("templates/", views.content_templates_page, name="content_templates"),
    path("templates/<int:template_id>/archive/", views.archive_content_template, name="archive_content_template"),
    path("templates/<int:template_id>/restore/", views.restore_content_template, name="restore_content_template"),
    path("templates/<int:template_id>/delete/", views.delete_content_template, name="delete_content_template"),

    # Settings
    path("settings/", views.settings_page, name="settings"),
    path("settings/password/", views.change_password_page, name="change_password"),
]
