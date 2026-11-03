"""Upgrade a persisted database before loading trading state."""
from contextlib import contextmanager

from django.conf import settings
from django.core.management import call_command
from django.db import connections
from django.db.migrations.executor import MigrationExecutor


MIGRATION_LOCK_ID = 734103202611


@contextmanager
def migration_lock(connection):
    # A session lock covers web/bot startups sharing the same PostgreSQL database.
    locked = connection.vendor == 'postgresql'
    if locked:
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_lock(%s)', [MIGRATION_LOCK_ID])
    try:
        yield
    finally:
        if locked:
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_unlock(%s)', [MIGRATION_LOCK_ID])


def bootstrap_database(verbosity=1):
    connection = connections['default']
    with migration_lock(connection):
        # Read the plan after acquiring the lock: another service may have migrated.
        executor = MigrationExecutor(connection)
        targets = executor.loader.graph.leaf_nodes()
        if executor.migration_plan(targets):
            call_command('migrate', interactive=False, verbosity=verbosity)
        call_command('ensure_defaults', verbosity=verbosity)

        from parameters.models import BaseParameters
        from users.models import User

        defaults = BaseParameters.objects.first()
        if defaults:
            for field in ('profit', 'loss', 'pause', 'buy_amount'):
                User.objects.filter(**{field + '__isnull': True}).update(**{field: getattr(defaults, field)})
        if settings.PAIR:
            User.objects.exclude(pair=settings.PAIR).update(pair=settings.PAIR)
