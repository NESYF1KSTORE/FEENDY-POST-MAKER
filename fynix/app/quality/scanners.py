"""Deterministic scanners backing the security gates (FR-013, AC-08).

These run on the *content of a change set* before anything is committed, so a
secret never reaches the repository, an artifact or a log. They are intentionally
dependency-free and conservative: a false positive costs a waiver conversation,
a false negative costs a leaked credential.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.core.redaction import find_secrets

# --------------------------------------------------------------------------
# Secret scanning
# --------------------------------------------------------------------------

#: Paths whose matches are downgraded — sample configs are expected to contain
#: placeholder-shaped strings.
EXAMPLE_PATH = re.compile(r"(?i)(?:^|/)(?:\.env\.example|.*\.sample|.*\.template|docs?/)")
PLACEHOLDER = re.compile(
    r"(?i)^(?:change[-_ ]?me|your[-_ ].*|xxx+|\.{3,}|<[^>]+>|placeholder|example|dummy|test)$"
)


@dataclass
class Finding:
    rule: str
    severity: str
    file: str
    detail: str
    line: int = 0
    remediation: str = ""

    def key(self) -> str:
        return f"{self.rule}:{self.file}:{self.line}"

    def as_dict(self) -> dict:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "file": self.file,
            "line": self.line,
            "detail": self.detail,
            "remediation": self.remediation,
            "key": self.key(),
        }


def scan_secrets(files: list[dict]) -> list[Finding]:
    """`files` is a list of `{"path": str, "content": str}`."""
    findings: list[Finding] = []
    for entry in files:
        path = entry.get("path", "")
        content = entry.get("content") or ""
        is_example = bool(EXAMPLE_PATH.search(path))
        for hit in find_secrets(content):
            line = content[: hit["start"]].count("\n") + 1
            findings.append(
                Finding(
                    rule=f"secret.{hit['rule']}",
                    severity="medium" if is_example else "critical",
                    file=path,
                    line=line,
                    detail=f"возможный секрет ({hit['rule']}) в тексте файла",
                    remediation="вынести значение в секрет-менеджер и обращаться по ссылке",
                )
            )
    return findings


# --------------------------------------------------------------------------
# Static analysis (SAST-lite)
# --------------------------------------------------------------------------

DANGEROUS_PATTERNS: tuple[tuple[str, str, str, re.Pattern[str]], ...] = (
    (
        "sast.sql_string_concat",
        "high",
        "SQL собирается конкатенацией — используйте параметризованный запрос",
        re.compile(r"(?i)(?:execute|cursor\.execute|query)\s*\(\s*[\"'].*?(?:SELECT|INSERT|UPDATE|DELETE).*?[\"']\s*[%+]"),
    ),
    (
        "sast.shell_injection",
        "high",
        "shell=True с интерполяцией — команда собирается из данных",
        re.compile(r"(?i)subprocess\.(?:run|call|Popen)\([^)]*shell\s*=\s*True"),
    ),
    (
        "sast.eval_exec",
        "high",
        "eval/exec над непроверенными данными",
        re.compile(r"(?<![\w.])(?:eval|exec)\s*\("),
    ),
    (
        "sast.tls_verify_disabled",
        "critical",
        "проверка TLS отключена",
        re.compile(r"(?i)verify\s*=\s*False|rejectUnauthorized\s*:\s*false|InsecureSkipVerify\s*:\s*true"),
    ),
    (
        "sast.weak_hash",
        "medium",
        "слабый хеш для чувствительных данных",
        re.compile(r"(?i)hashlib\.(?:md5|sha1)\s*\("),
    ),
    (
        "sast.destructive_sql",
        "critical",
        "разрушительная операция над схемой или данными",
        re.compile(r"(?i)\b(?:DROP\s+(?:TABLE|DATABASE|SCHEMA)|TRUNCATE\s+TABLE|DELETE\s+FROM\s+\w+\s*;)"),
    ),
    (
        "sast.destructive_fs",
        "critical",
        "рекурсивное удаление файловой системы",
        re.compile(r"rm\s+-rf\s+/(?:\s|$)|shutil\.rmtree\(\s*[\"']/[\"']"),
    ),
    (
        "sast.wildcard_cors",
        "medium",
        "CORS открыт для всех источников вместе с креденшелами",
        re.compile(r"(?i)allow_origins\s*=\s*\[\s*[\"']\*[\"']\s*\]"),
    ),
)


def scan_static(files: list[dict]) -> list[Finding]:
    findings: list[Finding] = []
    for entry in files:
        path = entry.get("path", "")
        content = entry.get("content") or ""
        for rule, severity, detail, pattern in DANGEROUS_PATTERNS:
            for match in pattern.finditer(content):
                findings.append(
                    Finding(
                        rule=rule,
                        severity=severity,
                        file=path,
                        line=content[: match.start()].count("\n") + 1,
                        detail=detail,
                        remediation="переписать безопасным эквивалентом или обосновать waiver",
                    )
                )
    return findings


# --------------------------------------------------------------------------
# Dependency and licence policy (spec §14.2)
# --------------------------------------------------------------------------

LICENSE_ALLOW = frozenset(
    {"MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "ISC", "PSF-2.0", "Unlicense", "Zlib"}
)
LICENSE_REVIEW = frozenset({"MPL-2.0", "LGPL-2.1", "LGPL-3.0", "EPL-2.0", "CDDL-1.0", "UNKNOWN"})
LICENSE_DENY = frozenset({"AGPL-3.0", "GPL-2.0", "GPL-3.0", "SSPL-1.0", "BUSL-1.1", "Commons-Clause"})


def license_policy(license_id: str) -> str:
    """`allow` | `review` | `deny` for a SPDX identifier."""
    normalized = (license_id or "UNKNOWN").strip()
    if normalized in LICENSE_ALLOW:
        return "allow"
    if normalized in LICENSE_DENY:
        return "deny"
    return "review"


REQUIREMENT_LINE = re.compile(r"^\s*([A-Za-z0-9._\-\[\]]+)\s*([=<>!~]=?)\s*([^\s;#]+)")


@dataclass
class Dependency:
    name: str
    version: str = ""
    pinned: bool = False
    license_id: str = "UNKNOWN"
    ecosystem: str = "pypi"
    source_file: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "pinned": self.pinned,
            "license": self.license_id,
            "ecosystem": self.ecosystem,
            "policy": license_policy(self.license_id),
        }


def parse_dependencies(files: list[dict]) -> list[Dependency]:
    """Extract dependencies from requirements.txt / package.json manifests."""
    import json

    deps: list[Dependency] = []
    for entry in files:
        path = entry.get("path", "")
        content = entry.get("content") or ""
        name = path.rsplit("/", 1)[-1]

        if name == "requirements.txt" or name.startswith("requirements"):
            for raw in content.splitlines():
                line = raw.strip()
                if not line or line.startswith(("#", "-r", "--")):
                    continue
                match = REQUIREMENT_LINE.match(line)
                if match:
                    deps.append(
                        Dependency(
                            name=match.group(1),
                            version=match.group(3),
                            pinned=match.group(2) == "==",
                            source_file=path,
                        )
                    )
                else:
                    deps.append(Dependency(name=line, pinned=False, source_file=path))

        elif name == "package.json":
            try:
                manifest = json.loads(content)
            except json.JSONDecodeError:
                continue
            for section in ("dependencies", "devDependencies"):
                for dep_name, spec in (manifest.get(section) or {}).items():
                    version = str(spec)
                    deps.append(
                        Dependency(
                            name=dep_name,
                            version=version,
                            pinned=bool(re.match(r"^\d", version)),
                            ecosystem="npm",
                            source_file=path,
                        )
                    )
    return deps


def scan_dependencies(files: list[dict], known_licenses: dict[str, str] | None = None) -> list[Finding]:
    """Unpinned or policy-denied dependencies are findings (spec §10.2 supply chain)."""
    licenses = known_licenses or {}
    findings: list[Finding] = []
    for dep in parse_dependencies(files):
        dep.license_id = licenses.get(dep.name, dep.license_id)
        if not dep.pinned:
            findings.append(
                Finding(
                    rule="dependency.unpinned",
                    severity="medium",
                    file=dep.source_file,
                    detail=f"зависимость '{dep.name}' не закреплена по версии",
                    remediation="зафиксировать точную версию и обновить lock-файл",
                )
            )
        policy = license_policy(dep.license_id)
        if policy == "deny":
            findings.append(
                Finding(
                    rule="license.denied",
                    severity="high",
                    file=dep.source_file,
                    detail=f"лицензия {dep.license_id} у '{dep.name}' запрещена политикой",
                    remediation="заменить зависимость или получить waiver у security/compliance",
                )
            )
        elif policy == "review" and dep.license_id != "UNKNOWN":
            findings.append(
                Finding(
                    rule="license.review",
                    severity="low",
                    file=dep.source_file,
                    detail=f"лицензия {dep.license_id} у '{dep.name}' требует ручной проверки",
                    remediation="подтвердить совместимость с моделью поставки клиенту",
                )
            )
    return findings


# --------------------------------------------------------------------------
# SBOM (spec §10.2, §14.2)
# --------------------------------------------------------------------------


@dataclass
class SBOM:
    components: list[dict] = field(default_factory=list)
    format: str = "CycloneDX-like/1.0"

    def as_dict(self) -> dict:
        return {
            "bomFormat": self.format,
            "specVersion": "1.0",
            "components": self.components,
        }


def build_sbom(files: list[dict], known_licenses: dict[str, str] | None = None) -> SBOM:
    licenses = known_licenses or {}
    components = []
    for dep in parse_dependencies(files):
        dep.license_id = licenses.get(dep.name, dep.license_id)
        components.append(
            {
                "type": "library",
                "name": dep.name,
                "version": dep.version,
                "purl": f"pkg:{dep.ecosystem}/{dep.name}@{dep.version}" if dep.version else "",
                "licenses": [{"id": dep.license_id}],
                "policy": license_policy(dep.license_id),
            }
        )
    return SBOM(components=components)
