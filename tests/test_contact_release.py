import tempfile
import unittest
from datetime import date
from pathlib import Path

from src.domain import Actor, ConflictError, InvalidTransition, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


ADMIN = Actor("admin", "admin")
INVESTIGATOR = Actor("inv-1", "investigator")


class ContactReleaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.clock = date(2026, 3, 5)
        self.rules = RuleEngine(today=lambda: self.clock)
        self.service = DomainService(self.repo, self.rules)

    def tearDown(self):
        self.tmp.cleanup()

    def _case(self, person_id, onset="2026-01-10"):
        case = self.service.create(
            ADMIN,
            "case",
            {
                "person_id": person_id,
                "onset_date": onset,
                "location": "District-A",
                "symptoms": ["fever"],
            },
        )
        self.service.transition(ADMIN, case["id"], "triage", {"clinician": "C-1"})
        self.service.transition(
            ADMIN, case["id"], "lab_positive", {"lab_id": "L-1", "result": "positive"}
        )
        return case["id"]

    def _recover_and_close(self, case_id, recovered_at="2026-01-20"):
        self.service.transition(
            ADMIN, case_id, "recover", {"recovered_at": recovered_at}
        )
        self.service.transition(
            INVESTIGATOR, case_id, "close", {"outcome": "recovered"}
        )

    def _contact(self, case_ids, exposure_end="2026-01-15"):
        entity = self.service.create(
            INVESTIGATOR,
            "contact",
            {
                "case_ids": case_ids,
                "person_id": "P-2",
                "exposure_start": "2026-01-10",
                "exposure_end": exposure_end,
            },
        )
        return self.service.transition(
            INVESTIGATOR,
            entity["id"],
            "begin_followup",
            {"followup_start": "2026-01-16", "due_at": "2026-01-29"},
        )

    def _complete(self, contact_id, as_of="2026-03-05"):
        return self.service.transition(
            INVESTIGATOR,
            contact_id,
            "complete_followup",
            {"outcome": "no symptoms", "as_of": as_of},
        )

    def test_create_normalizes_multiple_and_single_case_links(self):
        case_a, case_b = self._case("P-A"), self._case("P-B")
        contact = self.service.create(
            INVESTIGATOR,
            "contact",
            {
                "case_ids": [case_a, case_b],
                "person_id": "P-Multi",
                "exposure_start": "2026-01-10",
                "exposure_end": "2026-01-12",
            },
        )
        self.assertEqual(contact["data"]["case_ids"], [case_a, case_b])
        self.assertEqual(contact["data"]["case_id"], case_a)
        self.assertEqual(len(contact["data"]["links"]), 2)
        self.assertEqual(
            contact["release"]["linked_cases"], [case_a, case_b]
        )

        single = self.service.create(
            INVESTIGATOR,
            "contact",
            {"case_id": case_a, "person_id": "P-Single", "exposure_start": "2026-01-10"},
        )
        self.assertEqual(single["data"]["case_ids"], [case_a])
        self.assertEqual(
            single["data"]["links"][0]["exposure_end"], "2026-01-10"
        )

    def test_open_linked_case_blocks_release_and_list_explains_it(self):
        case_a = self._case("P-A")
        case_b = self._case("P-B")
        self._recover_and_close(case_a)
        contact = self._contact([case_a, case_b])

        view = self.service.get(contact["id"])
        self.assertFalse(view["release"]["eligible"])
        self.assertIsNone(view["release"]["earliest_release_date"])
        blocker = next(
            item for item in view["release"]["blockers"] if item["code"] == "case_open"
        )
        self.assertEqual(blocker["case_id"], case_b)
        self.assertEqual(blocker["case_status"], "confirmed")
        self.assertEqual(
            [item["case_id"] for item in view["release"]["pending_cases"]], [case_b]
        )

        with self.assertRaises(InvalidTransition) as raised:
            self._complete(contact["id"])
        message = str(raised.exception)
        self.assertIn(case_b, message)
        self.assertIn("尚未康复或关闭", message)
        self.assertIn("最早可解除时间待阻塞因素消除后确定", message)

        self._recover_and_close(case_b)
        released = self._complete(contact["id"])
        self.assertEqual(released["status"], "completed")
        self.assertEqual(released["data"]["released_at"], "2026-03-05")
        # 联系史完整保留
        self.assertEqual(released["data"]["case_ids"], [case_a, case_b])
        self.assertEqual(len(released["data"]["links"]), 2)

    def test_observation_window_blocks_until_earliest_date(self):
        case_a = self._case("P-A")
        self._recover_and_close(case_a)
        contact = self._contact([case_a], exposure_end="2026-03-01")

        view = self.service.get(contact["id"])
        blocker = view["release"]["blockers"][0]
        self.assertEqual(blocker["code"], "observation_window")
        self.assertEqual(view["release"]["earliest_release_date"], "2026-03-15")

        with self.assertRaises(InvalidTransition):
            self._complete(contact["id"], as_of="2026-03-14")
        released = self._complete(contact["id"], as_of="2026-03-15")
        self.assertEqual(released["status"], "completed")

    def test_symptoms_block_release(self):
        case_a = self._case("P-A")
        self._recover_and_close(case_a)
        contact = self._contact([case_a])

        self.service.transition(
            INVESTIGATOR,
            contact["id"],
            "report_symptoms",
            {"symptoms": ["cough", "fever"], "symptom_onset": "2026-02-28"},
        )
        view = self.service.get(contact["id"])
        self.assertEqual(view["status"], "following")
        self.assertTrue(view["data"]["symptomatic"])
        blocker = next(
            item for item in view["release"]["blockers"] if item["code"] == "symptomatic"
        )
        self.assertIn("报告症状", blocker["message"])
        self.assertIsNone(view["release"]["earliest_release_date"])

        with self.assertRaises(InvalidTransition) as raised:
            self._complete(contact["id"])
        self.assertIn("需转病例排查", str(raised.exception))

    def test_case_reopen_downgrades_released_contact_and_todo_result(self):
        case_a = self._case("P-A")
        case_b = self._case("P-B")
        self._recover_and_close(case_a)
        self._recover_and_close(case_b)
        contact = self._contact([case_a, case_b])
        released = self._complete(contact["id"])
        self.assertEqual(released["status"], "completed")

        # 来源病例转归回退（其他病例仍关闭）：已解除结果必须下调
        self.service.transition(
            ADMIN, case_a, "reopen", {"reason": "relapsed, needs reinvestigation"}
        )

        downgraded = self.service.get(contact["id"])
        self.assertEqual(downgraded["status"], "following")
        self.assertIsNone(downgraded["data"]["released_at"])
        self.assertEqual(len(downgraded["data"]["release_history"]), 1)
        self.assertEqual(
            downgraded["data"]["release_history"][0]["released_at"], "2026-03-05"
        )
        self.assertTrue(
            any(
                item["reason"] == "case_outcome_changed"
                and item["trigger_case_id"] == case_a
                for item in downgraded["data"]["release_revoked"]
            )
        )
        # 联系史仍保留
        self.assertEqual(downgraded["data"]["case_ids"], [case_a, case_b])
        self.assertEqual(len(downgraded["data"]["links"]), 2)
        blocker = next(
            item for item in downgraded["release"]["blockers"] if item["code"] == "case_open"
        )
        self.assertEqual(blocker["case_id"], case_a)

        with self.assertRaises(InvalidTransition):
            self._complete(contact["id"])

        audit = [
            item
            for item in self.service.audit_log(contact["id"])
            if item["action"] == "resume_followup"
        ]
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["from_status"], "completed")
        self.assertEqual(audit[0]["to_status"], "following")
        self.assertEqual(audit[0]["detail"]["trigger_case_id"], case_a)

        # 病例再次康复关闭后，可以重新解除
        self.service.transition(
            ADMIN,
            case_a,
            "lab_positive",
            {"lab_id": "L-2", "result": "detected"},
        )
        self.service.transition(
            ADMIN, case_a, "recover", {"recovered_at": "2026-03-04"}
        )
        self.service.transition(
            INVESTIGATOR, case_a, "close", {"outcome": "recovered"}
        )
        again = self._complete(contact["id"])
        self.assertEqual(again["status"], "completed")

    def test_linking_open_case_after_release_downgrades(self):
        case_a = self._case("P-A")
        case_b = self._case("P-B")
        self._recover_and_close(case_a)
        contact = self._contact([case_a])
        self._complete(contact["id"])

        # 已解除后关联仍在传染期的病例，不能照旧放行
        linked = self.service.transition(
            INVESTIGATOR,
            contact["id"],
            "link_case",
            {
                "case_id": case_b,
                "exposure_start": "2026-02-20",
                "exposure_end": "2026-02-22",
            },
        )
        self.assertEqual(linked["status"], "following")
        self.assertEqual(linked["data"]["case_ids"], [case_a, case_b])
        self.assertEqual(
            linked["release"]["last_exposure_end"], "2026-02-22"
        )
        with self.assertRaises(InvalidTransition):
            self._complete(contact["id"])

        # 同一病例不可重复关联
        with self.assertRaises(ConflictError):
            self.service.transition(
                INVESTIGATOR, contact["id"], "link_case", {"case_id": case_b}
            )

    def test_link_case_keeps_identified_status(self):
        case_a = self._case("P-A")
        case_b = self._case("P-B")
        contact = self.service.create(
            INVESTIGATOR,
            "contact",
            {"case_id": case_a, "person_id": "P-2", "exposure_start": "2026-01-10"},
        )
        linked = self.service.transition(
            INVESTIGATOR, contact["id"], "link_case", {"case_id": case_b}
        )
        self.assertEqual(linked["status"], "identified")
        self.assertEqual(linked["data"]["case_ids"], [case_a, case_b])

    def test_symptom_report_after_release_downgrades(self):
        case_a = self._case("P-A")
        self._recover_and_close(case_a)
        contact = self._contact([case_a])
        self._complete(contact["id"])

        updated = self.service.transition(
            INVESTIGATOR,
            contact["id"],
            "report_symptoms",
            {"symptoms": ["fever"], "symptom_onset": "2026-03-04"},
        )
        self.assertEqual(updated["status"], "following")
        self.assertEqual(
            updated["data"]["release_revoked"][0]["reason"], "symptoms_reported"
        )
        with self.assertRaises(InvalidTransition):
            self._complete(contact["id"])

    def test_missing_case_id_rejected_on_create_and_link(self):
        with self.assertRaises(ValidationError):
            self.service.create(
                INVESTIGATOR,
                "contact",
                {"person_id": "P-X", "exposure_start": "2026-01-10"},
            )
        case_a = self._case("P-A")
        contact = self.service.create(
            INVESTIGATOR,
            "contact",
            {"case_id": case_a, "person_id": "P-X", "exposure_start": "2026-01-10"},
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                INVESTIGATOR, contact["id"], "link_case", {"case_id": "missing-case"}
            )


if __name__ == "__main__":
    unittest.main()
