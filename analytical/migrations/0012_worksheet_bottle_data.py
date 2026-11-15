from django.db import migrations, models


def refuse_reverse_with_bottles(apps, schema_editor):
    worksheets = apps.get_model("analytical", "ResultWorksheet")
    if (
        worksheets.objects.using(schema_editor.connection.alias)
        .filter(bottle_data__isnull=False)
        .exists()
    ):
        raise RuntimeError(
            "BOD worksheets retain bottle observations and selections. Reversing "
            "analytical.0012 would discard that provenance; retain this schema."
        )


class Migration(migrations.Migration):
    """Add a nullable snapshot without rewriting worksheets protected by history triggers.

    Older code may still insert worksheets with NULL here. The reverse guard prevents losing
    observations once the new calculation has been used. Code rollback keeps the column.
    """

    dependencies = [("analytical", "0011_worksheet_unrounded_limit")]

    operations = [
        migrations.AddField(
            model_name="resultworksheet",
            name="bottle_data",
            field=models.JSONField(
                blank=True,
                null=True,
                help_text="BOD bottle observations and selections, with measured values kept as text. "
                "Empty for other calculations.",
            ),
        ),
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse_with_bottles),
    ]
