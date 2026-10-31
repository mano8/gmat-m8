"""Accent-insensitive trigram search and the two-level category rule (plan §4.2, M3.1).

``unaccent()`` is only STABLE, so an index cannot use it directly. ``catalog_fold`` wraps it
(with the dictionary named, so no search path matters) in an IMMUTABLE function, the usual
way to index ``lower(unaccent(text))``. Queries must build the same expression
(``apps.catalog.models.fold``) for the planner to use the indexes.

The extensions are trusted (PostgreSQL 13+), so the database owner role can create them.
Reversing drops what this migration made but leaves the extensions installed.
"""

from django.db import migrations

FOLD_FORWARD = """
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA public;

CREATE FUNCTION catalog_fold(text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
AS $$ SELECT lower(public.unaccent('public.unaccent'::regdictionary, $1)) $$;

CREATE INDEX catalog_material_reference_trgm
    ON catalog_material USING gin (catalog_fold(reference) gin_trgm_ops);
CREATE INDEX catalog_material_name_trgm
    ON catalog_material USING gin (catalog_fold(name) gin_trgm_ops);
"""

FOLD_REVERSE = """
DROP INDEX catalog_material_name_trgm;
DROP INDEX catalog_material_reference_trgm;
DROP FUNCTION catalog_fold(text);
"""

# A category with children is a root, and a subcategory has no children (two levels at most),
# whatever code path writes the row. The advisory lock serializes concurrent changes, and
# each statement after it reads the committed state (READ COMMITTED), so two changes that
# are each valid alone cannot combine into three levels.
TWO_LEVELS_FORWARD = """
CREATE FUNCTION catalog_category_two_levels() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.parent_id IS NULL THEN
        RETURN NEW;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('gmat:category-parent', 0));
    IF EXISTS (
        SELECT 1 FROM catalog_materialcategory
        WHERE id = NEW.parent_id AND parent_id IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'a subcategory cannot have subcategories'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'catalog_category_two_levels';
    END IF;
    IF EXISTS (
        SELECT 1 FROM catalog_materialcategory WHERE parent_id = NEW.id AND id <> NEW.id
    ) THEN
        RAISE EXCEPTION 'a category with subcategories cannot get a parent'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'catalog_category_two_levels';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER catalog_category_two_levels
BEFORE INSERT OR UPDATE OF parent_id ON catalog_materialcategory
FOR EACH ROW EXECUTE FUNCTION catalog_category_two_levels();
"""

TWO_LEVELS_REVERSE = """
DROP TRIGGER IF EXISTS catalog_category_two_levels ON catalog_materialcategory;
DROP FUNCTION IF EXISTS catalog_category_two_levels();
"""


class Migration(migrations.Migration):
    dependencies = [("catalog", "0001_initial")]

    operations = [
        migrations.RunSQL(FOLD_FORWARD, FOLD_REVERSE),
        migrations.RunSQL(TWO_LEVELS_FORWARD, TWO_LEVELS_REVERSE),
    ]
