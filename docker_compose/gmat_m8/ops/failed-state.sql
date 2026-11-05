-- What a restore must not forget, read from the failed database before it is replaced (plan §9).
--
-- recover.sh runs this as plain SQL on the db container: it has to work on a half-migrated
-- database, whatever image is running. One JSON object on one line:
--
--   outbox  the outcome of each message that reached a terminal status: after the restore that
--           row takes it instead of being quarantined (F44);
--   audit   the access and credential events; post_restore_reconcile replays the ones the
--           restored audit table lacks (apps.recovery.replay).
SELECT json_build_object(
    'format_version', 1,
    'exported_at', now(),
    'outbox', COALESCE((
        SELECT json_agg(json_build_object('id', id, 'status', status, 'sent_at', sent_at))
        FROM public.notifications_emailoutbox
        WHERE status IN ('SENT', 'CANCELLED', 'SUPERSEDED', 'SUPPRESSED', 'EXPIRED')
    ), '[]'::json),
    'audit', COALESCE((
        SELECT json_agg(json_build_object(
                   'id', id,
                   'occurred_at', occurred_at,
                   'actor_label', actor_label,
                   'action', action,
                   'object_type', object_type,
                   'object_id', object_id,
                   'data', data) ORDER BY id)
        FROM public.audit_auditevent
        WHERE action ~ '^(user|membership|project_assignment|company|branch|project|email_change|invitation)\.'
    ), '[]'::json)
);
