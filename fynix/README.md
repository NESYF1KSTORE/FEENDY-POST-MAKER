# FYNIX AI Pipeline

Control Plane для конвейера цифровых проектов по ТЗ *FYNIX AI Pipeline Technical
Specification v1.1*. Платформа проводит проект по маршруту
**бриф → blueprint → согласование → задачи → PR → gates → evidence → релиз → деплой**,
где AI-агенты делают работу внутри изолированных runner'ов, а человек принимает
решения там, где спецификация этого требует.

**Ключевой инвариант:** AI не имеет безусловного доступа к production, секретам и
клиентской инфраструктуре. Любое изменение проходит политики, тесты,
security-gates и подтверждение ответственного человека согласно уровню риска.

---

## Что уже работает

| Раздел ТЗ | Реализовано |
|---|---|
| §3 Сквозной процесс | Машина состояний S0–S8 с exit gate на каждый переход, идемпотентные переходы, инвалидация downstream при смене брифа |
| §4 Архитектура | Модульный монолит + фоновые jobs; ports/adapters для Git, AI, runner'ов и хранилища |
| §5 AI-оркестрация | 10 типов агентов, единый AI Gateway, маршрутизация по риску/классу данных, защита от prompt injection |
| §6 FR-001…FR-025 | Tenant onboarding, intake, clarification, blueprint, approvals, task DAG, agent runs, code delivery, gates, evidence, environments, deploy, cost control, audit, API, webhooks |
| §8 Данные | 25 сущностей, классификация данных, tenant isolation в запросах, кэше и хранилище |
| §9 API и события | Versioned REST API с Idempotency-Key, доменные события через transactional outbox с DLQ |
| §10 Безопасность | RBAC+ABAC deny-by-default, MFA на привилегированных действиях, изоляция runner'ов, редакция секретов, tamper-evident audit |
| §11 Quality | Гейты G0–G7, secret scan, SAST-lite, dependency/licence policy, SBOM, evidence bundle, waivers со сроком |
| §12 CI/CD | Immutable artifact digest, promotion, canary/blue-green, автоматический rollback, drift detection |
| §14 Cost | Cost ledger, бюджеты 50/80/100 с hard stop и time-bound override |
| §17 AC-01…AC-15 | Покрыты тестами — см. «Соответствие приёмочным критериям» |

Полный конвейер запускается **офлайн, без API-ключей и без расходов**: встроенный
`mock`-провайдер генерирует структурированные ответы по тем же схемам, что и
реальная модель, поэтому вся логика гейтов, git и evidence проверяется по-настоящему.

---

## Быстрый старт

### На сервере (одна команда)

```bash
curl -fsSL https://raw.githubusercontent.com/nesyf1kstore/feendy-post-maker/claude/deploy-project-server-yapfn4/fynix/deploy/install.sh \
  | bash -s -- --domain fynix.example.com
```

Скрипт ставит Docker, разворачивает стек, настраивает firewall и TLS, создаёт
первого админа. Подробности и вариант без домена — в [DEPLOY.md](DEPLOY.md).

### Локально

```bash
cd fynix
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt

export DATABASE_URL="sqlite:///./fynix.db"
export JWT_SECRET="$(openssl rand -hex 32)"
export AI_DEFAULT_PROVIDER=mock

.venv/bin/alembic upgrade head
.venv/bin/python -m app.cli bootstrap            # создаст тенант и админа
.venv/bin/python -m app.cli demo                 # прогонит конвейер целиком
.venv/bin/uvicorn app.main:app --reload          # портал на /portal, API на /docs
```

`demo` доводит проект до состояния `S6_VERIFY`: бриф разобран, blueprint собран и
утверждён, DAG задач построен, каждая задача прошла sandbox → commit → PR → гейты
→ review → merge, релиз собран вместе с evidence bundle.

---

## Архитектура

```
                      ┌──────────────────────────────┐
   Клиент / PM  ───▶  │  API Gateway + Client Portal │
                      └──────────────┬───────────────┘
                                     │
      ┌──────────────────────────────┼──────────────────────────────┐
      │              CONTROL PLANE (метаданные и решения)           │
      │                                                             │
      │  Intake ─▶ Orchestrator (S0–S8) ─▶ Quality ─▶ Delivery      │
      │     │            │      │              │          │         │
      │  Policy      Job queue  Approvals   Gates      Releases      │
      │  Engine      (durable)  (матрица)   G0–G7      Deployments   │
      │                                                             │
      │  Audit (hash chain) · Cost ledger · Outbox events           │
      └──────────────────────────────┬──────────────────────────────┘
                                     │
      ┌──────────────────────────────┴──────────────────────────────┐
      │  EXECUTION PLANE — одноразовые runner'ы                     │
      │  без сети, read-only root, non-root user, квоты, TTL        │
      │  Agent Runtime ─▶ AI Gateway ─▶ Anthropic / DeepSeek / mock │
      └─────────────────────────────────────────────────────────────┘
```

Control Plane хранит метаданные и управляет процессом, но **не исполняет
недоверенный проектный код** — это делает Execution Plane в изолированном
workspace, который уничтожается после запуска.

