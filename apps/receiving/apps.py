from django.apps import AppConfig


class ReceivingConfig(AppConfig):
    name = "apps.receiving"
    label = "receiving"
    default = True

    def ready(self) -> None:
        from apps.purchasing.receipts import register_receipt_source
        from apps.receiving.ledger import SOURCE

        # Received per order line and attributed per allocation (plan §4.5): purchasing's
        # waterfall, R of request lines and the receipt rules of orders read them.
        register_receipt_source(SOURCE)
