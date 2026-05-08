from __future__ import annotations

from sqlalchemy import create_engine, text

from app.services.task_efficiency import load_task_efficiency_report


def _seed_task_efficiency_db(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("""
                    CREATE TABLE bitrix_fact_employee_task_kpi_monthly (
                        month_start date NOT NULL,
                        month_end date NOT NULL,
                        employee_bitrix_id text NOT NULL,
                        employee_key text,
                        employee_name text,
                        metric_code text NOT NULL,
                        total_personal_tasks_with_deadline integer NOT NULL,
                        closed_on_time_personal_tasks integer NOT NULL,
                        late_closed_personal_tasks integer NOT NULL,
                        open_overdue_personal_tasks integer NOT NULL,
                        canceled_personal_tasks integer NOT NULL,
                        personal_tasks_on_time_share numeric,
                        include_subtasks boolean,
                        min_task_count integer,
                        is_metric_applicable boolean,
                        exclusion_reason text,
                        source_scope text,
                        calculation_note text,
                        calculated_at timestamp
                    )
                    """))
            connection.execute(text("""
                    INSERT INTO bitrix_fact_employee_task_kpi_monthly (
                        month_start,
                        month_end,
                        employee_bitrix_id,
                        employee_key,
                        employee_name,
                        metric_code,
                        total_personal_tasks_with_deadline,
                        closed_on_time_personal_tasks,
                        late_closed_personal_tasks,
                        open_overdue_personal_tasks,
                        canceled_personal_tasks,
                        personal_tasks_on_time_share,
                        include_subtasks,
                        min_task_count,
                        is_metric_applicable,
                        exclusion_reason,
                        source_scope,
                        calculation_note,
                        calculated_at
                    )
                    VALUES
                        (
                            '2026-03-01',
                            '2026-03-31',
                            '2',
                            'emp-petr',
                            'Петр',
                            'personal_tasks_on_time_share',
                            4,
                            2,
                            1,
                            1,
                            0,
                            50.00,
                            0,
                            1,
                            1,
                            NULL,
                            'personal_tasks_on_time_share_v1',
                            'test',
                            '2026-04-01 09:00:00'
                        ),
                        (
                            '2026-03-01',
                            '2026-03-31',
                            '1',
                            'emp-ivan',
                            'Иван',
                            'personal_tasks_on_time_share',
                            4,
                            4,
                            0,
                            0,
                            0,
                            100.00,
                            0,
                            1,
                            1,
                            NULL,
                            'personal_tasks_on_time_share_v1',
                            'test',
                            '2026-04-01 09:00:00'
                        )
                    """))
    finally:
        engine.dispose()


def _seed_raw_bitrix_tasks(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("""
                    CREATE TABLE bitrix_raw_task_current (
                        task_id text PRIMARY KEY,
                        parent_task_id text,
                        status_code text,
                        responsible_id text,
                        responsible_name text,
                        created_by text,
                        created_date timestamp,
                        changed_date timestamp,
                        status_changed_date timestamp,
                        deadline_date timestamp,
                        closed_date timestamp,
                        accomplices_json text,
                        payload_json text
                    )
                    """))
            connection.execute(text("""
                    INSERT INTO bitrix_raw_task_current (
                        task_id,
                        parent_task_id,
                        status_code,
                        responsible_id,
                        responsible_name,
                        created_by,
                        created_date,
                        changed_date,
                        status_changed_date,
                        deadline_date,
                        closed_date,
                        accomplices_json,
                        payload_json
                    )
                    VALUES
                        ('r1', '0', '5', '2', 'Петр', '100', '2026-03-02 10:00:00', '2026-03-02 12:00:00', '2026-03-02 12:00:00', '2026-03-03 19:00:00', '2026-03-02 12:00:00', '[]', '{}'),
                        ('r2', '0', '5', '2', 'Петр', '100', '2026-03-02 10:00:00', '2026-03-05 12:00:00', '2026-03-05 12:00:00', '2026-03-03 19:00:00', '2026-03-05 12:00:00', '[]', '{}'),
                        ('r3', '0', '2', '2', 'Петр', '100', '2026-03-02 10:00:00', '2026-03-10 12:00:00', '2026-03-10 12:00:00', '2026-04-10 19:00:00', NULL, '[]', '{}'),
                        ('r4', '0', '2', '2', 'Петр', '100', '2026-03-02 10:00:00', '2026-03-10 12:00:00', '2026-03-10 12:00:00', NULL, NULL, '[]', '{}'),
                        ('self-task', '0', '2', '2', 'Петр', '2', '2026-03-02 10:00:00', '2026-03-10 12:00:00', '2026-03-10 12:00:00', NULL, NULL, '[]', '{}'),
                        ('i1', '0', '5', '1', 'Иван', '100', '2026-03-02 10:00:00', '2026-03-02 12:00:00', '2026-03-02 12:00:00', '2026-03-03 19:00:00', '2026-03-02 12:00:00', '[]', '{}'),
                        ('i2', '0', '5', '1', 'Иван', '100', '2026-03-04 10:00:00', '2026-03-05 12:00:00', '2026-03-05 12:00:00', '2026-03-06 19:00:00', '2026-03-05 12:00:00', '[]', '{}')
                    """))
    finally:
        engine.dispose()


