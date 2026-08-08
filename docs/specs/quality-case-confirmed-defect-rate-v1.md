---
spec_id: "quality-case-confirmed-defect-rate-v1"
title: "Quality Case And Confirmed Defect Rate V1"
doc_type: spec
domain: "quality-control"
status: accepted
owner: "engineering"
source_of_truth: true
related_code:
  - app/api/quality_cases.py
  - app/models/quality_case.py
  - app/schemas/quality_case.py
  - app/services/quality_case.py
  - tasks/build_display_auto_order_dry_run.py
  - tasks/report_display_auto_order_adaptive_lead_time_comparison.py
related_tests:
  - tests/test_quality_case.py
  - tests/test_build_display_auto_order_dry_run_task.py
  - tests/test_report_display_auto_order_adaptive_lead_time_comparison_task.py
contracts:
  - docs/order_flow/Defect_Quality_Contour_2026-04-02.md
  - docs/order_flow/Return_Reason_Classifier_1C_Project_2026-04-08.md
depends_on:
  - docs/Onepage.QualityCaseMVP.md
supersedes: []
rollout_required: true
updated_at: "2026-08-03"
---

# Назначение

Сделать процент брака проверяемым: предварительное качество, указанное продавцом
в `Возврате товаров от покупателя`, не является окончательным решением. В
числитель подтверждённого товарного брака попадают только единицы, по которым ОКК
зафиксировал итог `factory_defect`, `supplier_defect` или `technical_defect`.

# Scope / Out of Scope

Входит:

- append-only `quality_case` и история решений;
- связь кейса со строкой возврата и `Корректировкой качества`;
- отдельные категории товарного дефекта, транспортного/внутреннего повреждения,
  ошибки ревизии и подтверждённо рабочего товара;
- метрики кандидатов, ожидающих проверки и подтверждённых исходов;
- защита автозаказа: ни один downstream-пересчёт не восстанавливает количество
  при наличии исходного `blocker`.

Не входит:

- автоматическая запись в УТ 10.3;
- автоматическая публикация задач в Bitrix24;
- production-миграция и backfill без отдельного rollout;
- денежный KPI до проверки полноты решений ОКК.

# Change Summary / Spec Delta

- было: качество строки возврата `Новый/Брак` одновременно использовалось как
  предварительный факт и как аналитический вывод;
- станет: исходное качество создаёт кандидата, а итог ОКК становится отдельным
  неизменяемым решением с причиной и исходом товара;
- не меняется: 1С остаётся системой учёта документов, остатков и физического
  качества; backend не проводит документы 1С.

# Acceptance Criteria

- [x] Исходный blocker качества остаётся `0/manual_review` после адаптивного расчёта.
- [x] Повторный sync возврата не затирает итоговое решение ОКК.
- [x] `confirmed_ok_after_check` не входит в подтверждённый товарный брак.
- [x] Возврат рабочего товара в оборот требует ссылки на `Корректировку качества`.
- [x] Транспортное повреждение не попадает в показатель дефекта поставщика/товара.
- [ ] Из 1С read-only поступают стабильный ключ строки возврата и связанная
      корректировка качества.
- [ ] Продажи и подтверждённые решения считаются на одном 90-дневном окне.
- [ ] После shadow-периода показатель подключён к закупочному решению.

# Source of Truth

- 1С УТ 10.3: возврат, перемещение, корректировка качества, остаток и продажи;
- `quality_case`: owner, SLA, итоговая классификация ОКК и audit trail;
- Bitrix24: обзор, очередь и производные задачи, но не аналитический факт.

# Data Flow

```text
Возврат 1С -> read-only sync -> quality_case.pending_review
-> проверка ОКК -> decision_recorded
-> при возврате в оборот ссылка на Корректировку качества
-> агрегат подтверждённых исходов
-> shadow defect rate -> закупочный blocker после приёмки
```

# API / Data Contracts

- `POST /api/quality/sync/cases` — идемпотентное создание/обновление исходного
  кандидата;
- `GET /api/quality/cases` и `/history` — очередь и audit trail;
- `POST /api/quality/cases/{id}/start-review` — начало проверки;
- `POST /api/quality/cases/{id}/decision` — итог ОКК;
- `GET /api/quality/metrics/quality` — количества по исходам без подмены
  знаменателя продаж.

Формула после подключения продаж:

```text
confirmed_product_defect_rate =
confirmed_product_defect_qty / gross_sales_qty_same_window * 100
```

# Invariants

- предварительное качество продавца не равно подтверждённому браку;
- pending не интерпретируется как `0%`;
- решение ОКК не затирается повторным source sync;
- транспорт, внутренняя обработка и ошибка ревизии не смешиваются с товарным браком;
- любой blocker сохраняет нулевой автозаказ во всех downstream-шагах.

# Errors / Edge Cases

- повторная доставка события дедуплицируется `idempotency_key`;
- возврат к продаже без ссылки на корректировку отклоняется `422`;
- повторное решение по закрытому кейсу отклоняется `409`;
- при недоступном backend исходные данные 1С не объявляются подтверждённым браком.

# Implementation Checklist

- [x] Исправить adaptive blocker leak и добавить двойной защитный барьер.
- [x] Добавить модели `quality_case`/`quality_case_event` и Alembic-миграцию.
- [x] Добавить API, сервис, итоговые причины и метрики количества.
- [x] Добавить unit/regression tests.
- [ ] Реализовать read-only exporter строк возврата/корректировок из УТ 10.3.
- [ ] Выполнить backfill задачи №2587 как shadow data, без влияния на заказ.
- [ ] Добавить общий 90-дневный знаменатель продаж и confidence/data-quality поля.
- [ ] Провести shadow-пилот и только после сверки включить confirmed rate в закупку.

# Review Notes / Risks

- Активный cron пока запускается из mutable-root; rollout должен идти через clean
  release и миграционный gate.
- Старые документы могут не иметь однозначной связи с корректировкой качества.
- Один возврат может содержать несколько строк: ключ обязан быть построчным.

# Tests

- `tests/test_quality_case.py`;
- `tests/test_report_display_auto_order_adaptive_lead_time_comparison_task.py`;
- `tests/test_build_display_auto_order_dry_run_task.py`;
- migration upgrade/downgrade на временной БД;
- ручная сверка кейсов РБ000032998, РБ000055338 и РБ000057820.

# Rollout

1. Собрать clean release и применить миграцию.
2. Включить read-only sync и shadow-метрики без влияния на заказ.
3. Сверить решения ОКК минимум за 30 дней.
4. Подключить подтверждённый процент к review/blocker отдельным изменением.
5. Rollback: отключить sync/endpoint consumer и откатить миграцию; документы 1С
   не затрагиваются.

# Changelog

- 2026-08-03 — контракт принят, backend foundation и защита blocker реализованы.
