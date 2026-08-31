STATUS_LABELS = {
    "active": "Активне",
    "new": "Нове",
    "processed": "Оброблене",
    "clustered": "У кластері",
    "rejected": "Відхилене",
    "archived": "Архів",
    "site": "Сайт",
    "telegram": "Telegram",
}

STATUS_BADGE_CLASSES = {
    "active": "badge-emerald",
    "new": "badge-blue",
    "processed": "badge-muted",
    "clustered": "badge-violet",
    "rejected": "badge-red",
    "archived": "badge-muted",
    "site": "badge-blue",
    "telegram": "badge-violet",
}

NAV_ITEMS = [
    {"href": "/", "label": "Панель", "badge": "П", "icon_paths": ["M4 13h6V4H4v9Z", "M14 20h6V4h-6v16Z", "M4 20h6v-4H4v4Z"]},
    {"href": "/monitoring/", "label": "Моніторинг", "badge": "М", "icon_paths": ["M12 4a8 8 0 1 0 8 8", "M12 4a8 8 0 0 0-8 8", "M12 8v4l3 2", "M12 2v2", "M12 20v2"]},
    {"href": "/sources/", "label": "Джерела", "badge": "Д", "icon_paths": ["M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3Z", "M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6", "M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"]},
    {"href": "/articles/", "label": "Новини", "badge": "Н", "icon_paths": ["M7 3h7l4 4v14H7V3Z", "M14 3v5h5", "M9 13h6", "M9 17h6"]},
    {"href": "/clusters/", "label": "Кластери", "badge": "К", "icon_paths": ["M8 8a2 2 0 1 0 0-4 2 2 0 0 0 0 4Z", "M18 12a2 2 0 1 0 0-4 2 2 0 0 0 0 4Z", "M8 20a2 2 0 1 0 0-4 2 2 0 0 0 0 4Z", "M10 7l6 3", "M10 17l6-5"]},
    {"href": "/drafts/", "label": "Генерація", "badge": "Г", "icon_paths": ["M4 20l10-10", "M13 5l6 6", "M15 3l6 6", "M6 4v3", "M4.5 5.5h3", "M19 17v3", "M17.5 18.5h3"], "active_exact": ["/drafts/"]},
    {
        "href": "/generated/",
        "label": "Згенеровані",
        "badge": "З",
        "icon_paths": ["M7 3h7l4 4v14H7V3Z", "M14 3v5h5", "M9 16l2 2 4-5"],
        "active_prefixes": ["/generated/"],
        "active_draft_detail": True,
    },
    {"href": "/templates/", "label": "Шаблони", "badge": "Ш", "icon_paths": ["M4 5h16v14H4V5Z", "M4 10h16", "M9 10v9"]},
    {"href": "/settings/", "label": "Налаштування", "badge": "Н", "icon_paths": ["M4 7h10", "M18 7h2", "M14 5v4", "M4 17h2", "M10 17h10", "M6 15v4"]},
]
