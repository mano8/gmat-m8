from django.apps import AppConfig


class MaterialRequestsConfig(AppConfig):
    name = "apps.material_requests"
    label = "material_requests"
    default = True

    def ready(self) -> None:
        from apps.material_requests.ledger import requests_with_outstanding
        from apps.material_requests.procurement import follow_project
        from apps.organizations.deactivation import register_open_documents
        from apps.projects.procurement import register_follower

        # S9, F62: no deactivation while an approved request still awaits deliveries.
        register_open_documents(requests_with_outstanding)
        # Open requests move with their project's procurement branch.
        register_follower(follow_project)
