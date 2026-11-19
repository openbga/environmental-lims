"""Print the candidate’s forward migration plan on the populated CI database via manage.py shell."""

import json

from django.db import connection
from django.db.migrations.executor import MigrationExecutor

executor = MigrationExecutor(connection)
plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
if any(backwards for _, backwards in plan):
    raise SystemExit("The candidate upgrade would reverse a migration.")
print(json.dumps([(migration.app_label, migration.name) for migration, _ in plan]))
