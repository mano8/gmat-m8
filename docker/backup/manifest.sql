-- The data part of a backup manifest (plan §9, "Snapshot-consistent dumps").
--
-- backup.sh runs this inside the same REPEATABLE READ snapshot that pg_dump reads, so the
-- numbers always describe the dump, even with concurrent writes. restore.sh runs it on the
-- restored copy and compares. Plain read-only SQL: it creates nothing.
--
-- psql variable: audit_table, the audit table's name (it joins in M2; absent means "none yet").
-- Output: one JSON object on one line.
SELECT json_build_object(
    'postgres_version', current_setting('server_version'),
    -- Exact row count of every table in the public schema.
    'row_counts', COALESCE((
        SELECT json_object_agg(t.relname, t.n ORDER BY t.relname)
        FROM (
            SELECT c.relname,
                   (xpath('/row/c/text()',
                          query_to_xml(format('SELECT count(*) AS c FROM %I.%I', n.nspname, c.relname),
                                       false, true, '')))[1]::text::bigint AS n
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition
        ) t
    ), '{}'::json),
    -- Latest applied migration of each Django app.
    'migration_head', CASE
        WHEN to_regclass('public.django_migrations') IS NULL THEN NULL
        ELSE (xpath('/row/j/text()',
                    query_to_xml($q$
                        SELECT json_object_agg(app, name ORDER BY app) AS j
                        FROM (SELECT DISTINCT ON (app) app, name
                              FROM public.django_migrations
                              ORDER BY app, id DESC) m
                    $q$, false, true, '')))[1]::text::json
    END,
    -- Identifier of the newest audit event (UUIDv7 sorts by time), once the audit table exists;
    -- null while it is empty. The scalar subquery always yields one row: a query with no row
    -- gives query_to_xml an empty document, which xpath refuses.
    'audit_last_event_id', CASE
        WHEN to_regclass(format('public.%I', :'audit_table')) IS NULL THEN NULL
        ELSE (xpath('/row/j/text()',
                    query_to_xml(format('SELECT (SELECT id FROM public.%I ORDER BY id DESC LIMIT 1)::text AS j',
                                        :'audit_table'), false, true, '')))[1]::text
    END
);
