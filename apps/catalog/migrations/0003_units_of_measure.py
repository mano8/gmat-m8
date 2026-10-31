"""The units of measure (plan §4.2), seeded; ``decimals`` is how many decimal places a
quantity in the unit may have. Reversing removes the ones nothing references."""

from django.db import migrations

UNITS = (
    ("UD", "Unidad", 0),
    ("PAR", "Par", 0),
    ("M", "Metro", 2),
    ("M2", "Metro cuadrado", 2),
    ("KG", "Kilogramo", 3),
    ("L", "Litro", 3),
    ("ROLLO", "Rollo", 0),
    ("CAJA", "Caja", 0),
    ("BOLSA", "Bolsa", 0),
    ("BOTE", "Bote", 0),
    ("JUEGO", "Juego", 0),
)


def seed(apps, schema_editor):
    from apps.core.ids import new_id

    unit = apps.get_model("catalog", "UnitOfMeasure")
    for code, name, decimals in UNITS:
        unit.objects.get_or_create(
            code=code, defaults={"id": new_id(), "name": name, "decimals": decimals}
        )


def unseed(apps, schema_editor):
    unit = apps.get_model("catalog", "UnitOfMeasure")
    unit.objects.filter(code__in=[code for code, _, _ in UNITS], materials__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [("catalog", "0002_search_and_two_levels")]

    operations = [migrations.RunPython(seed, unseed)]
