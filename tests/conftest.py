"""Shared pytest fixtures.

The hermetic environment (database defaults) is seeded in ``config.settings.test``,
because pytest-django imports the settings before this file is loaded.
"""
