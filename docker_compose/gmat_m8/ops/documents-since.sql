-- What was written since the deploy, for re-entry after a restore (plan §9). recover.sh keeps
-- it next to the failed dump: it names people, so it stays there, mode 0600.
-- psql variable: since, the time of the last deploy.
SELECT COALESCE(json_agg(json_build_object(
           'id', id,
           'occurred_at', occurred_at,
           'actor_label', actor_label,
           'action', action,
           'object_type', object_type,
           'object_id', object_id,
           'data', data) ORDER BY id), '[]'::json)
FROM public.audit_auditevent
WHERE occurred_at >= :'since'
  AND object_type <> ''
  AND action !~ '^recovery\.';
