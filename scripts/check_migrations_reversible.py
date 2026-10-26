#!/usr/bin/env python
"""Apply every migration, unapply each project app back to zero, and apply again.

Migrations must be reversible (CLAUDE.md, Conventions); a one-way migration has to be
flagged in the PR and the release notes instead. Run it against a scratch PostgreSQL
database: it drops the project's tables and recreates them.
"""

import os
import sys
from pathlib import Path

import django
from django.apps import apps
from django.core.management import call_command
from django.db.migrations.loader import MigrationLoader

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")
    django.setup()
    migrated = {label for label, _ in MigrationLoader(None).disk_migrations}
    labels = [
        config.label
        for config in apps.get_app_configs()
        if config.name.startswith("apps.") and config.label in migrated
    ]
    call_command("migrate", verbosity=1, interactive=False)
    for label in reversed(labels):
        call_command("migrate", label, "zero", verbosity=1, interactive=False)
    call_command("migrate", verbosity=1, interactive=False)
    print(f"check_migrations_reversible: ok ({', '.join(labels)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
