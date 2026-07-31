---
spec_id: "pricing-competitor-matching-bitrix-app-v1"
title: "Competitor Matching Bitrix24 App V1"
doc_type: spec
domain: matching
status: accepted
owner: engineering
source_of_truth: false
related_code: [app/api/bitrix_matching.py, app/services/bitrix_matching_auth.py, ui/]
related_tests: [tests/test_matching_api.py]
contracts: [openapi.yaml]
depends_on: [docs/specs/competitor-matching-ui-v1.md]
supersedes: []
rollout_required: true
updated_at: "2026-07-31"
---

# Назначение

Это расширение канонического item-level контура
`docs/specs/competitor-matching-ui-v1.md`; при расхождении приоритет у него.

Встроить текущий ручной интерфейс сопоставления товаров в Bitrix24 как локальное
приложение-страницу, не перенося данные и бизнес-логику в Bitrix24.

# Source of Truth

- `pricing-service` и его БД остаются источником товаров, кандидатов, решений и истории.
- Bitrix24 используется как слой входа, прав доступа и рабочее место оператора.

# API / Data Contracts

- `POST /api/bitrix/matching/session` принимает OAuth-данные Bitrix24,
  проверяет `domain`, `member_id`, `user.current` и, если настроен, whitelist
  пользователей.
- Matching endpoints `/api/matching/*` принимают текущий Basic auth или короткий
  Bearer-token, выпущенный session endpoint.
- Решения оператора пишут `created_by=bitrix:<member_id>:<user_id>` для Bitrix-входа.

# Security

- Не используем cookies для Bitrix iframe; frontend хранит короткую сессию в
  `sessionStorage` и передаёт её через `Authorization: Bearer`.
- Refresh token Bitrix24 не хранится.
- Nginx отключает Basic только для `/bitrix/matching/`,
  `/api/bitrix/matching/session` и `/api/matching/*`; standalone root остаётся под Basic.
- Для `/bitrix/matching/` нужен CSP `frame-ancestors` только на доверенный Bitrix24 portal.

# Tests

- Unit/API: session success, forbidden domain/member/user, Bearer access to matching API,
  Basic backward compatibility.
- Frontend: standalone mode, Bitrix bootstrap, auth error state, build.

# Rollout

- Smoke: `/` требует Basic, `/bitrix/matching/` открывается без Basic, matching API без auth
  возвращает 401, с Bitrix Bearer возвращает товары.
