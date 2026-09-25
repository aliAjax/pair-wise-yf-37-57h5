import tempfile
import unittest
from datetime import date
from pathlib import Path

from src.domain import Actor, InvalidTransition, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService

TODAY = date(2026, 9, 25)


class SituationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _case(self, person_id, location, onset_date):
        return self.service.create(self.admin, "case", {
            "person_id": person_id,
            "onset_date": onset_date,
            "location": location,
            "symptoms": ["fever"],
        })

    def _contact(self, case_id, person_id):
        return self.service.create(self.admin, "contact", {
            "case_id": case_id,
            "person_id": person_id,
            "exposure_start": "2026-03-01",
        })

    def _exclude(self, case):
        self.service.transition(self.admin, case["id"], "triage", {"clinician": "C-1"})
        return self.service.transition(self.admin, case["id"], "exclude", {"reason": "lab negative"})

    def test_clusters_paths_and_overdue_followups(self):
        case_a1 = self._case("P-A1", "School-1", "2026-03-01")
        case_a2 = self._case("P-A2", "School-1", "2026-03-10")
        case_b1 = self._case("P-B1", "Hospital-2", "2026-04-01")
        case_b2 = self._case("P-B2", "Hospital-2", "2026-04-05")
        excluded = self._case("P-E", "School-1", "2026-03-08")
        self._case("P-Solo", "School-1", "2026-05-01")
        self._exclude(excluded)

        bridge = self._contact(case_a2["id"], "P-B1")
        overdue = self._contact(case_a1["id"], "P-C1")
        done = self._contact(case_a1["id"], "P-C2")
        future = self._contact(case_b1["id"], "P-C3")
        self.service.transition(self.admin, overdue["id"], "begin_followup",
                                {"followup_start": "2026-03-02", "due_at": "2026-03-16"})
        self.service.transition(self.admin, done["id"], "begin_followup",
                                {"followup_start": "2026-03-02", "due_at": "2026-03-10"})
        self.service.transition(self.admin, done["id"], "complete_followup", {"outcome": "no symptoms"})
        self.service.transition(self.admin, future["id"], "begin_followup",
                                {"followup_start": "2026-04-02", "due_at": "2099-04-16"})

        situation = self.service.situation(today=TODAY)

        self.assertEqual(len(situation["clusters"]), 2)
        first, second = situation["clusters"]
        self.assertEqual(first["location"], "School-1")
        self.assertEqual(first["case_ids"], [case_a1["id"], case_a2["id"]])
        self.assertEqual(first["onset_start"], "2026-03-01")
        self.assertEqual(first["onset_end"], "2026-03-10")
        self.assertNotIn(excluded["id"], first["case_ids"])
        self.assertEqual(first["risk_persons"], 5)
        self.assertEqual(second["case_ids"], [case_b1["id"], case_b2["id"]])
        self.assertEqual(second["risk_persons"], 3)

        self.assertEqual(len(situation["risk_paths"]), 1)
        risk_path = situation["risk_paths"][0]
        self.assertEqual(risk_path["from_cluster"], first["id"])
        self.assertEqual(risk_path["to_cluster"], second["id"])
        self.assertEqual(
            [node["id"] for node in risk_path["path"]],
            [case_a2["id"], bridge["id"], case_b1["id"]],
        )
        self.assertEqual(
            [node["kind"] for node in risk_path["path"]],
            ["case", "contact", "case"],
        )

        self.assertEqual(len(situation["overdue_followups"]), 1)
        item = situation["overdue_followups"][0]
        self.assertEqual(item["contact_id"], overdue["id"])
        self.assertEqual(item["person_id"], "P-C1")
        self.assertEqual(item["days_overdue"], (TODAY - date(2026, 3, 16)).days)

        self.assertEqual(situation["summary"], {
            "cluster_count": 2,
            "risk_path_count": 1,
            "overdue_count": 1,
            "risk_person_count": 7,
        })

    def test_path_can_route_through_excluded_case(self):
        case_a1 = self._case("P-A1", "School-1", "2026-03-01")
        self._case("P-A2", "School-1", "2026-03-05")
        self._case("P-B1", "Hospital-2", "2026-04-01")
        case_b2 = self._case("P-B2", "Hospital-2", "2026-04-03")
        excluded = self._case("P-E", "School-1", "2026-03-03")
        self._exclude(excluded)

        link_ae = self._contact(case_a1["id"], "P-E")
        link_eb = self._contact(excluded["id"], "P-B2")

        situation = self.service.situation(today=TODAY)
        self.assertEqual(len(situation["clusters"]), 2)
        self.assertEqual(len(situation["risk_paths"]), 1)
        path = situation["risk_paths"][0]["path"]
        self.assertEqual(
            [node["id"] for node in path],
            [case_a1["id"], link_ae["id"], excluded["id"], link_eb["id"], case_b2["id"]],
        )
        by_id = {node["id"]: node for node in path}
        self.assertEqual(by_id[excluded["id"]]["status"], "excluded")

    def test_exclude_requires_reason_and_role(self):
        case = self._case("P-X", "School-1", "2026-03-01")
        with self.assertRaises(PermissionDenied):
            self.service.transition(Actor("viewer", "viewer"), case["id"], "exclude", {"reason": "lab negative"})
        with self.assertRaises(ValidationError):
            self.service.transition(self.admin, case["id"], "exclude", {})
        updated = self.service.transition(self.admin, case["id"], "exclude", {"reason": "lab negative"})
        self.assertEqual(updated["status"], "excluded")

    def test_exclude_from_confirmed_rejected(self):
        case = self._case("P-Y", "School-1", "2026-03-01")
        self.service.transition(self.admin, case["id"], "triage", {"clinician": "C-1"})
        self.service.transition(self.admin, case["id"], "lab_positive", {"lab_id": "L-1", "result": "positive"})
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.admin, case["id"], "exclude", {"reason": "late negative"})


if __name__ == "__main__":
    unittest.main()
