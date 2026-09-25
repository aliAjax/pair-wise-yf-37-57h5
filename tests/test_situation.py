import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, InvalidTransition
from src.repository import SQLiteRepository
from src.rules import RuleEngine, build_situation, cluster_cases
from src.service import DomainService


class ClusterRulesTest(unittest.TestCase):
    def test_chain_within_window_stays_in_one_group(self):
        cases = [
            {"id": "1", "location": "A", "onset_date": "2026-03-01"},
            {"id": "2", "location": "A", "onset_date": "2026-03-10"},
            {"id": "3", "location": "A", "onset_date": "2026-03-20"},
        ]
        groups = cluster_cases(cases, max_days=14)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["members"], ["1", "2", "3"])

    def test_different_locations_never_cluster(self):
        cases = [
            {"id": "1", "location": "A", "onset_date": "2026-03-01"},
            {"id": "2", "location": "B", "onset_date": "2026-03-02"},
        ]
        self.assertEqual(cluster_cases(cases), [])

    def test_window_break_splits_groups(self):
        cases = [
            {"id": "1", "location": "A", "onset_date": "2026-03-01"},
            {"id": "2", "location": "A", "onset_date": "2026-03-10"},
            {"id": "3", "location": "A", "onset_date": "2026-03-30"},
            {"id": "4", "location": "A", "onset_date": "2026-04-02"},
        ]
        groups = cluster_cases(cases)
        self.assertEqual([group["members"] for group in groups], [["1", "2"], ["3", "4"]])


class SituationServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _case(self, person_id, onset, location):
        return self.service.create(
            self.admin,
            "case",
            {
                "person_id": person_id,
                "onset_date": onset,
                "location": location,
                "symptoms": ["fever"],
            },
        )

    def _contact(self, case_id, person_id, **extra):
        payload = {"case_id": case_id, "person_id": person_id, "exposure_start": "2026-03-01"}
        payload.update(extra)
        return self.service.create(self.admin, "contact", payload)

    def test_clusters_counts_and_overdue_todos(self):
        a1 = self._case("P-1", "2026-03-01", "A")
        a2 = self._case("P-2", "2026-03-10", "A")
        a3 = self._case("P-3", "2026-03-20", "A")
        b1 = self._case("P-4", "2026-03-05", "B")
        b2 = self._case("P-5", "2026-03-08", "B")

        due = self._contact(a1["id"], "P-9")
        self.service.transition(
            self.admin,
            due["id"],
            "begin_followup",
            {"followup_start": "2026-03-02", "due_at": "2026-03-15"},
        )
        done = self._contact(a2["id"], "P-10")
        done = self.service.transition(
            self.admin,
            done["id"],
            "begin_followup",
            {"followup_start": "2026-03-02", "due_at": "2026-03-15"},
        )
        self.service.transition(self.admin, done["id"], "complete_followup", {"outcome": "ok"})

        situation = self.service.situation(as_of="2026-03-20")
        members = {frozenset(group["members"]) for group in situation["clusters"]}
        self.assertIn(frozenset([a1["id"], a2["id"], a3["id"]]), members)
        self.assertIn(frozenset([b1["id"], b2["id"]]), members)
        self.assertEqual(situation["cluster_count"], 2)
        self.assertEqual(situation["risk_case_count"], 5)
        self.assertEqual(situation["overdue_count"], 1)
        self.assertEqual(situation["overdue_followups"][0]["id"], due["id"])
        self.assertEqual(situation["overdue_followups"][0]["days_overdue"], 5)

    def test_risk_path_follows_shared_contact_between_clusters(self):
        a1 = self._case("P-1", "2026-03-01", "A")
        a2 = self._case("P-2", "2026-03-10", "A")
        b1 = self._case("P-4", "2026-03-05", "B")
        b2 = self._case("P-5", "2026-03-08", "B")
        self._contact(a1["id"], "PX")
        self._contact(b2["id"], "PX")

        situation = self.service.situation(as_of="2026-03-20")
        self.assertEqual(len(situation["risk_paths"]), 1)
        path = situation["risk_paths"][0]
        node_ids = [node["id"] for node in path["nodes"]]
        self.assertIn(node_ids[0], [a1["id"], a2["id"]])
        self.assertEqual(node_ids[-1], b2["id"])
        endpoint_kinds = {path["nodes"][0]["kind"], path["nodes"][-1]["kind"]}
        self.assertEqual(endpoint_kinds, {"case"})
        self.assertTrue(all(node["kind"] == "contact" for node in path["nodes"][1:-1]))
        self.assertEqual(path["length"], 3)

    def test_excluded_case_keeps_links_but_is_not_risk_count(self):
        a1 = self._case("P-1", "2026-03-01", "A")
        a2 = self._case("P-2", "2026-03-05", "A")
        b1 = self._case("P-4", "2026-03-04", "B")
        b2 = self._case("P-5", "2026-03-08", "B")
        excluded = self._case("P-6", "2026-03-06", "C")
        self.service.transition(
            self.admin, excluded["id"], "rule_out", {"reason": "lab negative"}
        )

        # Bridge A -> excluded case (via shared contact person PE)
        self._contact(a1["id"], "PE")
        self._contact(excluded["id"], "PE")
        # Bridge excluded case -> B (contact is the excluded case's person P-6)
        self._contact(b1["id"], "P-6")

        situation = self.service.situation(as_of="2026-03-20")
        self.assertEqual(situation["risk_case_count"], 4)
        excluded_ids = [item["id"] for item in situation["excluded_cases"]]
        self.assertEqual(excluded_ids, [excluded["id"]])
        self.assertEqual(len(situation["risk_paths"]), 1)
        node_ids = [node["id"] for node in situation["risk_paths"][0]["nodes"]]
        self.assertIn(excluded["id"], node_ids)

        # Excluded cases leave the normal status flow untouched.
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.admin, excluded["id"], "triage", {"clinician": "C-1"}
            )

    def test_situation_refreshes_after_create_without_touching_records(self):
        before = self.service.situation(as_of="2026-03-20")
        self.assertEqual(before["cluster_count"], 0)
        case = self._case("P-1", "2026-03-01", "A")
        self._case("P-2", "2026-03-05", "A")
        after = self.service.situation(as_of="2026-03-20")
        self.assertEqual(after["cluster_count"], 1)
        # Source records and existing list query stay in their original state.
        self.assertEqual(self.service.get(case["id"])["status"], "reported")
        self.assertEqual(len(self.service.list("case")), 2)


class SituationPureBuilderTest(unittest.TestCase):
    def test_empty_dataset(self):
        situation = build_situation([], [], as_of="2026-03-20")
        self.assertEqual(situation["cluster_count"], 0)
        self.assertEqual(situation["risk_case_count"], 0)
        self.assertEqual(situation["overdue_count"], 0)
        self.assertEqual(situation["risk_paths"], [])


if __name__ == "__main__":
    unittest.main()
