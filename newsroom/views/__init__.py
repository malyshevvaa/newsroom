from .auth_views import login_page, logout_view, register_page
from .dashboard_views import dashboard
from .monitoring_views import (
    archive_monitoring_topic,
    delete_monitoring_topic,
    monitoring_page,
    restore_monitoring_topic,
    run_monitoring,
    save_monitoring_topic,
    stop_monitoring,
)
from .source_views import archive_source, delete_source, restore_source, save_source, sources_page
from .article_views import approve_article_relevance, articles_page, delete_article, toggle_article_favorite
from .cluster_views import cluster_detail, clusters_page, delete_cluster, run_deduplication
from .template_views import (
    archive_content_template,
    content_templates_page,
    delete_content_template,
    restore_content_template,
)
from .draft_views import delete_draft, draft_detail, drafts_page, export_draft_docx, generated_materials_page
from .settings_views import change_password_page, settings_page

__all__ = [
    'login_page',
    'register_page',
    'logout_view',
    'dashboard',
    'monitoring_page',
    'save_monitoring_topic',
    'run_monitoring',
    'stop_monitoring',
    'delete_monitoring_topic',
    'archive_monitoring_topic',
    'restore_monitoring_topic',
    'sources_page',
    'save_source',
    'delete_source',
    'archive_source',
    'restore_source',
    'articles_page',
    'toggle_article_favorite',
    'approve_article_relevance',
    'delete_article',
    'clusters_page',
    'run_deduplication',
    'delete_cluster',
    'cluster_detail',
    'content_templates_page',
    'archive_content_template',
    'restore_content_template',
    'delete_content_template',
    'drafts_page',
    'generated_materials_page',
    'delete_draft',
    'draft_detail',
    'export_draft_docx',
    'settings_page',
    'change_password_page',
]