### Структура кода

```
fynix/
├── app/
│   ├── config.py            конфигурация; отказ стартовать с плейсхолдерами в prod
│   ├── models/              25 сущностей из §8, TZ-нормализованные timestamps
│   ├── core/
│   │   ├── policy.py        RBAC+ABAC, матрица согласований §2.1, tool grants §5.4
│   │   ├── audit.py         append-only журнал с хеш-цепочкой
│   │   ├── tenancy.py       изоляция в запросах, кэше и storage-префиксах
│   │   ├── redaction.py     редакция секретов до логов, промптов и артефактов
│   │   ├── classification.py классы данных, egress-политика, детект инъекций
│   │   └── idempotency.py   Idempotency-Key для мутирующих эндпоинтов
│   ├── orchestrator/
│   │   ├── state_machine.py S0–S8 и exit gates
│   │   ├── dag.py           граф задач, готовность, компенсация
│   │   ├── jobs.py          durable-очередь с лизами и backoff
│   │   ├── events.py        transactional outbox, at-least-once, DLQ
│   │   ├── handlers.py      сам конвейер: intake → blueprint → build → release
│   │   └── worker.py        воркер-процесс
│   ├── ai/
│   │   ├── gateway.py       единая дверь к моделям: класс данных → бюджет → вызов
│   │   ├── routing.py       профили моделей, primary/fallback, ставки для ledger
│   │   └── providers/       anthropic · deepseek · mock
│   ├── agents/              10 агентов: контракт, схема вывода, границы доверия
│   ├── quality/             гейты G0–G7, сканеры, SBOM, evidence bundle
│   ├── delivery/            окружения, релизы, деплой, откат, drift
│   ├── runners/             sandbox: local и docker-драйверы
│   ├── api/v1/              REST API §9.1
│   ├── portal/              портал и админ-консоль (Jinja2, без сборки)
│   └── cli.py               bootstrap · demo · worker · verify-audit · reconcile
├── migrations/              Alembic
├── tests/                   117 тестов
├── deploy/                  install.sh (на сервере) · push.sh (по SSH)
├── Dockerfile · docker-compose.yml · Caddyfile
└── DEPLOY.md
```

---

## Как принимаются решения

### Матрица согласований (§2.1)

| Действие | Кто подтверждает | Правило |
|---|---|---|
| Blueprint | Solution Architect → Client Owner | последовательно, до генерации production-кода |
| Изменение бюджета | Finance/Admin + Client Owner | параллельно, pipeline приостановлен |
| High-risk dependency | Security/Compliance | waiver или замена |
| Deploy в production | Client Owner + DevOps/SRE | двойное подтверждение **разными людьми** |
| Доступ к чувствительным данным | Data Owner + Security | time-bound, least privilege |

Согласование привязано к checksum того, что утверждали. Изменился бриф — blueprint
помечается устаревшим, подписи аннулируются, оценки пересчитываются.

### Бюджеты (§14.1)

| Порог | Поведение |
|---|---|
| 50% | прогноз и рекомендация по оптимизации, ничего не блокируется |
| 80% | уведомление PM/Finance, необязательные eval и retry отключаются |
| 100% | hard stop: новые billable-запуски отклоняются, активные завершаются |
| override | time-bound, с владельцем, суммой, причиной и audit-событием |

### Гейты (§11.1)

`G0` требования · `G1` статика и secret scan · `G2` unit · `G3` интеграция ·
`G4` E2E · `G5` security и лицензии · `G6` производительность · `G7` релиз.

Блокирующий гейт останавливает конвейер. Обойти его можно только waiver'ом от
security/compliance — с обоснованием и сроком действия. У доказательств гейта
есть срок годности: просроченные снова становятся блокером.

---

## Защита от prompt injection (§5.4)

1. Каждый источник контекста помечен уровнем доверия: `trusted`, `project`, `untrusted`.
2. Клиентские файлы, issue, README и веб-страницы попадают в промпт внутри
   `<untrusted_data>` с явной инструкцией не исполнять их содержимое.
3. Перед запуском контекст сканируется на маркеры инъекции; при находке run
   останавливается с `reason=policy_conflict` и уходит человеку.
4. Инструменты выдаются по allowlist на тип агента; `read_secret_value`,
   `push_protected_branch`, `deploy_prod` и `delete_infrastructure` не выдаются никогда.
5. Секреты редактируются до отправки в модель, до логов и до артефактов.

---

## Соответствие приёмочным критериям (§17)

