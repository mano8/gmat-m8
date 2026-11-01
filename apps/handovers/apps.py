from django.apps import AppConfig


class HandoversConfig(AppConfig):
    name = "apps.handovers"
    label = "handovers"
    default = True

    def ready(self) -> None:
        from apps.handovers.ledger import delivered
        from apps.material_requests.ledger import register_flow_source

        # D of request lines: posted handovers of both kinds (plan §4.5).
        register_flow_source(delivered)
