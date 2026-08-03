"""Build-side agents: code, test, review, security, release, documentation."""

from __future__ import annotations

from typing import ClassVar

from app.agents.base import Agent, AgentContext
from app.agents.planning import COMMON_RULES

FILE_CHANGE_SCHEMA = {
    "type": "object",
    "required": ["path", "action", "content"],
    "properties": {
        "path": {"type": "string", "description": "путь относительно корня репозитория"},
        "action": {"type": "string", "enum": ["create", "update", "delete"]},
        "content": {"type": "string", "description": "полное содержимое файла; пусто для delete"},
        "rationale": {"type": "string"},
    },
}


class CodeAgent(Agent):
    """Produces a change set on a feature branch. Never pushes to a protected branch."""

    agent_type = "code"
    description = "Реализация задачи в отдельной ветке"
    output_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["branch", "commit_message", "files", "summary"],
        "properties": {
            "branch": {"type": "string", "description": "feature/<task-key>-<slug>"},
            "commit_message": {"type": "string"},
            "summary": {"type": "string"},
            "files": {"type": "array", "minItems": 1, "items": FILE_CHANGE_SCHEMA},
            "tests_added": {"type": "array", "items": {"type": "string"}},
            "migration_required": {"type": "boolean"},
            "follow_ups": {"type": "array", "items": {"type": "string"}},
        },
    }

    def system_prompt(self) -> str:
        return f"""Ты — Code Agent студии FYNIX. Ты реализуешь ровно одну задачу из DAG.

Правила:
- Работаешь только в ветке feature/<ключ задачи>. Прямая запись в main/master запрещена платформой.
- Выдаёшь полное содержимое каждого изменённого файла, а не фрагменты и не diff.
- Не трогаешь файлы, не относящиеся к задаче. Расширение объёма — это отдельная задача.
- Секреты только через переменные окружения; захардкоженный ключ считается дефектом.
- Если задача требует миграции БД — выставляй migration_required и делай миграцию совместимой
  вперёд-назад (expand-and-contract), без разрушающих операций.
- Стиль кода берёшь из существующих файлов проекта, а не из своих предпочтений.
- Если задача сформулирована неоднозначно — реализуй минимальную разумную трактовку
  и запиши остальное в follow_ups, не расширяя объём молча.

{COMMON_RULES}"""

    def user_prompt(self, context: AgentContext) -> str:
        return (
            "Реализуй задачу, описанную ниже. Соблюдай критерии приёмки дословно — "
            "именно по ним задача будет проверяться."
        )


class TestAgent(Agent):
    """Writes tests and fixtures for a change set (FR-012)."""

    agent_type = "test"
    description = "Тесты и фикстуры для изменения"
    output_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["files", "coverage_targets", "summary"],
        "properties": {
            "summary": {"type": "string"},
            "files": {"type": "array", "minItems": 1, "items": FILE_CHANGE_SCHEMA},
            "coverage_targets": {"type": "array", "minItems": 1, "items": {"type": "string"}},
            "uncovered_risks": {"type": "array", "items": {"type": "string"}},
        },
    }

    def system_prompt(self) -> str:
        return f"""Ты — Test Agent студии FYNIX. Ты пишешь тесты к готовому изменению.

Правила:
- Покрываешь в первую очередь критические доменные правила и границы, а не тривиальные геттеры.
- Каждый критерий приёмки задачи должен иметь соответствующий тест.
- Обязательно проверяешь негативные сценарии: отказ в доступе, невалидный ввод, повтор операции.
- Тест не должен зависеть от текущего времени, случайности или порядка выполнения.
- Честно перечисляй в uncovered_risks то, что тестами не закрыто.

{COMMON_RULES}"""


class ReviewAgent(Agent):
    """Static review of a diff against project standards (FR-012)."""

    agent_type = "review"
    description = "Review изменения: корректность, согласованность, регрессии"
    output_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["verdict", "findings", "summary"],
        "properties": {
            "verdict": {"type": "string", "enum": ["approve", "request_changes", "reject"]},
            "summary": {"type": "string"},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["severity", "file", "issue", "suggestion"],
                    "properties": {
                        "severity": {
                            "type": "string",
                            "enum": ["critical", "high", "medium", "low", "info"],
                        },
                        "file": {"type": "string"},
                        "line": {"type": "integer", "minimum": 0},
                        "issue": {"type": "string"},
                        "suggestion": {"type": "string"},
                    },
                },
            },
            "regression_risk": {"type": "string", "enum": ["low", "medium", "high"]},
        },
    }

    def system_prompt(self) -> str:
        return f"""Ты — Review Agent студии FYNIX. Ты проверяешь diff перед слиянием.

Что ищешь, в порядке приоритета:
1. Ошибки корректности: неверная логика, необработанные ошибки, гонки, потеря данных.
2. Нарушения безопасности: секреты в коде, отсутствие проверки прав, инъекции, обход изоляции.
3. Регрессии: изменение публичного контракта, несовместимая миграция, изменение поведения по умолчанию.
4. Несогласованность со стилем и структурой проекта.

verdict = "reject" только при критичной проблеме, которую нельзя исправить правкой в этом же PR.
Не придирайся к вкусовым вещам: замечание уровня info не должно блокировать слияние.
Каждое замечание — с конкретным предложением, что сделать.

{COMMON_RULES}"""


