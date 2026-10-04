import tempfile
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    InvalidTransition,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class OfflineQuarantineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.official = Actor("officer-1", "official")
        self.inspector = Actor("insp-1", "inspector")

    def tearDown(self):
        self.tmp.cleanup()

    def _facility(self, code="F-1"):
        return self.service.create(
            self.admin,
            "facility",
            {"name": "苗圃-" + code, "address": "一县", "code": code},
        )

    def _offline_batch(self, code="B-1", facility=None, certificate_no="CERT-1"):
        facility = facility or self._facility()
        return facility, self.service.create(
            self.inspector,
            "consignment",
            {
                "code": code,
                "origin": "口岸A",
                "destination": "苗圃",
                "offline": True,
                "certificate_no": certificate_no,
                "inspector": "张查验",
                "site_conclusion": "未见疑似症状",
                "facility_id": facility["id"],
            },
        )

    def test_offline_registration_then_matched_reconcile(self):
        facility, batch = self._offline_batch()
        self.assertEqual(batch["status"], "offline_registered")
        for field in ("certificate_no", "inspector", "site_conclusion", "facility_id"):
            self.assertIn(field, batch["data"])

        result = self.service.reconcile_many(
            self.official,
            [{"code": "B-1", "certificate_no": "CERT-1", "official_result": "matched"}],
        )
        self.assertEqual(result["results"][0]["outcome"], "reconciled")
        self.assertEqual(self.service.get(facility["id"])["status"], "registered")
        reconciled = self.service.get(batch["id"])
        self.assertEqual(reconciled["status"], "reconciled")
        self.assertTrue(reconciled["data"]["reconciled"])

        inspected = self.service.transition(
            self.inspector,
            batch["id"],
            "inspect",
            {"inspector": "张查验", "inspection_result": "clean"},
        )
        self.assertEqual(inspected["status"], "inspected")

    def test_official_revocation_holds_batch_and_suspends_facility(self):
        facility, batch = self._offline_batch()
        self.service.reconcile_many(
            self.official,
            [{"code": "B-1", "certificate_no": "CERT-1", "official_result": "revoked"}],
        )
        self.assertEqual(self.service.get(batch["id"])["status"], "held")
        # 所属种植点停止调运
        self.assertEqual(self.service.get(facility["id"])["status"], "shipping_suspended")

    def test_manual_release_cannot_override_official_hold(self):
        facility, batch = self._offline_batch()
        self.service.reconcile_many(
            self.official,
            [{"code": "B-1", "certificate_no": "CERT-1", "official_result": "returned"}],
        )
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.inspector,
                batch["id"],
                "inspect",
                {"inspector": "张查验", "inspection_result": "clean"},
            )
        # 人工复核只能留待修正，不能解除拦截
        reviewed = self.service.transition(
            self.inspector,
            batch["id"],
            "review",
            {"review_note": "凭证号录入有误，待修正"},
        )
        self.assertEqual(reviewed["status"], "correction_pending")
        # 停调运未解除
        self.assertEqual(self.service.get(facility["id"])["status"], "shipping_suspended")
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                Actor("q-1", "quarantine"),
                batch["id"],
                "release",
                {"pest_found": False, "treatment": "none"},
            )

    def test_facility_resumes_after_official_reconcile_clears_hold(self):
        facility, batch = self._offline_batch()
        self.service.reconcile_many(
            self.official,
            [{"code": "B-1", "certificate_no": "CERT-1", "official_result": "revoked"}],
        )
        self.service.transition(
            self.inspector, batch["id"], "review", {"review_note": "申诉中"}
        )
        # 修正后官方再次对账通过，拦截解除、种植点恢复调运
        self.service.reconcile_many(
            self.official,
            [{"code": "B-1", "certificate_no": "CERT-1", "official_result": "matched"}],
        )
        self.assertEqual(self.service.get(batch["id"])["status"], "reconciled")
        self.assertEqual(self.service.get(facility["id"])["status"], "registered")

    def test_suspended_facility_rejects_new_batch(self):
        facility, batch = self._offline_batch()
        self.service.reconcile_many(
            self.official,
            [{"code": "B-1", "certificate_no": "CERT-1", "official_result": "revoked"}],
        )
        with self.assertRaises(InvalidTransition):
            self.service.create(
                self.inspector,
                "consignment",
                {
                    "code": "B-2",
                    "origin": "口岸B",
                    "destination": "苗圃",
                    "offline": True,
                    "facility_id": facility["id"],
                },
            )

    def test_reconcile_requires_official_role(self):
        _, batch = self._offline_batch()
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.inspector,
                batch["id"],
                "reconcile",
                {"official_result": "matched"},
            )

    def test_batch_reconcile_continues_after_single_missing_code(self):
        _, batch = self._offline_batch()
        result = self.service.reconcile_many(
            self.official,
            [
                {"code": "MISSING", "certificate_no": "X", "official_result": "matched"},
                {"code": "B-1", "certificate_no": "CERT-1", "official_result": "matched"},
            ],
        )
        outcomes = {item["code"]: item["outcome"] for item in result["results"]}
        self.assertEqual(outcomes["MISSING"], "not_found")
        self.assertEqual(outcomes["B-1"], "reconciled")


class CertificateConflictTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.official = Actor("officer-1", "official")

    def tearDown(self):
        self.tmp.cleanup()

    def _revise(self, port, submitted_at, facility_id=None, base=None, changes=None):
        return self.service.submit_certificate_revision(
            self.official,
            "CERT-77",
            port,
            changes or {"site_conclusion": "adjusted-" + port},
            facility_id=facility_id,
            base_revision_id=base,
            submitted_at=submitted_at,
        )

    def test_two_ports_keep_both_versions_and_list_conflict(self):
        first = self._revise("port-a", "2026-10-04T08:00:00+00:00", "site-a")
        self.assertFalse(first["conflict"])
        second = self._revise(
            "port-b",
            "2026-10-04T09:00:00+00:00",
            "site-b",
            base=first["revision"]["id"],
        )
        self.assertTrue(second["conflict"])

        revisions = self.repo.list_certificate_revisions("CERT-77")
        self.assertEqual(len(revisions), 2)

        conflicts = self.service.list_certificate_conflicts("CERT-77")
        self.assertEqual(len(conflicts), 1)
        versions = conflicts[0]["versions"]
        # 两版都保留，按提交时间、再按种植点排列
        self.assertEqual([item["port"] for item in versions], ["port-a", "port-b"])
        self.assertEqual([item["facility_id"] for item in versions], ["site-a", "site-b"])

    def test_same_port_resubmission_is_not_a_conflict(self):
        first = self._revise("port-a", "2026-10-04T08:00:00+00:00", "site-a")
        second = self._revise(
            "port-a",
            "2026-10-04T08:30:00+00:00",
            "site-a",
            base=first["revision"]["id"],
        )
        self.assertFalse(second["conflict"])
        self.assertEqual(self.service.list_certificate_conflicts("CERT-77"), [])

    def test_conflict_ordering_falls_back_to_facility(self):
        first = self._revise("port-a", "2026-10-04T08:00:00+00:00", "site-z")
        self._revise(
            "port-b",
            "2026-10-04T08:00:00+00:00",
            "site-a",
            base=first["revision"]["id"],
        )
        versions = self.service.list_certificate_conflicts("CERT-77")[0]["versions"]
        self.assertEqual([item["facility_id"] for item in versions], ["site-a", "site-z"])

    def test_only_whitelisted_fields_are_editable(self):
        with self.assertRaises(ValidationError):
            self.service.submit_certificate_revision(
                self.official,
                "CERT-77",
                "port-a",
                {"official_result": "matched"},
            )

    def test_unknown_base_revision_rejected(self):
        with self.assertRaises(NotFoundError):
            self.service.submit_certificate_revision(
                self.official,
                "CERT-77",
                "port-a",
                {"site_conclusion": "x"},
                base_revision_id=999,
            )


class BackfillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _legacy_batch(self, code):
        return self.service.create(
            self.admin,
            "consignment",
            {"code": code, "origin": "老口岸", "destination": "老苗圃"},
        )

    def test_backfill_fills_certificate_no_for_legacy_data(self):
        batch = self._legacy_batch("OLD-1")
        self.assertNotIn("certificate_no", batch["data"])
        result = self.service.backfill_certificate_numbers({"OLD-1": "CERT-OLD-1"})
        self.assertEqual(result["updated"], ["OLD-1"])
        self.assertEqual(result["failed"], [])
        self.assertEqual(
            self.service.get(batch["id"])["data"]["certificate_no"], "CERT-OLD-1"
        )
        # 幂等：再次运行不重复更新
        again = self.service.backfill_certificate_numbers({"OLD-1": "CERT-OLD-1"})
        self.assertEqual(again["updated"], [])

    def test_backfill_failure_leaves_retryable_record(self):
        result = self.service.backfill_certificate_numbers({"GONE": "CERT-X"})
        self.assertEqual(result["failed"], ["GONE"])
        failures = self.service.list_backfill_failures("pending")
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["code"], "GONE")
        self.assertEqual(failures[0]["attempts"], 1)
        self.assertEqual(failures[0]["reason"], "batch not found")

        # 批号到位后重试成功
        self._legacy_batch("GONE")
        retry = self.service.retry_backfill("GONE")
        self.assertTrue(retry["resolved"])
        self.assertEqual(retry["failure"]["status"], "resolved")
        self.assertEqual(self.service.list_backfill_failures("pending"), [])

    def test_retry_increments_attempts_while_still_failing(self):
        self.service.backfill_certificate_numbers({"GONE": "CERT-X"})
        retry = self.service.retry_backfill("GONE")
        self.assertFalse(retry["resolved"])
        self.assertEqual(retry["failure"]["attempts"], 2)
        self.assertEqual(retry["failure"]["status"], "pending")

    def test_empty_certificate_number_is_a_failure(self):
        self._legacy_batch("OLD-2")
        result = self.service.backfill_certificate_numbers({"OLD-2": ""})
        self.assertEqual(result["failed"], ["OLD-2"])
        failure = self.service.list_backfill_failures()[0]
        self.assertEqual(failure["reason"], "missing certificate number")
        retry = self.service.retry_backfill("OLD-2", "CERT-OLD-2")
        self.assertTrue(retry["resolved"])


if __name__ == "__main__":
    unittest.main()
