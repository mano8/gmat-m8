from django.apps import AppConfig


class MaterialRequestsConfig(AppConfig):
    name = "apps.material_requests"
    label = "material_requests"
    default = True

    def ready(self) -> None:
        from apps.catalog.unit_usage import register_unit_users
        from apps.material_requests.ledger import lines_using_unit, requests_with_outstanding
        from apps.material_requests.procurement import follow_project
        from apps.organizations.deactivation import register_open_documents
        from apps.projects.procurement import register_follower

        # S9, F62: no deactivation while an approved request still awaits deliveries.
        register_open_documents(requests_with_outstanding)
        # Q72: no unit deactivated while a line in progress is measured in it.
        register_unit_users(lines_using_unit)
        # Open requests move with their project's procurement branch.
        register_follower(follow_project)
