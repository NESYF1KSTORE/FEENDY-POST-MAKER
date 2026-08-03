"""Guided brief intake and the clarification loop (FR-002, FR-003, spec §23).

The specification asks for "a Telegram bot or a form plus a brief checklist"
rather than a single free-text box, and for a clarification loop where the
questions the analyst raises come back to the client and their answers produce a
new brief version.

Both live here as data: a checklist of questions, and a small step machine that
walks a chat through them. Handlers stay thin and the wording is in one place.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MIN_ANSWER_LEN = 3
SKIP_WORDS = frozenset({"-", "—", "нет", "не знаю", "пропустить", "skip", "later", "потом"})


@dataclass(frozen=True)
class Question:
    key: str
    prompt: str
    hint: str = ""
    #: Optional questions may be skipped with "-", required ones may not.
    optional: bool = False
    min_length: int = MIN_ANSWER_LEN

    def render(self, index: int, total: int) -> str:
        head = f"<b>Вопрос {index + 1} из {total}</b>\n{self.prompt}"
        if self.hint:
            head += f"\n<i>{self.hint}</i>"
        if self.optional:
            head += "\n\nМожно пропустить — отправьте «-»."
        return head


#: Spec §23 "чек-лист брифа". Ordered so the answers a person gives most easily
#: come first, and nothing blocks on a number they have to look up.
CHECKLIST: tuple[Question, ...] = (
    Question(
        key="goal",
        prompt="Что нужно сделать и зачем?",
        hint="Опишите задачу и результат, который считается успехом.",
        min_length=15,
    ),
    Question(
        key="audience",
        prompt="Кто будет этим пользоваться?",
        hint="Клиенты, сотрудники, партнёры — и примерно сколько их.",
    ),
    Question(
        key="scope",
        prompt="Какие функции обязательно должны быть?",
        hint="Перечислите списком — это станет объёмом работ.",
        min_length=10,
    ),
    Question(
        key="integrations",
        prompt="С чем нужно интегрироваться?",
        hint="CRM, платежи, доставка, 1С, внешние API.",
        optional=True,
    ),
    Question(
        key="deadline",
        prompt="К какому сроку нужен результат?",
        hint="Дата или срок вида «две недели». Если жёсткого срока нет — так и напишите.",
    ),
    Question(
        key="constraints",
        prompt="Что важно учесть или чего делать нельзя?",
        hint="Ограничения по данным, бренду, законодательству, существующей системе.",
        optional=True,
    ),
)


@dataclass
class Progress:
    """Where a chat is inside the checklist. Serialised into `state_data`."""

    project_id: str
    index: int = 0
    answers: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"project_id": self.project_id, "index": self.index, "answers": self.answers}

    @classmethod
    def from_dict(cls, data: dict) -> Progress:
        return cls(
            project_id=data.get("project_id", ""),
            index=int(data.get("index", 0)),
            answers=dict(data.get("answers") or {}),
        )

    @property
    def current(self) -> Question | None:
        if self.index >= len(CHECKLIST):
            return None
        return CHECKLIST[self.index]

    @property
    def finished(self) -> bool:
        return self.index >= len(CHECKLIST)


def is_skip(text: str) -> bool:
    return text.strip().lower() in SKIP_WORDS


def validate(question: Question, text: str) -> str | None:
    """Return an error message, or None when the answer is acceptable."""
    stripped = text.strip()
    if is_skip(stripped):
        if question.optional:
            return None
        return "Этот пункт нужен для оценки — ответьте хотя бы одним предложением."
    if len(stripped) < question.min_length:
        return (
            f"Слишком коротко: нужно хотя бы {question.min_length} символов, "
            "иначе задачу нельзя оценить."
        )
    return None


def record(progress: Progress, question: Question, text: str) -> None:
    if not is_skip(text):
        progress.answers[question.key] = text.strip()
    progress.index += 1


def compose_brief(answers: dict[str, str]) -> str:
    """Render the checklist answers into the text the analyst agent receives."""
    labels = {q.key: q.prompt for q in CHECKLIST}
    lines = []
    for question in CHECKLIST:
        value = answers.get(question.key)
        if value:
            lines.append(f"{labels[question.key]}\n{value}")
    return "\n\n".join(lines)


def summary(answers: dict[str, str]) -> str:
    """Short confirmation shown to the client before the brief is submitted."""
    lines = ["<b>Бриф собран</b>", ""]
    for question in CHECKLIST:
        value = answers.get(question.key)
        lines.append(
            f"• <b>{_short(question.prompt)}</b> — {value if value else '<i>не указано</i>'}"
        )
    return "\n".join(lines)


def _short(prompt: str) -> str:
    return prompt.rstrip("?").strip()


# --------------------------------------------------------------------------
# Clarification loop — FR-003
# --------------------------------------------------------------------------


@dataclass
class Clarification:
    """Answers to the questions the analyst raised on a brief version."""

    project_id: str
    brief_version_id: str
    questions: list[str] = field(default_factory=list)
    index: int = 0
    answers: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "brief_version_id": self.brief_version_id,
            "questions": self.questions,
            "index": self.index,
            "answers": self.answers,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Clarification:
        return cls(
            project_id=data.get("project_id", ""),
            brief_version_id=data.get("brief_version_id", ""),
            questions=list(data.get("questions") or []),
            index=int(data.get("index", 0)),
            answers=dict(data.get("answers") or {}),
        )

    @property
    def current(self) -> str | None:
        if self.index >= len(self.questions):
            return None
        return self.questions[self.index]

    @property
    def finished(self) -> bool:
        return self.index >= len(self.questions)

    def render_current(self) -> str:
        return (
            f"<b>Уточнение {self.index + 1} из {len(self.questions)}</b>\n"
            f"{self.current}\n\n"
            "<i>Если ответа пока нет — отправьте «-», вопрос останется открытым.</i>"
        )

    def record(self, text: str) -> None:
        question = self.current
        if question is not None and not is_skip(text):
            self.answers[question] = text.strip()
        self.index += 1


def extract_questions(open_questions: list) -> list[str]:
    """Normalise the analyst's output into plain question strings.

    Blocking questions come first: they are the ones stopping the estimate.
    """
    blocking: list[str] = []
    rest: list[str] = []
    for item in open_questions or []:
        if isinstance(item, dict):
            text = str(item.get("question") or "").strip()
            if not text:
                continue
            (blocking if item.get("blocking") else rest).append(text)
        else:
            text = str(item).strip()
            if text:
                rest.append(text)
    return blocking + rest
