from __future__ import annotations


def sql_literal(value: str) -> str:
    return "'" + (value or "").replace("'", "''") + "'"


def _normalized_phone_sql(column_sql: str) -> str:
    digits = f"regexp_replace(COALESCE({column_sql}, ''), '\\D', '', 'g')"
    return (
        "CASE "
        f"WHEN length({digits}) = 11 AND left({digits}, 1) = '8' "
        f"THEN '7' || substring({digits} from 2) "
        f"ELSE {digits} "
        "END"
    )


def _logical_call_key_sql(alias: str) -> str:
    normalized_phone = _normalized_phone_sql(f"{alias}.phone")
    started_bucket = f"to_char(date_trunc('second', {alias}.started_at), 'YYYY-MM-DD HH24:MI:SSOF')"
    fallback = (
        f"md5(COALESCE({alias}.direction, 'unknown') || '|' || "
        f"{normalized_phone} || '|' || "
        f"{started_bucket} || '|' || "
        f"COALESCE({alias}.duration_sec, 0)::text || '|' || "
        f"COALESCE(NULLIF({alias}.line_id, ''), ''))"
    )
    return (
        "CASE "
        f"WHEN COALESCE(NULLIF({alias}.external_call_id, ''), '') <> '' "
        f"THEN 'ext:' || {alias}.external_call_id "
        f"ELSE 'fp:' || {fallback} "
        "END"
    )


def _projection_cte(where_sql: str) -> str:
    logical_key = _logical_call_key_sql("c")
    return f"""
    WITH raw_calls AS (
      SELECT c.call_id,
             COALESCE(c.source, 'bitrix') AS source,
             c.started_at,
             COALESCE(c.duration_sec, 0) AS duration_sec,
             c.manager_id AS raw_manager_id,
             COALESCE(c.resolved_manager_id, c.manager_id) AS manager_id,
             COALESCE(c.resolved_manager_name, '') AS manager_name,
             COALESCE(c.phone, '') AS phone,
             COALESCE(c.call_record_url, '') AS call_record_url,
             COALESCE(c.status, 'unknown') AS status,
             COALESCE(c.store_id, 'unknown') AS raw_store_id,
             COALESCE(NULLIF(c.resolved_store_id, ''), c.store_id, 'unknown') AS store_id,
             COALESCE(c.resolved_store_name, '') AS store_name,
             COALESCE(c.line_id, '') AS raw_line_id,
             COALESCE(NULLIF(c.resolved_line_id, ''), c.line_id, '') AS line_id,
             COALESCE(NULLIF(c.resolution_source, ''), 'unresolved') AS resolution_source,
             COALESCE(c.manager_resolution_conflict, false) AS manager_resolution_conflict,
             COALESCE(c.direction, 'unknown') AS direction,
             COALESCE(c.external_call_id, '') AS external_call_id,
             COALESCE(c.portal_number, '') AS portal_number,
             COALESCE(c.call_failed_code, '') AS call_failed_code,
             COALESCE(c.provider_name, '') AS provider_name,
             c.updated_at,
             {logical_key} AS logical_call_key
      FROM calls c
      WHERE {where_sql}
    ),
    raw_enriched AS (
      SELECT r.*,
             t.transcript_text,
             t.model AS transcript_model,
             ca.outcome,
             ca.sentiment,
             ca.summary,
             ca.analysis_json
      FROM raw_calls r
      LEFT JOIN transcripts t ON t.call_id = r.call_id
      LEFT JOIN call_analysis ca ON ca.call_id = r.call_id
    ),
    grouped AS (
      SELECT logical_call_key,
             min(started_at) AS started_at,
             max(duration_sec) AS duration_sec,
             COALESCE(
               max(phone) FILTER (WHERE phone <> ''),
               ''
             ) AS phone,
             COALESCE(
               max(direction) FILTER (WHERE direction <> '' AND direction <> 'unknown'),
               max(direction),
               'unknown'
             ) AS direction,
             max(manager_id) FILTER (WHERE manager_id IS NOT NULL) AS manager_id,
             COALESCE(
               max(store_id) FILTER (WHERE store_id <> '' AND store_id <> 'unknown'),
               max(store_id),
               'unknown'
             ) AS store_id,
             COALESCE(
               max(store_name) FILTER (WHERE store_name <> ''),
               ''
             ) AS store_name,
             COALESCE(
               max(line_id) FILTER (WHERE line_id <> ''),
               ''
             ) AS line_id,
             COALESCE(
               max(manager_name) FILTER (WHERE manager_name <> ''),
               ''
             ) AS manager_name,
             COALESCE(
               max(resolution_source) FILTER (WHERE resolution_source <> '' AND resolution_source <> 'unresolved'),
               max(resolution_source),
               'unresolved'
             ) AS resolution_source,
             bool_or(manager_resolution_conflict) AS manager_resolution_conflict,
             COALESCE(
               max(call_record_url) FILTER (
                 WHERE source = 'retail_megafon' AND call_record_url <> ''
               ),
               max(call_record_url) FILTER (WHERE call_record_url <> ''),
               ''
             ) AS call_record_url,
             COALESCE(
               max(status) FILTER (WHERE call_record_url <> ''),
               max(status),
               'unknown'
             ) AS status,
             COALESCE(
               max(external_call_id) FILTER (WHERE external_call_id <> ''),
               ''
             ) AS external_call_id,
             COALESCE(
               max(portal_number) FILTER (WHERE portal_number <> ''),
               ''
             ) AS portal_number,
             COALESCE(
               max(call_failed_code) FILTER (WHERE call_failed_code <> ''),
               ''
             ) AS call_failed_code,
             COALESCE(
               max(provider_name) FILTER (WHERE provider_name <> ''),
               ''
             ) AS provider_name,
             string_agg(DISTINCT source, ',') AS source_set,
             bool_or(call_record_url <> '') AS has_record,
             bool_or(COALESCE(length(trim(transcript_text)), 0) > 0) AS has_transcript,
             count(*) AS raw_row_count
      FROM raw_enriched
      GROUP BY logical_call_key
    ),
    ranked AS (
      SELECT r.*,
             row_number() OVER (
               PARTITION BY logical_call_key
               ORDER BY
                 CASE WHEN COALESCE(length(trim(r.transcript_text)), 0) > 0 THEN 0 ELSE 1 END,
                 CASE WHEN COALESCE(r.call_record_url, '') <> '' THEN 0 ELSE 1 END,
                 CASE WHEN COALESCE(r.source, '') = 'retail_megafon' THEN 0 ELSE 1 END,
                 CASE
                   WHEN COALESCE(r.resolution_source, 'unresolved') IN ('onec_extension', 'line_map', 'phone_map')
                     THEN 0
                   WHEN COALESCE(r.resolution_source, 'unresolved') = 'bitrix_raw_fallback'
                     THEN 1
                   ELSE 2
                 END,
                 CASE WHEN r.manager_id IS NOT NULL THEN 0 ELSE 1 END,
                 r.updated_at DESC NULLS LAST,
                 r.call_id
             ) AS row_rank
      FROM raw_enriched r
    ),
    canonical AS (
      SELECT *
      FROM ranked
      WHERE row_rank = 1
    )
    """


