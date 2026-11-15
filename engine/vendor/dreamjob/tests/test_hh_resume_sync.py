from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from candidate.hh_official import HHOfficialError
from candidate.hh_resume_sync import (
    DictionaryResolutionError,
    HHDictionaries,
    HHResumeApplyError,
    ProfileValidationError,
    apply_profile,
    build_payload,
    create_resume,
    diff,
    load_profile,
    publish,
    restore,
    snapshot,
)


# ---------------------------------------------------------------------------
# Fakes and fixtures — no real network call anywhere in this file.
# ---------------------------------------------------------------------------

class FakeHHClient:
    """Mimics the parts of HHOfficialClient this module touches."""

    def __init__(self):
        self.calls: list[dict] = []
        self.last_response_headers: dict[str, str] = {}
        self.last_response_status: int = 0
        self._responses: dict[tuple[str, str], dict] = {}

    def queue(self, method: str, url: str, *, body=None, status: int = 200, headers=None, error: HHOfficialError | None = None):
        self._responses[(method, url)] = {"body": body, "status": status, "headers": headers or {}, "error": error}

    def _request(self, method, url, data=None, *, auth=True, json_data=None):
        self.calls.append({"method": method, "url": url, "json_data": json_data})
        key = (method, url)
        if key not in self._responses:
            raise AssertionError(f"FakeHHClient: no queued response for {method} {url}")
        spec = self._responses[key]
        if spec["error"] is not None:
            self.last_response_status = spec["error"].status
            raise spec["error"]
        self.last_response_status = spec["status"]
        self.last_response_headers = spec["headers"]
        return spec["body"] if spec["body"] is not None else {}

    def resume(self, resume_id):
        return self._request("GET", f"https://api.hh.ru/resumes/{resume_id}")


DICTIONARIES_BODY = {
    "employment": [
        {"id": "full", "name": "Полная занятость"},
        {"id": "project", "name": "Проектная работа"},
    ],
    "schedule": [
        {"id": "remote", "name": "Удаленная работа"},
        {"id": "fullDay", "name": "Полный день"},
    ],
    "relocation_type": [
        {"id": "no_relocation", "name": "Не готов к переезду"},
        {"id": "living_or_relocation", "name": "Хочу переехать или уже живу тут"},
    ],
    "business_trip_readiness": [
        {"id": "ready", "name": "Готов к командировкам"},
        {"id": "never", "name": "Не готов к командировкам"},
    ],
    "education_level": [
        {"id": "higher", "name": "Высшее"},
        {"id": "unfinished_higher", "name": "Неоконченное высшее"},
    ],
    "language_level": [
        {"id": "l1", "name": "Родной"},
        {"id": "c1", "name": "Продвинутый"},
    ],
    "currency": [
        {"code": "RUR", "name": "руб."},
        {"code": "USD", "name": "долл."},
    ],
}

PROFESSIONAL_ROLES_BODY = {
    "categories": [
        {
            "id": "cat1",
            "name": "Информационные технологии",
            "roles": [
                {"id": "10", "name": "Тестовый специалист"},
                {"id": "11", "name": "Аналитик"},
            ],
        },
    ],
}

AREAS_BODY = [
    {
        "id": "demo-country",
        "name": "Тестовая страна",
        "areas": [
            {"id": "demo-area", "name": "Тестовый регион", "areas": []},
            {"id": "old-area", "name": "Старый тестовый регион", "areas": []},
        ],
    },
]


LANGUAGES_BODY = [
    {"id": "rus", "name": "Русский"},
    {"id": "eng", "name": "Английский"},
]


def _queue_dictionaries(client: FakeHHClient) -> None:
    client.queue("GET", "https://api.hh.ru/dictionaries", body=DICTIONARIES_BODY)
    client.queue("GET", "https://api.hh.ru/professional_roles", body=PROFESSIONAL_ROLES_BODY)
    client.queue("GET", "https://api.hh.ru/areas", body=AREAS_BODY)
    client.queue("GET", "https://api.hh.ru/languages", body=LANGUAGES_BODY)


