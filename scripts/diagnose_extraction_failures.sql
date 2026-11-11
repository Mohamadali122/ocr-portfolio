-- Diagnostic for documents landing in EXTRACTION_FAILED.
-- Counts, enums, lengths and UUIDs only -- no patient values, no OCR text.
-- Run:  Get-Content scripts\diagnose_extraction_failures.sql | docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'

\pset pager off

\echo
\echo ===== 1. doc_type x status census =====
SELECT doc_type, status, count(*)
FROM documents
GROUP BY 1, 2
ORDER BY 1, 2;

\echo
\echo ===== 2. failing documents, most recent 20 =====
SELECT d.id,
       d.doc_type,
       o.page_count,
       round(o.mean_confidence::numeric, 3)      AS ocr_conf,
       length(o.raw_text)                        AS ocr_chars,
       (e.id IS NOT NULL)                        AS has_extraction,
       substring(d.failure_reason from 1 for 80) AS reason
FROM documents d
LEFT JOIN ocr_results o ON o.document_id = d.id
LEFT JOIN extractions e ON e.document_id = d.id
WHERE d.status = 'extraction_failed'
ORDER BY d.created_at DESC
LIMIT 20;

\echo
\echo ===== 3. failure reason shapes =====
SELECT substring(failure_reason from 1 for 70) AS reason, count(*)
FROM documents
WHERE status IN ('extraction_failed', 'failed')
GROUP BY 1
ORDER BY 2 DESC
LIMIT 15;

\echo
\echo ===== 4. OCR text volume by outcome =====
SELECT d.status,
       count(*)                                   AS docs,
       round(avg(length(o.raw_text))::numeric, 0) AS avg_chars,
       min(length(o.raw_text))                    AS min_chars,
       max(length(o.raw_text))                    AS max_chars,
       round(avg(o.mean_confidence)::numeric, 3)  AS avg_ocr_conf
FROM documents d
JOIN ocr_results o ON o.document_id = d.id
GROUP BY 1
ORDER BY 1;

\echo
\echo ===== 5. name-alias pollution check (SOP "UNKNOWN" in the search index) =====
SELECT patient_last_name IS NULL              AS name_null,
       patient_last_name = 'UNKNOWN'          AS name_is_alias,
       status,
       count(*)
FROM documents
GROUP BY 1, 2, 3
ORDER BY 4 DESC;
