from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path


_DEFAULT_DATA_ROOT = Path(__file__).resolve().parents[1] / "candidate_data" / "v1"
DATA_ROOT = Path(
    os.environ.get("CANDIDATE_DATA_ROOT", "").strip() or _DEFAULT_DATA_ROOT
).expanduser().resolve()

_INTERNAL_MARKERS = (
    "конфиденциально", "не публиковать", "только для внутреннего использования",
    "do_not_publish", "internal_only",
)
_POLICY_MARKERS = (
    "не выносить", "не раскрывать", "не использовать", "не указывать", "не выдавать",
    "не приписывать", "не позиционировать", "не создавать впечатление", "не считать",
    "нельзя", "требует уточ", "нужно уточ", "запрещ",
)


@dataclass(frozen=True)
class Fact:
    id: str
    text: str
    category: str
    source: str
    source_line: int
    publication_status: str

    @property
    def publishable(self) -> bool:
        return self.publication_status == "public_confirmed"


@dataclass(frozen=True)
class MasterProfile:
    key: str
    name: str
    desired_title: str
    about: str
    achievements: tuple[tuple[str, str], ...]
    skills: tuple[tuple[str, str], ...]
    experience: tuple["ExperienceEntry", ...]
    guardrails: tuple[str, ...]
    markdown: str


@dataclass(frozen=True)
class ExperienceEntry:
    id: str
    title: str
    role: str
    dates: str
    bullet_ids: tuple[str, ...]


@dataclass(frozen=True)
class CandidateKnowledgeBase:
    version: str
    manifest: dict
    facts: tuple[Fact, ...]
    profiles: dict[str, MasterProfile]
    documents: dict[str, str]

    @property
    def public_facts(self) -> tuple[Fact, ...]:
        return tuple(f for f in self.facts if f.publishable)

    def search(self, query: str, limit: int = 10) -> tuple[Fact, ...]:
        needle = query.casefold().strip()
        if not needle:
            return self.facts[:limit]
        return tuple(f for f in self.facts if needle in f.text.casefold() or needle in f.category.casefold())[:limit]

    def status_counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for fact in self.facts:
            result[fact.publication_status] = result.get(fact.publication_status, 0) + 1
        return result


CandidateData = CandidateKnowledgeBase


def profile_manifest(root: Path = DATA_ROOT) -> dict[str, dict]:
    """Return the public profile mapping declared in ``manifest.json``."""
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    profiles = manifest.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise ValueError("Candidate data manifest must declare profiles")
    return profiles


def profile_keys(root: Path = DATA_ROOT) -> tuple[str, ...]:
    return tuple(profile_manifest(root))


def _confirmed_status(text: str) -> str:
    lower = text.lower()
    if any(marker in lower for marker in _POLICY_MARKERS):
        return "do_not_publish"
    if any(marker in lower for marker in _INTERNAL_MARKERS):
        return "internal"
    return "public_confirmed"


def _fact_id(prefix: str, source: str, category: str, text: str) -> str:
    digest = hashlib.sha256(f"{source}\n{category}\n{text}".encode()).hexdigest()[:12]
    return f"{prefix}-{digest}"


def _parse_career_facts(markdown: str) -> tuple[Fact, ...]:
    status = "needs_confirmation"
    prefix = "CF"
    section = ""
    facts: list[Fact] = []
    for line_no, line in enumerate(markdown.splitlines(), start=1):
        if line == "## Подтверждено кандидатом":
            status = "confirmed"
            continue
        if line.startswith("## "):
            status = "needs_confirmation"
            prefix = "EV" if "репозитор" in line.lower() else "CF"
            section = line[3:].strip()
            continue
        if line.startswith("### "):
            section = line[4:].strip()
            continue
        if not line.startswith("- "):
            continue
        text = line[2:].strip()
        publication_status = _confirmed_status(text) if status == "confirmed" else "needs_confirmation"
        actual_prefix = "POL" if publication_status == "do_not_publish" else prefix
        facts.append(Fact(
            _fact_id(actual_prefix, "career_facts.md", section, text), text, section,
            "career_facts.md", line_no, publication_status,
        ))
    return tuple(facts)


def _parse_document_bullets(source: str, markdown: str, status: str, prefix: str) -> tuple[Fact, ...]:
    category = ""
    facts: list[Fact] = []
    for line_no, line in enumerate(markdown.splitlines(), start=1):
        if line.startswith(("## ", "### ")):
            category = line.lstrip("# ").strip()
        if line.startswith("- "):
            text = line[2:].strip()
            facts.append(Fact(
                _fact_id(prefix, source, category, text), text, category,
                source, line_no, status,
            ))
    return tuple(facts)


def _section(markdown: str, title: str) -> str:
    match = re.search(
        rf"^## {re.escape(title)}\s*$\n(.*?)(?=^## |\Z)",
        markdown,
        flags=re.MULTILINE | re.DOTALL,
    )
    return match.group(1).strip() if match else ""


def _public_master_markdown(markdown: str) -> str:
    return markdown.split("## Не переносить в публичное резюме", 1)[0].rstrip()