def _profile_fixture() -> dict:
    return {
        "profile_key": "demo_profile",
        "title": "Тестовый специалист",
        "salary": {"amount": 1, "currency": "RUR"},
        "area": "Тестовый регион",
        "relocation": "no_relocation",
        "employments": ["full", "project"],
        "schedules": ["remote", "fullDay"],
        "about": "Полностью вымышленное описание тестового профиля.",
        "skill_set": ["Тестовый навык", "Документация"],
        "experience": [
            {
                "company": "Вымышленная компания", "position": "Тестовый специалист",
                "start": "2020-01-01", "end": None, "area": "Тестовый регион",
                "description": "Демонстрационная запись без реального опыта.",
            },
        ],
        "education": {
            "level": "Неоконченное высшее",
            "primary": [{"name": "Вымышленный университет", "organization": "Тестовый факультет", "result": "", "year": 2000}],
        },
        "languages": [{"name": "Русский", "level": "Родной"}],
        "professional_roles": ["Тестовый специалист"],
    }


def _current_resume_fixture() -> dict:
    return {
        "id": "resume-1",
        "title": "Старый тестовый заголовок",
        "skills": "Старое summary текста о себе.",
        "skill_set": ["Старый навык"],
        "area": {"id": "old-area", "name": "Старый тестовый регион"},
        "relocation": {"type": {"id": "living_or_relocation", "name": "Хочу переехать или уже живу тут"}},
        "employments": [{"id": "part", "name": "Частичная занятость"}],
        "schedules": [{"id": "flexible", "name": "Гибкий график"}],
        "education": {"level": {"id": "higher", "name": "Высшее"}, "primary": []},
        "language": [{"name": "Английский", "level": {"id": "c1", "name": "Продвинутый"}}],
        "salary": {"amount": 2, "currency": "USD"},
        "experience": [{
            "company": "Другая вымышленная компания", "position": "Старая тестовая роль",
            "start": "2018-01-01", "end": "2019-01-01", "area": "Старый тестовый регион",
            "description": "Старая демонстрационная запись.",
        }],
        "professional_roles": [{"id": "11", "name": "Аналитик"}],
        "first_name": "Тест", "last_name": "Пользователь", "middle_name": "",
        "birth_date": "2000-01-01",
        "gender": {"id": "male", "name": "Мужской"},
        "photo": {"id": "photo-demo", "url": "https://example.invalid/photo"},
        "contact": [{"type": {"id": "cell"}, "value": {"formatted": "+0 000 000-00-00"}}],
        "created_at": "2020-01-01T00:00:00+0300",
        "updated_at": "2020-01-01T00:00:00+0300",
        "status": {"id": "published"},
        "url": "https://api.hh.ru/resumes/resume-1",
    }


# ---------------------------------------------------------------------------
# 1. load_profile
# ---------------------------------------------------------------------------

class LoadProfileTest(unittest.TestCase):
    def test_missing_top_level_field_names_the_field(self):
        profile = _profile_fixture()
        del profile["salary"]
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "demo_profile.json").write_text(json.dumps(profile), encoding="utf-8")
            with self.assertRaises(ProfileValidationError) as ctx:
                load_profile("demo_profile", root=root)
        self.assertIn("salary", str(ctx.exception))

    def test_profile_key_mismatch_is_rejected(self):
        profile = _profile_fixture()
        profile["profile_key"] = "someone_else"
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "demo_profile.json").write_text(json.dumps(profile), encoding="utf-8")
            with self.assertRaises(ProfileValidationError) as ctx:
                load_profile("demo_profile", root=root)
        self.assertIn("profile_key", str(ctx.exception))

    def test_bad_experience_date_is_rejected(self):
        profile = _profile_fixture()
        profile["experience"][0]["start"] = "01.02.2022"
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "demo_profile.json").write_text(json.dumps(profile), encoding="utf-8")
            with self.assertRaises(ProfileValidationError) as ctx:
                load_profile("demo_profile", root=root)
        self.assertIn("experience[0].start", str(ctx.exception))

    def test_invalid_json_is_rejected(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "demo_profile.json").write_text("{not json", encoding="utf-8")
            with self.assertRaises(ProfileValidationError):
                load_profile("demo_profile", root=root)

    def test_valid_profile_loads(self):
        profile = _profile_fixture()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "demo_profile.json").write_text(json.dumps(profile), encoding="utf-8")
            loaded = load_profile("demo_profile", root=root)
        self.assertEqual(loaded["title"], "Тестовый специалист")


