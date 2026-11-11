-- Follow-up: did the LLM return nothing, or did the grounding guard null the name?
-- Counts, enums and booleans only -- no patient values.
-- Run:  Get-Content scripts\diagnose_name_dropout.sql | docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'

\pset pager off

\echo
\echo ===== how much did each failing extraction actually populate? =====
SELECT d.id,
       d.doc_type,
       (SELECT count(*) FROM jsonb_each(e.fields) f
         WHERE jsonb_typeof(f.value) = 'object'
           AND f.value->>'value' IS NOT NULL)        AS populated,
       (SELECT count(*) FROM jsonb_each(e.fields) f
         WHERE jsonb_typeof(f.value) = 'object')     AS total_fields,
       jsonb_array_length(COALESCE(e.fields->'insurance', '[]'::jsonb)) AS ins_slots,
       e.fields->'last_name'->>'source'              AS last_src,
       e.fields->'first_name'->>'source'             AS first_src,
       e.repaired,
       e.llm_model
FROM documents d
JOIN extractions e ON e.document_id = d.id
WHERE d.status = 'extraction_failed'
ORDER BY d.created_at DESC;

\echo
\echo ===== same numbers for COMPLETED documents, as the baseline =====
SELECT d.doc_type,
       count(*)                                                  AS docs,
       round(avg((SELECT count(*) FROM jsonb_each(e.fields) f
                   WHERE jsonb_typeof(f.value) = 'object'
                     AND f.value->>'value' IS NOT NULL))::numeric, 1) AS avg_populated
FROM documents d
JOIN extractions e ON e.document_id = d.id
WHERE d.status = 'completed'
GROUP BY 1
ORDER BY 1;
