"""A schema rollback must not discard bottle provenance."""

import importlib
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.db import connection

from analytical import services

from .test_bod import configure, entry
from .test_workflow import result_for, start


@pytest.mark.django_db
def test_reverse_refuses_after_a_bottle_snapshot_is_recorded(config, analyst):
    migration = importlib.import_module("analytical.migrations.0012_worksheet_bottle_data")
    editor = SimpleNamespace(connection=connection)
    migration.refuse_reverse_with_bottles(apps, editor)
    configure(config)
    result = result_for(start(config, analyst), "Lead")
    revision = services.enter_result(actor=analyst, result_id=result.pk, **entry())
    with pytest.raises(RuntimeError, match="discard that provenance"):
        migration.refuse_reverse_with_bottles(apps, editor)
    revision.worksheet.refresh_from_db()
    assert revision.worksheet.bottle_data[0]["initial_do"] == "8.00"