def build_asr_candidates_sql(
    start_at: str,
    end_at: str,
    max_calls: int,
    min_duration_seconds: int = 0,
) -> str:
    where_sql = (
        f"c.started_at >= {sql_literal(start_at)}::timestamptz "
        f"AND c.started_at < {sql_literal(end_at)}::timestamptz"
    )
    min_duration_filter = ""
    if min_duration_seconds > 0:
        min_duration_filter = f"AND grouped.duration_sec >= {int(min_duration_seconds)}"
    return _projection_cte(where_sql) + f"""
    SELECT canonical.call_id,
           grouped.call_record_url,
           canonical.source,
           grouped.store_id,
           grouped.line_id
    FROM grouped
    JOIN canonical USING (logical_call_key)
    WHERE grouped.call_record_url <> ''
      AND NOT grouped.has_transcript
      {min_duration_filter}
    ORDER BY grouped.started_at DESC
    LIMIT {int(max_calls)};
    """


def build_asr_window_stats_sql(start_at: str, end_at: str) -> str:
    where_sql = (
        f"c.started_at >= {sql_literal(start_at)}::timestamptz "
        f"AND c.started_at < {sql_literal(end_at)}::timestamptz"
    )
    return _projection_cte(where_sql) + """
    SELECT count(*)::text,
           sum(CASE WHEN grouped.call_record_url <> '' THEN 1 ELSE 0 END)::text,
           sum(CASE WHEN grouped.call_record_url = '' THEN 1 ELSE 0 END)::text,
           sum(CASE WHEN grouped.has_transcript THEN 1 ELSE 0 END)::text
    FROM grouped;
    """


def build_meeting_action_rows_sql(lookback_days: int) -> str:
    where_sql = (
        "c.started_at >= ("
        "((date_trunc('day', now() AT TIME ZONE 'Europe/Moscow') - "
        f"interval '{int(lookback_days)} day') AT TIME ZONE 'Europe/Moscow')"
        ")"
    )
    return _projection_cte(where_sql) + """
    SELECT canonical.call_id,
           canonical.source,
           grouped.store_id,
           COALESCE(grouped.manager_id::text, ''),
           to_char(grouped.started_at AT TIME ZONE 'Europe/Moscow', 'YYYY-MM-DD HH24:MI:SS'),
           COALESCE(canonical.outcome, ''),
           COALESCE(canonical.sentiment, ''),
           left(COALESCE(convert_from(convert_to(canonical.summary, 'SQL_ASCII'), 'UTF8'), ''), 500),
           left(COALESCE(convert_from(convert_to(canonical.transcript_text, 'SQL_ASCII'), 'UTF8'), ''), 1500)
    FROM grouped
    JOIN canonical USING (logical_call_key)
    WHERE grouped.has_transcript
    ORDER BY grouped.started_at DESC;
    """


def build_calls_content_rows_sql() -> str:
    where_sql = (
        "c.started_at >= ("
        "((date_trunc('day', now() AT TIME ZONE 'Europe/Moscow') - interval '1 day') "
        "AT TIME ZONE 'Europe/Moscow')"
        ") AND c.started_at < ("
        "((date_trunc('day', now() AT TIME ZONE 'Europe/Moscow')) "
        "AT TIME ZONE 'Europe/Moscow')"
        ")"
    )
    return _projection_cte(where_sql) + """
    SELECT canonical.call_id,
           canonical.source,
           grouped.store_id,
           COALESCE(grouped.manager_id::text, ''),
           grouped.store_name,
           grouped.manager_name,
           grouped.phone,
           grouped.status,
           grouped.direction,
           left(COALESCE(convert_from(convert_to(canonical.transcript_text, 'SQL_ASCII'), 'UTF8'), ''), 1200)
    FROM grouped
    JOIN canonical USING (logical_call_key)
    ORDER BY grouped.started_at DESC;
    """
