from django.apps import AppConfig


class DisabledCommandsConfig(AppConfig):
    name = "apps.accounts.disabled_commands"
    label = "disabled_commands"
    verbose_name = "Disabled account commands"