# ---------------------------------------------------------------------------
# 2. HHDictionaries
# ---------------------------------------------------------------------------

class HHDictionariesTest(unittest.TestCase):
    def test_unmappable_value_lists_valid_options(self):
        with TemporaryDirectory() as tmp:
            client = FakeHHClient()
            _queue_dictionaries(client)
            dictionaries = HHDictionaries(client=client, cache_path=Path(tmp) / "cache.json")
            with self.assertRaises(DictionaryResolutionError) as ctx:
                dictionaries.resolve_employment("не существует")
        message = str(ctx.exception)
        self.assertIn("Полная занятость", message)
        self.assertIn("Проектная работа", message)
        self.assertIn("не существует", message)

    def test_resolution_is_case_insensitive_by_id_and_name(self):
        with TemporaryDirectory() as tmp:
            client = FakeHHClient()
            _queue_dictionaries(client)
            dictionaries = HHDictionaries(client=client, cache_path=Path(tmp) / "cache.json")
            self.assertEqual(dictionaries.resolve_employment("FULL"), "full")
            self.assertEqual(dictionaries.resolve_relocation("не готов к переезду"), "no_relocation")
            self.assertEqual(dictionaries.resolve_area("тестовый регион"), "demo-area")

    def test_cache_avoids_refetch_within_ttl(self):
        with TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "cache.json"
            client = FakeHHClient()
            _queue_dictionaries(client)
            first = HHDictionaries(client=client, cache_path=cache_path, ttl_seconds=3600)
            first.load()
            self.assertEqual(len(client.calls), 4)

            second = HHDictionaries(client=client, cache_path=cache_path, ttl_seconds=3600)
            self.assertEqual(second.resolve_employment("full"), "full")
            self.assertEqual(len(client.calls), 4, "cached dictionaries must not trigger new HTTP calls")

    def test_force_refresh_refetches(self):
        with TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "cache.json"
            client = FakeHHClient()
            _queue_dictionaries(client)
            first = HHDictionaries(client=client, cache_path=cache_path, ttl_seconds=3600)
            first.load()
            _queue_dictionaries(client)  # re-arm the fake for a second round
            first.load(force_refresh=True)
            self.assertEqual(len(client.calls), 8)


# ---------------------------------------------------------------------------
# 3. build_payload
# ---------------------------------------------------------------------------

