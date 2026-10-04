import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, InvalidTransition, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class QuarantineFeatureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _register_batch(self, code, credential_no, site="Farm-B"):
        consignment = self.service.create(
            self.actor, "consignment",
            {"code": code, "origin": "Port-A", "destination": site},
        )
        self.service.transition(
            self.actor, consignment["id"], "register",
            {
                "credential_no": credential_no,
                "inspector": "I-1",
                "inspection_result": "suspected",
                "planting_site": site,
            },
        )
        return self.service.get(consignment["id"])

    # --- offline registration + reconciliation ---

    def test_register_records_credential_inspector_conclusion_site(self):
        consignment = self._register_batch("C-1", "CR-1")
        self.assertEqual(consignment["status"], "inspected")
        self.assertEqual(consignment["data"]["credential_no"], "CR-1")
        self.assertEqual(consignment["data"]["inspector"], "I-1")
        self.assertEqual(consignment["data"]["inspection_result"], "suspected")
        self.assertEqual(consignment["data"]["planting_site"], "Farm-B")
        self.assertFalse(consignment["data"]["reconciled"])

    def test_reconcile_valid_credential_marks_reconciled(self):
        self.service.create(self.actor, "credential",
                            {"credential_no": "CR-1", "batch_code": "C-1"})
        consignment = self._register_batch("C-1", "CR-1")
        updated = self.service.reconcile_consignment(self.actor, consignment["id"])
        self.assertTrue(updated["data"]["reconciled"])
        self.assertEqual(updated["data"]["reconcile_status"], "valid")
        self.assertFalse(updated["data"].get("shipment_stopped", False))

    def test_reconcile_without_credential_is_pending(self):
        consignment = self._register_batch("C-1", "CR-MISSING")
        updated = self.service.reconcile_consignment(self.actor, consignment["id"])
        self.assertFalse(updated["data"]["reconciled"])
        self.assertEqual(updated["data"]["reconcile_status"], "pending")

    # --- official return/revoke stops batch + planting site shipment ---

    def test_official_return_stops_batch_and_planting_site(self):
        self.service.create(self.actor, "facility", {"name": "Farm-B", "address": "County 1"})
        self.service.create(self.actor, "credential",
                            {"credential_no": "CR-1", "batch_code": "C-1"})
        consignment = self._register_batch("C-1", "CR-1")

        self.service.return_credential(self.actor, "CR-1", "official return")

        held = self.service.get(consignment["id"])
        self.assertTrue(held["data"]["shipment_stopped"])
        self.assertTrue(held["data"]["official_hold"])
        self.assertEqual(held["data"]["official_action"], "returned")

        facilities = self.service.list("facility")
        self.assertTrue(facilities[0]["data"]["shipment_stopped"])

    def test_official_revoke_lifts_only_for_official(self):
        self.service.create(self.actor, "facility", {"name": "Farm-B", "address": "County 1"})
        self.service.create(self.actor, "credential",
                            {"credential_no": "CR-1", "batch_code": "C-1"})
        consignment = self._register_batch("C-1", "CR-1")

        self.service.revoke_credential(self.actor, "CR-1", "revoked")
        held = self.service.get(consignment["id"])
        self.assertTrue(held["data"]["official_hold"])

        self.service.lift_credential(self.actor, "CR-1", "reinstated")
        lifted = self.service.get(consignment["id"])
        self.assertFalse(lifted["data"]["official_hold"])
        self.assertFalse(lifted["data"]["shipment_stopped"])

    # --- manual review cannot overwrite an official interception ---

    def test_manual_review_records_correction_but_cannot_clear_hold(self):
        self.service.create(self.actor, "credential",
                            {"credential_no": "CR-1", "batch_code": "C-1"})
        consignment = self._register_batch("C-1", "CR-1")
        self.service.return_credential(self.actor, "CR-1", "returned")

        held = self.service.get(consignment["id"])
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.actor, held["id"], "release",
                {"pest_found": False, "treatment": "completed"},
            )

        corrected = self.service.correct_consignment(self.actor, held["id"], "复核确认标识无误")
        self.assertEqual(len(corrected["data"]["corrections"]), 1)
        self.assertTrue(corrected["data"]["official_hold"])
        self.assertTrue(corrected["data"]["shipment_stopped"])

    # --- concurrent modification: keep two versions, list conflict ---

    def test_concurrent_modification_keeps_two_versions_and_lists_conflict(self):
        self.service.create(self.actor, "credential",
                            {"credential_no": "CR-1", "batch_code": "C-1"})
        self.service.modify_credential(
            self.actor, "CR-1", {"note": "port-a"},
            port="port-a", submitted_at="2026-10-01T08:00:00+00:00", expected_version=1,
        )
        result = self.service.modify_credential(
            self.actor, "CR-1", {"note": "port-b"},
            port="port-b", submitted_at="2026-10-01T09:00:00+00:00", expected_version=1,
        )
        self.assertTrue(result["conflict"])
        conflict_id = result["conflict_id"]

        conflicts = self.service.list_conflicts(status="open")
        self.assertEqual(len(conflicts), 1)
        conflict = conflicts[0]
        self.assertEqual(conflict["credential_no"], "CR-1")
        self.assertEqual(conflict["version_a"]["port"], "port-a")
        self.assertEqual(conflict["version_b"]["port"], "port-b")
        self.assertEqual(conflict["version_a"]["submitted_at"], "2026-10-01T08:00:00+00:00")
        self.assertEqual(conflict["version_b"]["submitted_at"], "2026-10-01T09:00:00+00:00")

        resolved = self.service.resolve_conflict(self.actor, conflict_id, "b")
        self.assertEqual(resolved["picked"], "b")
        self.assertEqual(resolved["credential"]["data"]["note"], "port-b")
        self.assertEqual(len(self.service.list_conflicts(status="open")), 0)

    # --- credential number backfill with retryable records ---

    def test_upgrade_backfill_fills_credential_no(self):
        self.service.create(self.actor, "credential",
                            {"credential_no": "CR-OLD", "batch_code": "C-OLD"})
        consignment = self.service.create(
            self.actor, "consignment",
            {"code": "C-OLD", "origin": "A", "destination": "B"},
        )
        summary = self.service.upgrade_backfill(self.actor)
        self.assertEqual(summary["backfilled"], 1)
        self.assertEqual(summary["pending"], 0)
        updated = self.service.get(consignment["id"])
        self.assertEqual(updated["data"]["credential_no"], "CR-OLD")

    def test_failed_backfill_leaves_retryable_record(self):
        consignment = self.service.create(
            self.actor, "consignment",
            {"code": "C-MISSING", "origin": "A", "destination": "B"},
        )
        summary = self.service.upgrade_backfill(self.actor)
        self.assertEqual(summary["backfilled"], 0)
        self.assertEqual(summary["pending"], 1)
        records = self.service.list_backfill(status="pending")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["batch_code"], "C-MISSING")
        self.assertEqual(records[0]["attempts"], 1)

        self.service.create(self.actor, "credential",
                            {"credential_no": "CR-NEW", "batch_code": "C-MISSING"})
        retry = self.service.retry_backfill(self.actor, records[0]["id"])
        self.assertEqual(retry["backfilled"], 1)
        updated = self.service.get(consignment["id"])
        self.assertEqual(updated["data"]["credential_no"], "CR-NEW")
        self.assertEqual(len(self.service.list_backfill(status="pending")), 0)


if __name__ == "__main__":
    unittest.main()