| AC | Критерий | Где проверяется |
|---|---|---|
| AC-01 | Тенант и проект создаются без правки БД; роли ограничены scope | `test_policy_and_isolation.py`, `test_api.py` |
| AC-02 | Versioned blueprint; изменение брифа инвалидирует approval | `test_state_machine.py::test_new_brief_marks_blueprint_and_approvals_stale` |
| AC-03 | Задача создаёт PR, а не прямой commit в main | `test_pipeline_e2e.py::test_agent_cannot_push_to_a_protected_branch` |
| AC-04 | Для run видны model/prompt/policy versions, tools, cost, logs | `test_gateway_and_agents.py::test_agent_run_captures_the_full_contract` |
| AC-05 | Релиз невозможен при blocker gate или просроченном approval | `test_approvals_and_budgets.py`, `test_pipeline_e2e.py` |
| AC-06 | Staging deploy из immutable artifact; повтор идемпотентен | `test_api.py::test_idempotency_key_replays_the_same_project` |
| AC-07 | Production требует двойного approval и проверенного rollback | `test_approvals_and_budgets.py::test_dual_control_needs_two_distinct_people` |
| AC-08 | Secret scan не пускает секрет в repo/artifact/log | `test_dag_jobs_and_quality.py::test_secret_scan_blocks_a_change_set` |
| AC-09 | Cross-tenant тесты подтверждают изоляцию | `test_api.py::test_a_project_of_another_tenant_is_not_found` |
| AC-10 | Budget hard stop блокирует запуски и пишет audit event | `test_approvals_and_budgets.py::test_hard_stop_blocks_new_billable_runs` |
| AC-11 | Failure/retry/cancel не создают дубли; orphan reconciler чистит | `test_dag_jobs_and_quality.py::test_dedupe_key_prevents_duplicate_jobs` |
| AC-13 | Dashboard показывает SLI по API/workflow/runners/AI/cost | `test_api.py::test_dashboard_reports_slis` |
| AC-14 | Путь brief → blueprint → PR → evidence → staging | `test_pipeline_e2e.py::test_full_pipeline_reaches_a_release_with_evidence` |
| AC-15 | Handover: source, SBOM, docs, runbook, release record | `GET /v1/releases/{id}/evidence` |

**AC-12** (backup restore в пределах RPO/RTO) — процедура описана в
[DEPLOY.md](DEPLOY.md#резервное-копирование-и-восстановление); автоматической
проверки восстановления в этой версии нет, её нужно поставить на расписание
после выбора инфраструктуры.

---

## API

Полная спецификация — `GET /docs` (Swagger) и `GET /openapi.json`.

```
POST   /v1/auth/login
POST   /v1/projects                              Idempotency-Key
GET    /v1/projects/{id}                         состояние, gate, прогресс, бюджет
POST   /v1/projects/{id}:transition              переход по S0–S8
POST   /v1/projects/{id}/briefs                  → 202 + operation
POST   /v1/projects/{id}/blueprints:generate     → 202 + operation
GET    /v1/operations/{id}                       статус длительной операции
GET    /v1/approvals/actionable                  что может решить именно этот человек
POST   /v1/approvals/{id}:decide
POST   /v1/waivers                               только security/compliance
POST   /v1/releases                              Idempotency-Key
GET    /v1/releases/{id}/evidence                evidence bundle
POST   /v1/deployments                           Idempotency-Key
POST   /v1/deployments/{id}:health               unhealthy → автоматический откат
POST   /v1/deployments/{id}:rollback
GET    /v1/projects/{id}/costs                   usage и прогноз
POST   /v1/projects/{id}/budget:override         требует MFA
GET    /v1/audit  ·  GET /v1/audit:verify        поиск и проверка хеш-цепочки
POST   /v1/webhooks                              подписанные HMAC-SHA256
GET    /v1/dashboard  ·  /metrics  ·  /health
```

Ошибки возвращаются в едином виде:

```json
{"error": {"code": "approval_required", "message": "...", "details": {"reason": "approval_pending"}}}
```

`details.reason` — машинночитаемый код, по которому клиент может принять решение
без парсинга текста.

---

## Разработка

```bash
.venv/bin/python -m pytest                # 117 тестов
.venv/bin/python -m ruff check app tests  # линт
.venv/bin/alembic revision --autogenerate -m "..."
```

Тесты идут на SQLite и полностью офлайн: ни один тест не ходит в сеть и не тратит
токены. Timestamps нормализуются типом `TZDateTime`, поэтому поведение на SQLite и
PostgreSQL одинаковое.

---

## Что осознанно не сделано

Честный список, чтобы не создавать ложного впечатления о готовности:

- **Реальный workflow-движок.** `orchestrator/jobs.py` даёт durable-очередь с
  лизами, retry и таймерами — этого достаточно для MVP, но это не Temporal.
  Замена — переписывание одного модуля, а не вызывающего кода (решение D-05).
- **Object storage.** Артефакты лежат на диске по content-addressed путям за
  S3-подобным интерфейсом. Для одного VPS этого хватает; для HA нужен S3.
- **Реальные CI-прогоны.** Гейты `G2`/`G3`/`G4` принимают отчёты и корректно по ним
  решают, но сами тесты проекта в этой версии не запускаются в runner'е —
  подключается на этапе 3 плана реализации (§16).
- **GitHub/GitLab драйвер.** Repository Adapter работает с локальным bare-репозиторием.
  Интерфейс тот же, драйвер добавляется отдельно (решение D-03).
- **Автопроверка восстановления из бэкапа** (AC-12) — процедура есть, расписания нет.

---

## Лицензия

MIT — см. [LICENSE](../LICENSE).
