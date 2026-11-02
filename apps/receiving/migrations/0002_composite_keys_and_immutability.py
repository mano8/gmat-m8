"""Composite foreign keys of receipts (plan §4.6) and their immutability (I11).

- C1: ``(receipt_id, po_id) → goodsreceipt(id, po_id)`` and ``(po_line_id, po_id) →
  purchaseorderline(id, po_id)``: a receipt line's order line belongs to the receipt's
  order.
- C2: ``(allocation_id, po_line_id) → purchaseallocation(id, po_line_id)`` and
  ``(receipt_line_id, po_line_id) → goodsreceiptline(id, po_line_id)``: an attribution's
  allocation belongs to the received order line.
- I11: a row trigger lets a posted receipt change only by its void (status, the void
  fields and ``updated_at``; the CHECK ``rcpt_void_fields`` keeps them together) and never
  again once voided; receipts are never deleted, and their lines and attributions are
  never updated or deleted. Refusals raise ``restrict_violation`` (an ``IntegrityError``).

Reversible: the reverse drops the triggers, their functions and the constraints.
"""

from django.db import migrations

from apps.core.migration_ops import AddCompositeForeignKey

FORWARD = """
CREATE FUNCTION receiving_receipt_posted() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'goods receipts are never deleted'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.status <> 'POSTED'
        OR (to_jsonb(NEW) - ARRAY['status', 'voided_at', 'voided_by_id', 'void_reason', 'updated_at'])
            IS DISTINCT FROM
           (to_jsonb(OLD) - ARRAY['status', 'voided_at', 'voided_by_id', 'void_reason', 'updated_at'])
    THEN
        RAISE EXCEPTION 'a posted goods receipt changes only by its void'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER receiving_receipt_posted
BEFORE UPDATE OR DELETE ON receiving_goodsreceipt
FOR EACH ROW EXECUTE FUNCTION receiving_receipt_posted();

CREATE FUNCTION receiving_receipt_rows_frozen() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% rows of a goods receipt are never changed: % refused', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$;

CREATE TRIGGER receiving_receipt_line_frozen
BEFORE UPDATE OR DELETE ON receiving_goodsreceiptline
FOR EACH ROW EXECUTE FUNCTION receiving_receipt_rows_frozen();

CREATE TRIGGER receiving_attribution_frozen
BEFORE UPDATE OR DELETE ON receiving_receiptattribution
FOR EACH ROW EXECUTE FUNCTION receiving_receipt_rows_frozen();
"""

REVERSE = """
DROP TRIGGER IF EXISTS receiving_attribution_frozen ON receiving_receiptattribution;
DROP TRIGGER IF EXISTS receiving_receipt_line_frozen ON receiving_goodsreceiptline;
DROP FUNCTION IF EXISTS receiving_receipt_rows_frozen();
DROP TRIGGER IF EXISTS receiving_receipt_posted ON receiving_goodsreceipt;
DROP FUNCTION IF EXISTS receiving_receipt_posted();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("receiving", "0001_initial"),
    ]

    operations = [
        AddCompositeForeignKey(
            name="rcpt_line_receipt_order_fk",
            table="receiving_goodsreceiptline",
            columns=("receipt_id", "po_id"),
            ref_table="receiving_goodsreceipt",
            ref_columns=("id", "po_id"),
        ),
        AddCompositeForeignKey(
            name="rcpt_line_po_line_order_fk",
            table="receiving_goodsreceiptline",
            columns=("po_line_id", "po_id"),
            ref_table="purchasing_purchaseorderline",
            ref_columns=("id", "po_id"),
        ),
        AddCompositeForeignKey(
            name="rcpt_attribution_allocation_line_fk",
            table="receiving_receiptattribution",
            columns=("allocation_id", "po_line_id"),
            ref_table="purchasing_purchaseallocation",
            ref_columns=("id", "po_line_id"),
        ),
        AddCompositeForeignKey(
            name="rcpt_attribution_receipt_line_fk",
            table="receiving_receiptattribution",
            columns=("receipt_line_id", "po_line_id"),
            ref_table="receiving_goodsreceiptline",
            ref_columns=("id", "po_line_id"),
        ),
        migrations.RunSQL(FORWARD, REVERSE),
    ]