class BuildPayloadTest(unittest.TestCase):
    def _dictionaries(self, tmp: str) -> HHDictionaries:
        client = FakeHHClient()
        _queue_dictionaries(client)
        return HHDictionaries(client=client, cache_path=Path(tmp) / "cache.json")

    def test_business_trip_readiness_is_opt_in_and_never_blanks_the_live_resume(self):
        """The profile can opt into business trips, while an absent key must
        leave hh.ru's existing value alone rather than erase it."""
        with TemporaryDirectory() as tmp:
            dictionaries = self._dictionaries(tmp)
            current_resume = _current_resume_fixture()
            current_resume["business_trip_readiness"] = {"id": "never", "name": "Не готов к командировкам"}

            silent = build_payload(_profile_fixture(), dictionaries, current_resume)
            self.assertEqual(silent["business_trip_readiness"], current_resume["business_trip_readiness"])

            stated = _profile_fixture()
            stated["business_trip_readiness"] = "ready"
            payload = build_payload(stated, dictionaries, current_resume)
            self.assertEqual(payload["business_trip_readiness"], {"id": "ready"})

    def test_resolves_ids_and_preserves_carried_over_fields(self):
        with TemporaryDirectory() as tmp:
            dictionaries = self._dictionaries(tmp)
            profile = _profile_fixture()
            current_resume = _current_resume_fixture()
            payload = build_payload(profile, dictionaries, current_resume)

        # Carried over untouched (profile says nothing about them).
        self.assertEqual(payload["photo"], current_resume["photo"])
        self.assertEqual(payload["contact"], current_resume["contact"])
        self.assertEqual(payload["first_name"], "Тест")
        self.assertEqual(payload["birth_date"], "2000-01-01")
        self.assertEqual(payload["gender"], current_resume["gender"])
        # Never carries over read-only/meta fields.
        self.assertNotIn("id", payload)
        self.assertNotIn("url", payload)
        self.assertNotIn("status", payload)
        self.assertNotIn("created_at", payload)

        # Profile-controlled fields resolved to dictionary ids.
        self.assertEqual(payload["title"], "Тестовый специалист")
        self.assertEqual(payload["skills"], profile["about"])
        self.assertEqual(payload["skill_set"], profile["skill_set"])
        self.assertEqual(payload["area"], {"id": "demo-area"})
        self.assertEqual(payload["relocation"], {"type": {"id": "no_relocation"}})
        self.assertEqual(payload["employments"], [{"id": "full"}, {"id": "project"}])
        self.assertEqual(payload["schedules"], [{"id": "remote"}, {"id": "fullDay"}])
        self.assertEqual(payload["education"]["level"], {"id": "unfinished_higher"})
        self.assertEqual(payload["language"], [{"id": "rus", "name": "Русский", "level": {"id": "l1"}}])
        self.assertEqual(payload["salary"], {"amount": 1, "currency": "RUR"})
        self.assertEqual(payload["professional_roles"], [{"id": "10"}])
        self.assertEqual(payload["experience"][0]["company"], "Вымышленная компания")
        self.assertIsNone(payload["experience"][0]["end"])

    def test_unmappable_value_raises_before_writing_anything(self):
        with TemporaryDirectory() as tmp:
            dictionaries = self._dictionaries(tmp)
            profile = _profile_fixture()
            profile["area"] = "Город которого нет"
            current_resume = _current_resume_fixture()
            with self.assertRaises(DictionaryResolutionError):
                build_payload(profile, dictionaries, current_resume)

    def test_truncation_is_reported_not_silent(self):
        with TemporaryDirectory() as tmp:
            dictionaries = self._dictionaries(tmp)
            profile = _profile_fixture()
            profile["title"] = "Очень длинный заголовок резюме " * 5  # > 100 chars
            profile["skill_set"] = [f"Навык {i}" for i in range(40)]  # > 30 items
            current_resume = _current_resume_fixture()
            truncations: list[dict] = []
            payload = build_payload(profile, dictionaries, current_resume, truncations=truncations)

        self.assertEqual(len(payload["title"]), 100)
        self.assertEqual(len(payload["skill_set"]), 30)
        fields = {t["field"] for t in truncations}
        self.assertIn("title", fields)
        self.assertIn("skill_set:count", fields)
        title_report = next(t for t in truncations if t["field"] == "title")
        self.assertGreater(title_report["original_length"], title_report["truncated_length"])


# ---------------------------------------------------------------------------
# 4. diff
# ---------------------------------------------------------------------------

class DiffTest(unittest.TestCase):
    def _payload(self):
        with TemporaryDirectory() as tmp:
            client = FakeHHClient()
            _queue_dictionaries(client)
            dictionaries = HHDictionaries(client=client, cache_path=Path(tmp) / "cache.json")
            profile = _profile_fixture()
            current_resume = _current_resume_fixture()
            payload = build_payload(profile, dictionaries, current_resume)
        return profile, current_resume, payload

    def test_diff_is_structured_records_with_russian_labels(self):
        profile, current_resume, payload = self._payload()
        records = diff(current_resume, payload, profile=profile)
        self.assertIsInstance(records, list)
        for record in records:
            self.assertIn("field", record)
            self.assertIn("label", record)
            self.assertIn("old", record)
            self.assertIn("new", record)
            self.assertIn("changed", record)

        by_field = {r["field"]: r for r in records}
        self.assertEqual(by_field["title"]["label"], "Заголовок резюме")
        self.assertTrue(by_field["title"]["changed"])
        self.assertEqual(by_field["title"]["new"], "Тестовый специалист")
        self.assertTrue(by_field["area"]["changed"])
        self.assertEqual(by_field["area"]["new"], "Тестовый регион")
        self.assertTrue(by_field["salary"]["changed"])

    def test_long_text_field_reports_summary_not_full_text(self):
        profile, current_resume, payload = self._payload()
        records = diff(current_resume, payload, profile=profile)
        skills_record = next(r for r in records if r["field"] == "skills")
        self.assertIsInstance(skills_record["old"], dict)
        self.assertIsInstance(skills_record["new"], dict)
        self.assertIn("length", skills_record["new"])
        self.assertIn("first_line", skills_record["new"])
        self.assertEqual(skills_record["new"]["length"], len(payload["skills"]))

    def test_diff_without_profile_still_works(self):
        _, current_resume, payload = self._payload()
        records = diff(current_resume, payload)
        self.assertTrue(any(r["field"] == "title" for r in records))


