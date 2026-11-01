from django.apps import AppConfig


class PurchasingConfig(AppConfig):
    name = "apps.purchasing"
    label = "purchasing"
    default = True

    def ready(self) -> None:
        from apps.material_requests.commitments import register_component_locker
        from apps.material_requests.ledger import register_flow_source
        from apps.notifications.dispatch import register_dispatch_source
        from apps.organizations.deactivation import register_open_documents
        from apps.purchasing.components import lock_for_requests
        from apps.purchasing.dispatch import SOURCE
        from apps.purchasing.ledger import issued_orders, request_line_flows

        # R, P, Q and the allocated ceiling of request lines (plan §4.5).
        register_flow_source(request_line_flows)
        # The request services lock and reconcile the allocation component (§4.5, §4.7).
        register_component_locker(lock_for_requests)
        # S9, F62: no deactivation while an issued order is open.
        register_open_documents(issued_orders)
        # The worker sends supplier dispatches through the order's locks (plan §5, M5.3).
        register_dispatch_source(SOURCE)
