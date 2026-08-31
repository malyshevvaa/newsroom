from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from newsroom.services.monitoring_tasks import _run_monitoring_job


class Command(BaseCommand):
    help = "Запускає моніторинг однієї теми в окремому процесі."

    def add_arguments(self, parser):
        parser.add_argument("topic_id", type=int)
        parser.add_argument("--actor-id", type=int, default=None)

    def handle(self, *args, **options):
        topic_id = options["topic_id"]
        actor_id = options["actor_id"]

        if topic_id <= 0:
            raise CommandError("topic_id має бути додатним числом.")

        # Увесь робочий процес уже описаний у сервісному модулі,
        # тому команда лише передає параметри та запускає його.
        _run_monitoring_job(topic_id, actor_id)

