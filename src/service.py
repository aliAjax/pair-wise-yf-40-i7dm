from uuid import uuid4

from .audit import AuditTrail
from .domain import Actor, ConflictError, NotFoundError, ValidationError
from .repository import utcnow
from .rules import (
    CERTIFICATE_EDITABLE_FIELDS,
    HELD_STATUSES,
    RuleEngine,
)

SYSTEM_ACTOR = Actor("system", "official")


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind, payload)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        if updated["kind"] == "consignment":
            self._sync_facility_hold(updated)
        return updated

    def _force_status(self, entity, next_status, action, detail, actor=SYSTEM_ACTOR):
        """系统按官方凭证库联动改状态，绕过现场角色校验，但仍留审计。"""
        if entity["status"] == next_status:
            return entity
        updated = self.repository.update_entity(
            entity["id"], entity["version"], next_status, entity["data"]
        )
        self.audit.record(
            entity["id"],
            actor,
            action,
            entity["status"],
            next_status,
            detail,
        )
        return updated

    def _sync_facility_hold(self, consignment):
        """批次被退回/吊销则挂起所属种植点；挂起批次清零后恢复调运。"""
        facility_id = consignment["data"].get("facility_id")
        if not facility_id:
            return
        facility = self.repository.get_entity(facility_id)
        if not facility or facility["kind"] != "facility":
            return
        if consignment["status"] == "held":
            if facility["status"] != "shipping_suspended":
                self._force_status(
                    facility,
                    "shipping_suspended",
                    "suspend_shipping",
                    {"reason": "official interception", "consignment_id": consignment["id"]},
                )
            return
        if facility["status"] == "shipping_suspended" and consignment["status"] not in HELD_STATUSES:
            outstanding = [
                item
                for item in self.repository.list_entities(kind="consignment")
                if item["data"].get("facility_id") == facility_id
                and item["status"] in HELD_STATUSES
            ]
            if not outstanding:
                self._force_status(
                    facility,
                    "registered",
                    "resume_shipping",
                    {"reason": "all interceptions cleared"},
                )

    def reconcile_many(self, actor, items):
        """网络恢复后批量与官方凭证库对账，逐条记录结果。"""
        results = []
        for item in items or []:
            result = {"code": item.get("code"), "certificate_no": item.get("certificate_no")}
            consignments = self.repository.find_entities("consignment", "code", item.get("code"))
            if not consignments:
                result["outcome"] = "not_found"
                results.append(result)
                continue
            consignment = consignments[0]
            try:
                updated = self.transition(
                    actor,
                    consignment["id"],
                    "reconcile",
                    {
                        "certificate_no": item.get("certificate_no"),
                        "official_result": item.get("official_result"),
                    },
                )
                result["outcome"] = updated["status"]
            except Exception as exc:  # 单条失败不阻断整批对账
                result["outcome"] = "failed"
                result["error"] = str(exc)
            results.append(result)
        return {"results": results}

    # ---- 跨口岸凭证修改：多版本与冲突 --------------------------------------

    def submit_certificate_revision(
        self, actor, certificate_no, port, changes, facility_id=None,
        base_revision_id=None, submitted_at=None, idempotency_key=None,
    ):
        if not certificate_no:
            raise ValidationError("certificate_no is required")
        if not port:
            raise ValidationError("port is required")
        if not isinstance(changes, dict) or not changes:
            raise ValidationError("changes must be a non-empty object")
        unknown = sorted(set(changes) - set(CERTIFICATE_EDITABLE_FIELDS))
        if unknown:
            raise ValidationError("field is not editable on a certificate: " + ",".join(unknown))
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                revision = self.repository.get_certificate_revision(existing)
                if revision:
                    return {"revision": revision, "conflict": False}
        if not submitted_at:
            submitted_at = utcnow()
        peers = self.repository.list_certificate_revisions(certificate_no)
        if base_revision_id is None and peers:
            base_revision_id = peers[-1]["id"]
        base = (
            self.repository.get_certificate_revision(base_revision_id)
            if base_revision_id is not None
            else None
        )
        if base_revision_id is not None and not base:
            raise NotFoundError("base revision not found: " + str(base_revision_id))
        if base and base["certificate_no"] != certificate_no:
            raise ValidationError("base revision belongs to another certificate")
        revision = self.repository.add_certificate_revision(
            certificate_no,
            port,
            facility_id,
            base_revision_id,
            changes,
            submitted_at,
            actor.user_id,
        )
        conflict = False
        if base:
            # 与基准版本自身、以及任何基于同一基准的分叉修改比较；
            # 口岸或种植点不同即为冲突
            divergent = [
                item
                for item in peers + [base]
                if item["id"] != revision["id"]
                and (item["id"] == base_revision_id or item["base_revision_id"] == base_revision_id)
                and (item["port"] != port or item["facility_id"] != facility_id)
            ]
            for other in divergent:
                self.repository.add_certificate_conflict(
                    certificate_no, revision["id"], other["id"]
                )
            conflict = bool(divergent)
        self.repository.append_audit(
            certificate_no,
            actor.user_id,
            actor.role,
            "certificate_revision",
            None,
            "revised",
            {"revision_id": revision["id"], "port": port, "conflict": conflict},
        )
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, revision["id"])
        return {"revision": revision, "conflict": conflict}

    def list_certificate_conflicts(self, certificate_no=None):
        return self.repository.list_certificate_conflicts(certificate_no)

    # ---- 旧数据凭证号回填 --------------------------------------------------

    def backfill_certificate_numbers(self, mappings):
        """升级时按批号回填凭证号；失败留下可重试记录。"""
        if not isinstance(mappings, dict) or not mappings:
            raise ValidationError("mappings must be a non-empty object of code -> certificate_no")
        updated, failed = [], []
        for code, certificate_no in mappings.items():
            consignments = self.repository.find_entities("consignment", "code", code)
            if not consignments:
                self.repository.record_backfill_failure(code, certificate_no, "batch not found")
                failed.append(code)
                continue
            if len(consignments) > 1:
                self.repository.record_backfill_failure(
                    code, certificate_no, "duplicate batch code"
                )
                failed.append(code)
                continue
            consignment = consignments[0]
            if not certificate_no:
                self.repository.record_backfill_failure(
                    code, certificate_no, "missing certificate number"
                )
                failed.append(code)
                continue
            if consignment["data"].get("certificate_no"):
                continue  # 已有凭证号，跳过保证幂等
            merged = dict(consignment["data"])
            merged["certificate_no"] = certificate_no
            self.repository.update_entity(
                consignment["id"], consignment["version"], consignment["status"], merged
            )
            self.repository.append_audit(
                consignment["id"],
                "upgrade-system",
                "admin",
                "backfill_certificate_no",
                consignment["status"],
                consignment["status"],
                {"certificate_no": certificate_no},
            )
            self.repository.resolve_backfill_failure(code)
            updated.append(code)
        return {"updated": updated, "failed": failed}

    def retry_backfill(self, code, certificate_no=None):
        failure = self.repository.get_backfill_failure(code)
        if not failure:
            raise NotFoundError("no backfill failure for code: " + str(code))
        result = self.backfill_certificate_numbers(
            {code: certificate_no if certificate_no is not None else failure["certificate_no"]}
        )
        return {
            "code": code,
            "resolved": code in result["updated"],
            "failure": self.repository.get_backfill_failure(code),
        }

    def list_backfill_failures(self, status=None):
        return self.repository.list_backfill_failures(status)

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