def _parse_profile(key: str, markdown: str) -> MasterProfile:
    public = _public_master_markdown(markdown)
    h1 = re.search(r"^# (.+)$", markdown, re.MULTILINE)
    desired = re.search(r"Желаемая должность:\s*\*\*(.+?)\*\*", markdown)
    about = _section(public, "О себе")
    guardrails = tuple(
        line[2:].strip()
        for line in _section(markdown, "Не переносить в публичное резюме").splitlines()
        if line.startswith("- ")
    )

    skills_block = _section(public, "Ключевые навыки")
    skill_texts = [line[2:].strip() for line in skills_block.splitlines() if line.startswith("- ")]
    skills = tuple((f"{key}:s{i:02d}", text) for i, text in enumerate(skill_texts, 1))

    achievement_texts: list[str] = []
    for line in public.splitlines():
        if line.startswith("- "):
            text = line[2:].strip()
            if text not in skill_texts and not text.startswith(("Желаемая должность:", "Доход:", "Занятость:", "Формат:", "Местонахождение:")):
                achievement_texts.append(text)
    achievements = tuple(
        (f"{key}:a{i:02d}", text) for i, text in enumerate(dict.fromkeys(achievement_texts), 1)
    )
    achievement_ids = {text: item_id for item_id, text in achievements}
    experience: list[ExperienceEntry] = []
    experience_block = _section(public, "Опыт работы")
    for index, match in enumerate(re.finditer(
        r"^### (.+?)\s*$\n(.*?)(?=^### |\Z)", experience_block,
        flags=re.MULTILINE | re.DOTALL,
    ), 1):
        title, block = match.groups()
        role_match = re.search(r"^\*\*(.+?)\*\*", block, re.MULTILINE)
        role = role_match.group(1).strip() if role_match else title.strip()
        after_role = block[role_match.end():] if role_match else block
        dates = next((line.strip() for line in after_role.splitlines() if line.strip() and not line.startswith("- ")), "")
        bullet_ids = tuple(
            achievement_ids[text]
            for text in (line[2:].strip() for line in block.splitlines() if line.startswith("- "))
            if text in achievement_ids
        )
        experience.append(ExperienceEntry(f"{key}:e{index:02d}", title.strip(), role, dates, bullet_ids))
    return MasterProfile(
        key=key,
        name=h1.group(1).strip() if h1 else key,
        desired_title=desired.group(1).strip() if desired else (h1.group(1).strip() if h1 else key),
        about=about,
        achievements=achievements,
        skills=skills,
        experience=tuple(experience),
        guardrails=guardrails,
        markdown=public,
    )


def reseal_manifest_entry(name: str, root: Path = DATA_ROOT) -> str:
    """Re-pin one file's hash in the manifest after an authorised owner edit.

    The manifest exists so a silently drifted knowledge base cannot be used, and
    `load_candidate_data` refuses to start when a hash does not match. That is
    right for the reviewed fact bank — but the owner edits `hh_profiles/*.json`
    from the Telegram resume screen, and without resealing, his very next edit
    would leave the bot unable to start at all.

    Call this immediately after writing such a file. It re-pins exactly the one
    entry named, so drift in every other file is still caught.
    """
    path = root / name
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.setdefault("files", {})[name] = digest
    tmp = manifest_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(manifest_path)
    return digest


def load_candidate_data(root: Path = DATA_ROOT) -> CandidateKnowledgeBase:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    for name, expected_hash in manifest.get("files", {}).items():
        path = root / name
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            raise ValueError(f"Candidate data manifest mismatch: {name}")
    documents = {
        name: (root / name).read_text(encoding="utf-8")
        for name in (
            "career_facts.md", "repository_evidence.md", "project_architecture_audit.md",
            "project_blueprint.md",
            "distribution_strategy.md", "cover_letter_voice.md", "resume_voice.md",
            "resume_tailoring_prompt.md",
        )
    }
    facts = (
        _parse_career_facts(documents["career_facts.md"])
        + _parse_document_bullets("repository_evidence.md", documents["repository_evidence.md"], "needs_confirmation", "EV")
        + _parse_document_bullets(
            "project_architecture_audit.md", documents["project_architecture_audit.md"],
            "needs_confirmation", "EV",
        )
        + _parse_document_bullets("project_blueprint.md", documents["project_blueprint.md"], "do_not_publish", "POL")
        + _parse_document_bullets("distribution_strategy.md", documents["distribution_strategy.md"], "do_not_publish", "POL")
        + _parse_document_bullets("cover_letter_voice.md", documents["cover_letter_voice.md"], "do_not_publish", "POL")
        + _parse_document_bullets("resume_voice.md", documents["resume_voice.md"], "do_not_publish", "POL")
        + _parse_document_bullets("resume_tailoring_prompt.md", documents["resume_tailoring_prompt.md"], "do_not_publish", "POL")
    )
    profiles = {
        key: _parse_profile(key, (root / spec["resume"]).read_text(encoding="utf-8"))
        for key, spec in profile_manifest(root).items()
    }
    return CandidateKnowledgeBase(manifest["candidate_data_version"], manifest, facts, profiles, documents)
