import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _case(self, person_id):
        return self.service.create(
            self.actor,
            "case",
            {
                "person_id": person_id,
                "onset_date": "2026-03-01",
                "location": "A",
                "symptoms": ["fever"],
            },
        )

    def _resolve_case(self, case_id):
        self.service.transition(self.actor, case_id, "triage", {"clinician": "C-1"})
        self.service.transition(
            self.actor, case_id, "lab_positive", {"lab_id": "L-1", "result": "positive"}
        )
        self.service.transition(
            self.actor, case_id, "recover", {"recovered_at": "2026-03-10"}
        )
        self.service.transition(self.actor, case_id, "close", {"outcome": "recovered"})

    def _contact(self, case_ids, exposure_end="2026-02-25"):
        contact = self.service.create(
            self.actor,
            "contact",
            {
                "case_ids": case_ids,
                "person_id": "P-contact",
                "exposure_start": "2026-02-20",
                "exposure_end": exposure_end,
            },
        )
        self.service.transition(
            self.actor,
            contact["id"],
            "begin_followup",
            {"followup_start": "2026-02-26", "due_at": "2026-03-12"},
        )
        return contact

    def _release_info(self, contact_id, as_of="2026-03-20"):
        items = [
            item
            for item in self.service.list("contact", as_of=as_of)
            if item["id"] == contact_id
        ]
        return items[0]["release"]

    def test_create_normalizes_multiple_cases(self):
        c1 = self._case("P-1")
        c2 = self._case("P-2")
        contact = self.service.create(
            self.actor,
            "contact",
            {
                "case_ids": [c1["id"], c2["id"]],
                "person_id": "P-3",
                "exposure_start": "2026-02-20",
            },
        )
        self.assertEqual(contact["data"]["case_ids"], [c1["id"], c2["id"]])
        self.assertEqual(contact["data"]["case_id"], c1["id"])
        self.assertEqual(contact["data"]["exposure_end"], "2026-02-20")

    def test_create_rejects_unknown_case(self):
        with self.assertRaises(ValidationError):
            self.service.create(
                self.actor,
                "contact",
                {
                    "case_ids": ["no-such-case"],
                    "person_id": "P-3",
                    "exposure_start": "2026-02-20",
                },
            )

    def test_list_blocks_on_pending_case(self):
        c1 = self._case("P-1")
        c2 = self._case("P-2")
        self._resolve_case(c1["id"])
        contact = self._contact([c1["id"], c2["id"]])
        release = self._release_info(contact["id"])
        self.assertFalse(release["releasable"])
        self.assertTrue(any(c2["id"] in reason for reason in release["reasons"]))
        self.assertEqual(release["earliest_release_at"], "2026-03-11")
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.actor,
                contact["id"],
                "complete_followup",
                {"outcome": "no symptoms", "as_of": "2026-03-20"},
            )

    def test_list_blocks_on_observation_window(self):
        c1 = self._case("P-1")
        self._resolve_case(c1["id"])
        contact = self._contact([c1["id"]], exposure_end="2026-03-10")
        release = self._release_info(contact["id"], as_of="2026-03-15")
        self.assertFalse(release["releasable"])
        self.assertTrue(any("14" in reason for reason in release["reasons"]))
        self.assertEqual(release["earliest_release_at"], "2026-03-24")
        release = self._release_info(contact["id"], as_of="2026-03-24")
        self.assertTrue(release["releasable"])

    def test_release_follows_case_outcomes(self):
        c1 = self._case("P-1")
        c2 = self._case("P-2")
        contact = self._contact([c1["id"], c2["id"]])
        self._resolve_case(c1["id"])
        self.assertFalse(self._release_info(contact["id"])["releasable"])
        self._resolve_case(c2["id"])
        self.assertTrue(self._release_info(contact["id"])["releasable"])

    def test_release_downgrades_when_new_case_linked(self):
        c1 = self._case("P-1")
        self._resolve_case(c1["id"])
        contact = self._contact([c1["id"]])
        self.assertTrue(self._release_info(contact["id"])["releasable"])
        c2 = self._case("P-2")
        updated = self.service.transition(
            self.actor,
            contact["id"],
            "link_case",
            {"case_id": c2["id"], "exposure_end": "2026-03-05"},
        )
        self.assertEqual(updated["status"], "following")
        self.assertEqual(updated["data"]["case_ids"], [c1["id"], c2["id"]])
        release = self._release_info(contact["id"])
        self.assertFalse(release["releasable"])
        self.assertEqual(release["earliest_release_at"], "2026-03-19")
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.actor, contact["id"], "link_case", {"case_id": "missing"}
            )

    def test_reported_symptoms_block_release(self):
        c1 = self._case("P-1")
        self._resolve_case(c1["id"])
        contact = self._contact([c1["id"]])
        self.assertTrue(self._release_info(contact["id"])["releasable"])
        self.service.transition(
            self.actor,
            contact["id"],
            "report_symptoms",
            {"symptoms": ["cough"], "symptom_onset": "2026-03-15"},
        )
        release = self._release_info(contact["id"])
        self.assertFalse(release["releasable"])
        self.assertTrue(any("症状" in reason for reason in release["reasons"]))
        self.assertEqual(release["earliest_release_at"], "2026-03-29")

    def test_successful_release_keeps_history(self):
        c1 = self._case("P-1")
        c2 = self._case("P-2")
        self._resolve_case(c1["id"])
        self._resolve_case(c2["id"])
        contact = self._contact([c1["id"], c2["id"]])
        updated = self.service.transition(
            self.actor,
            contact["id"],
            "complete_followup",
            {"outcome": "no symptoms", "as_of": "2026-03-20"},
        )
        self.assertEqual(updated["status"], "completed")
        self.assertEqual(updated["data"]["case_ids"], [c1["id"], c2["id"]])
        self.assertEqual(updated["data"]["released_by"], "admin")
        self.assertTrue(updated["data"]["released_at"])
        release = self._release_info(contact["id"])
        self.assertTrue(release["releasable"])
        self.assertEqual(release["case_ids"], [c1["id"], c2["id"]])


if __name__ == "__main__":
    unittest.main()