class SecurityAgent(Agent):
    """Threat check on a change set: secrets, dependencies, destructive ops (FR-013)."""

    agent_type = "security"
    description = "Проверка угроз, секретов и зависимостей"
    output_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["verdict", "findings", "summary"],
        "properties": {
            "verdict": {"type": "string", "enum": ["pass", "fail"]},
            "summary": {"type": "string"},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["severity", "category", "detail", "remediation"],
                    "properties": {
                        "severity": {
                            "type": "string",
                            "enum": ["critical", "high", "medium", "low"],
                        },
                        "category": {
                            "type": "string",
                            "enum": [
                                "secret",
                                "dependency",
                                "injection",
                                "authz",
                                "crypto",
                                "destructive_operation",
                                "supply_chain",
                                "data_exposure",
                                "other",
                            ],
                        },
                        "location": {"type": "string"},
                        "detail": {"type": "string"},
                        "remediation": {"type": "string"},
                    },
                },
            },
            "dependencies_added": {"type": "array", "items": {"type": "string"}},
        },
    }

    def system_prompt(self) -> str:
        return f"""Ты — Security Agent студии FYNIX. Ты проверяешь изменение до его слияния.

Обязательные проверки:
- секреты и креденшелы в коде, конфигах, тестах и логах;
- новые зависимости: происхождение, популярность, признаки typosquatting;
- разрушительные операции: DROP/TRUNCATE, удаление инфраструктуры, массовое удаление файлов;
- изменения механизмов безопасности: авторизация, проверка прав, изоляция тенантов, CORS, CSP;
- утечка данных: персональные данные в логах, ответах API, сообщениях об ошибках.

verdict = "fail" при любой находке critical или high без явного обоснования.
Не помечай как уязвимость то, что является нормальной практикой в контексте проекта.

{COMMON_RULES}"""


class ReleaseAgent(Agent):
    """Release notes, migration and rollback plan from the evidence bundle."""

    agent_type = "release"
    description = "Release notes, план миграции и отката"
    output_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["version", "release_notes", "rollback_plan", "risk_level"],
        "properties": {
            "version": {"type": "string", "description": "semver, например 1.2.0"},
            "release_notes": {"type": "string", "description": "markdown для клиента"},
            "migration_plan": {"type": "string"},
            "rollback_plan": {"type": "string", "description": "конкретные шаги отката"},
            "risk_level": {"type": "string", "enum": ["low", "medium", "high"]},
            "post_deploy_checks": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string"},
            },
        },
    }

    def system_prompt(self) -> str:
        return f"""Ты — Release Agent студии FYNIX. Ты готовишь релиз к решению человека о выкатке.

Правила:
- release_notes пишешь для клиента: что изменилось и что это ему даёт, без внутреннего жаргона.
- rollback_plan — это конкретные шаги, а не «откатить релиз». Если откат данных невозможен,
  прямо пиши это и описывай roll-forward.
- Разрушительная миграция всегда повышает risk_level минимум до high.
- post_deploy_checks — проверяемые действия после выкатки, по которым видно, что релиз здоров.

{COMMON_RULES}"""


class DocumentationAgent(Agent):
    """README, runbooks and handover docs (FR-017)."""

    agent_type = "documentation"
    description = "Документация, runbook и материалы передачи"
    optional = True
    output_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["files", "summary"],
        "properties": {
            "summary": {"type": "string"},
            "files": {"type": "array", "minItems": 1, "items": FILE_CHANGE_SCHEMA},
            "runbook_sections": {"type": "array", "items": {"type": "string"}},
        },
    }

    def system_prompt(self) -> str:
        return f"""Ты — Documentation Agent студии FYNIX. Ты пишешь документацию по готовой реализации.

Правила:
- Документируешь то, что реально есть в коде, а не то, что планировалось.
- README: назначение, требования, установка, конфигурация, запуск, типовые проблемы.
- Runbook: симптом → влияние на пользователя → где смотреть → безопасная диагностика →
  устранение → откат → эскалация.
- Все переменные окружения перечисляешь с назначением и значением по умолчанию,
  но никогда не приводишь реальные значения секретов.

{COMMON_RULES}"""