# ---------------------------------------------------------------------------
# 5 & 6. apply_profile / publish / create_resume / snapshot / restore
# ---------------------------------------------------------------------------

class WriteOperationsTest(unittest.TestCase):
    def test_hh_400_error_becomes_readable_russian_message(self):
        client = FakeHHClient()
        error = HHOfficialError(400, "bad_request", errors=[
            {"field": "area.id", "type": "invalid"},
            {"field": "title", "value": "too_long"},
        ])
        client.queue("PUT", "https://api.hh.ru/resume_profile/resume-1", error=error)
        with self.assertRaises(HHResumeApplyError) as ctx:
            apply_profile(client, "resume-1", {"title": "x" * 200})
        message = str(ctx.exception)
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(len(ctx.exception.field_errors), 2)
        self.assertIn("Регион", message)
        self.assertIn("Заголовок резюме", message)
        self.assertIn("слишком длинное", message)

    def test_apply_profile_success_returns_real_status_and_body(self):
        client = FakeHHClient()
        client.queue("PUT", "https://api.hh.ru/resume_profile/resume-1", body={"id": "resume-1"}, status=204)
        result = apply_profile(client, "resume-1", {"title": "Тестовый специалист"})
        self.assertEqual(result["status"], 204)
        self.assertEqual(result["body"], {"id": "resume-1"})

    def test_publish_success_and_error(self):
        client = FakeHHClient()
        client.queue("POST", "https://api.hh.ru/resumes/resume-1/publish", body={}, status=204)
        result = publish(client, "resume-1")
        self.assertEqual(result["status"], 204)

        client2 = FakeHHClient()
        client2.queue(
            "POST", "https://api.hh.ru/resumes/resume-2/publish",
            error=HHOfficialError(403, "forbidden", errors=[{"field": "resume", "type": "forbidden"}]),
        )
        with self.assertRaises(HHResumeApplyError):
            publish(client2, "resume-2")

    def test_create_resume_returns_id_from_body(self):
        client = FakeHHClient()
        client.queue("POST", "https://api.hh.ru/resumes", body={"id": "new-resume-1"}, status=201)
        resume_id = create_resume(client, {"title": "Тестовый специалист"})
        self.assertEqual(resume_id, "new-resume-1")

    def test_create_resume_returns_id_from_location_header(self):
        client = FakeHHClient()
        client.queue(
            "POST", "https://api.hh.ru/resumes", body={},
            status=201, headers={"Location": "https://api.hh.ru/resumes/new-resume-2"},
        )
        resume_id = create_resume(client, {"title": "Тестовый специалист"})
        self.assertEqual(resume_id, "new-resume-2")

    def test_create_resume_without_id_raises_instead_of_faking_success(self):
        client = FakeHHClient()
        client.queue("POST", "https://api.hh.ru/resumes", body={}, status=204)
        with self.assertRaises(HHResumeApplyError):
            create_resume(client, {"title": "Тестовый специалист"})

    def test_snapshot_and_restore_roundtrip(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "resume-1.snapshot.json"
            current_resume = _current_resume_fixture()
            client = FakeHHClient()
            client.queue("GET", "https://api.hh.ru/resumes/resume-1", body=current_resume)
            saved_path = snapshot(client, "resume-1", path)
            self.assertTrue(saved_path.exists())
            saved = json.loads(saved_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["title"], current_resume["title"])

            client.queue("PUT", "https://api.hh.ru/resume_profile/resume-1", body={}, status=204)
            result = restore(client, "resume-1", saved_path)
            self.assertEqual(result["status"], 204)
            restore_call = next(c for c in client.calls if c["method"] == "PUT")
            restored_payload = restore_call["json_data"]["resume"]
            self.assertEqual(restored_payload["title"], current_resume["title"])
            self.assertNotIn("id", restored_payload)
            self.assertNotIn("url", restored_payload)


if __name__ == "__main__":
    unittest.main()