def test_load_task_efficiency_report_summarizes_all_employees(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'task_efficiency.db'}"
    _seed_task_efficiency_db(database_url)

    report = load_task_efficiency_report(
        month="2026-03",
        database_url=database_url,
        schema="main",
        source_scope="personal_tasks_on_time_share_v1",
        low_threshold_pct=80.0,
    )

    assert report["freshness_status"] == "fresh"
    assert report["source_status"] == "ready"
    assert report["summary"]["employee_count"] == 2
    assert report["summary"]["applicable_count"] == 2
    assert report["summary"]["average_on_time_share"] == 75.0
    assert report["summary"]["bitrix_average_effectiveness_pct"] == 75.0
    assert report["summary"]["bitrix_total_in_work_count"] == 8
    assert report["summary"]["bitrix_completed_tasks_count"] == 7
    assert report["summary"]["bitrix_task_remarks_count"] == 2
    assert report["summary"]["low_efficiency_count"] == 1
    assert report["summary"]["total_personal_tasks_with_deadline"] == 8
    assert report["payload"][0]["employee_name"] == "Петр"
    assert report["payload"][0]["personal_tasks_on_time_share"] == 50.0
    assert report["payload"][0]["bitrix_total_in_work_count"] == 4
    assert report["payload"][0]["bitrix_task_remarks_count"] == 2
    assert report["payload"][0]["bitrix_effectiveness_pct"] == 50.0
    assert report["payload"][0]["bitrix_effectiveness_source"] == "monthly_fact_deadline_proxy"
    assert report["payload"][1]["employee_name"] == "Иван"


def test_load_task_efficiency_report_uses_bitrix_like_raw_stats(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'task_efficiency_raw.db'}"
    _seed_task_efficiency_db(database_url)
    _seed_raw_bitrix_tasks(database_url)

    report = load_task_efficiency_report(
        month="2026-03",
        database_url=database_url,
        schema="main",
        source_scope="personal_tasks_on_time_share_v1",
        low_threshold_pct=80.0,
    )

    petr = next(item for item in report["payload"] if item["employee_name"] == "Петр")
    assert petr["bitrix_total_in_work_count"] == 4
    assert petr["bitrix_completed_tasks_count"] == 2
    assert petr["bitrix_task_remarks_count"] == 1
    assert petr["bitrix_effectiveness_pct"] == 75.0
    assert petr["bitrix_effectiveness_source"] == "bitrix_raw_task_current"
    assert report["summary"]["bitrix_average_effectiveness_pct"] == 87.5
    assert report["summary"]["bitrix_total_in_work_count"] == 6
    assert report["summary"]["bitrix_task_remarks_count"] == 1


def test_load_task_efficiency_report_avoids_deadline_proxy_when_raw_source_exists(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'task_efficiency_raw_self.db'}"
    _seed_task_efficiency_db(database_url)
    _seed_raw_bitrix_tasks(database_url)

    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("""
                    INSERT INTO bitrix_fact_employee_task_kpi_monthly (
                        month_start,
                        month_end,
                        employee_bitrix_id,
                        employee_key,
                        employee_name,
                        metric_code,
                        total_personal_tasks_with_deadline,
                        closed_on_time_personal_tasks,
                        late_closed_personal_tasks,
                        open_overdue_personal_tasks,
                        canceled_personal_tasks,
                        personal_tasks_on_time_share,
                        include_subtasks,
                        min_task_count,
                        is_metric_applicable,
                        exclusion_reason,
                        source_scope,
                        calculation_note,
                        calculated_at
                    )
                    VALUES (
                        '2026-03-01',
                        '2026-03-31',
                        '3',
                        'emp-self',
                        'Сам себе',
                        'personal_tasks_on_time_share',
                        2,
                        0,
                        0,
                        2,
                        0,
                        0.00,
                        0,
                        1,
                        1,
                        NULL,
                        'personal_tasks_on_time_share_v1',
                        'test',
                        '2026-04-01 09:00:00'
                    )
                    """))
            connection.execute(text("""
                    INSERT INTO bitrix_raw_task_current (
                        task_id,
                        parent_task_id,
                        status_code,
                        responsible_id,
                        responsible_name,
                        created_by,
                        created_date,
                        changed_date,
                        status_changed_date,
                        deadline_date,
                        closed_date,
                        accomplices_json,
                        payload_json
                    )
                    VALUES
                        (
                            'self-1',
                            '0',
                            '2',
                            '3',
                            'Сам себе',
                            '3',
                            '2026-03-02 10:00:00',
                            '2026-03-10 12:00:00',
                            '2026-03-10 12:00:00',
                            '2026-03-12 19:00:00',
                            NULL,
                            '[]',
                            '{}'
                        ),
                        (
                            'self-2',
                            '0',
                            '2',
                            '3',
                            'Сам себе',
                            '3',
                            '2026-03-05 10:00:00',
                            '2026-03-11 12:00:00',
                            '2026-03-11 12:00:00',
                            '2026-03-15 19:00:00',
                            NULL,
                            '[]',
                            '{}'
                        )
                    """))
    finally:
        engine.dispose()

    report = load_task_efficiency_report(
        month="2026-03",
        database_url=database_url,
        schema="main",
        source_scope="personal_tasks_on_time_share_v1",
        low_threshold_pct=80.0,
    )

    self_employee = next(item for item in report["payload"] if item["employee_name"] == "Сам себе")
    assert self_employee["personal_tasks_on_time_share"] == 0.0
    assert self_employee["bitrix_total_in_work_count"] == 0
    assert self_employee["bitrix_completed_tasks_count"] == 0
    assert self_employee["bitrix_task_remarks_count"] == 0
    assert self_employee["bitrix_effectiveness_pct"] is None
    assert self_employee["bitrix_effectiveness_source"] == "bitrix_raw_task_current"


def test_load_task_efficiency_report_not_configured() -> None:
    report = load_task_efficiency_report(
        month="2026-03",
        database_url=None,
        schema="main",
    )

    assert report["freshness_status"] == "missing"
    assert report["source_status"] == "not_configured"
    assert report["payload"] == []
