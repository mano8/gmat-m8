from django.apps import AppConfig


class MaterialRequestsConfig(AppConfig):
    name = "apps.material_requests"
    label = "material_requests"
    default = True

    def ready(self) -> None:
        from apps.material_requests.ledger import requests_with_outstanding
        from apps.organizations.deactivation import register_open_documents

        # S9, F62: no deactivation while an approved request still awaits deliveries.
        register_open_documents(requests_with_outstanding)
