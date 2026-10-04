from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ValidationError
from .repository import utcnow
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def _find_one(self, kind, field, value):
        rows = self._lookup(kind, field, value) or []
        return rows[0] if rows else None

    def _find_all(self, kind, field, value):
        return self._lookup(kind, field, value) or []

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
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if entity["kind"] == "credential" and action in ("return", "revoke", "lift"):
            return self._credential_hold_action(
                actor, entity["data"].get("credential_no"), action, (data or {}).get("reason")
            )
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
        return updated

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

    # --- official credential holds (return / revoke / lift) ---

    def return_credential(self, actor, credential_no, reason):
        return self._credential_hold_action(actor, credential_no, "return", reason)

    def revoke_credential(self, actor, credential_no, reason):
        return self._credential_hold_action(actor, credential_no, "revoke", reason)

    def lift_credential(self, actor, credential_no, reason):
        return self._credential_hold_action(actor, credential_no, "lift", reason)

    def _credential_hold_action(self, actor, credential_no, action, reason):
        if not credential_no:
            raise ValidationError("credential_no is required")
        cred = self._find_one("credential", "credential_no", credential_no)
        if not cred:
            raise NotFoundError("credential not found: " + credential_no)
        next_status, patch = self.rules.validate_transition(
            actor, cred, action, {"reason": reason}, self._lookup
        )
        data = dict(cred["data"])
        data.update(patch)
        updated = self.repository.update_entity(cred["id"], cred["version"], next_status, data)
        hold = next_status in ("returned", "revoked")
        self._propagate_hold(actor, credential_no, reason, next_status, hold)
        self.audit.record(
            cred["id"], actor, action, cred["status"], next_status,
            {"reason": reason, "hold": hold},
        )
        return updated

    def _propagate_hold(self, actor, credential_no, reason, official_action, hold):
        consignments = self._find_all("consignment", "credential_no", credential_no)
        touched_sites = set()
        for consignment in consignments:
            data = dict(consignment["data"])
            if hold:
                data["shipment_stopped"] = True
                data["official_hold"] = True
                data["hold_reason"] = reason
                data["official_action"] = official_action
            else:
                data["official_hold"] = False
                data["shipment_stopped"] = False
            self.repository.update_entity(
                consignment["id"], consignment["version"], consignment["status"], data
            )
            self.audit.record(
                consignment["id"], actor, "official_hold", consignment["status"], consignment["status"],
                {"credential_no": credential_no, "hold": hold, "reason": reason},
            )
            site = data.get("planting_site")
            if site:
                touched_sites.add(site)
        for site in touched_sites:
            self._set_facility_hold(actor, site, hold)

    def _set_facility_hold(self, actor, site, hold):
        facilities = self._find_all("facility", "name", site)
        for facility in facilities:
            if not hold:
                still_held = [
                    item for item in self._find_all("consignment", "planting_site", site)
                    if item["data"].get("official_hold")
                ]
                if still_held:
                    continue
            data = dict(facility["data"])
            data["shipment_stopped"] = hold
            self.repository.update_entity(facility["id"], facility["version"], facility["status"], data)
            self.audit.record(
                facility["id"], actor, "shipment_hold", facility["status"], facility["status"],
                {"planting_site": site, "hold": hold},
            )

    # --- offline registration reconciliation ---

    def reconcile_consignment(self, actor, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if entity["kind"] != "consignment":
            raise ValidationError("reconcile only applies to consignments")
        self.rules.check_role(actor, "consignment", "reconcile")
        data = dict(entity["data"])
        credential_no = data.get("credential_no")
        if not credential_no:
            data["reconciled"] = False
            data["reconcile_status"] = "pending"
            updated = self.repository.update_entity(entity_id, entity["version"], entity["status"], data)
            self.audit.record(entity_id, actor, "reconcile", entity["status"], entity["status"],
                              {"result": "pending", "reason": "no credential_no"})
            return updated
        credentials = self._find_all("credential", "credential_no", credential_no)
        if not credentials:
            data["reconciled"] = False
            data["reconcile_status"] = "pending"
            updated = self.repository.update_entity(entity_id, entity["version"], entity["status"], data)
            self.audit.record(entity_id, actor, "reconcile", entity["status"], entity["status"],
                              {"result": "pending", "reason": "credential not found"})
            return updated
        credential = credentials[0]
        if credential["status"] in ("returned", "revoked"):
            data["shipment_stopped"] = True
            data["official_hold"] = True
            data["hold_reason"] = "credential %s" % credential["status"]
            data["official_action"] = credential["status"]
            data["reconciled"] = True
            data["reconcile_status"] = "held"
            site = data.get("planting_site")
            if site:
                self._set_facility_hold(actor, site, True)
        elif credential["status"] == "valid":
            data["reconciled"] = True
            data["reconcile_status"] = "valid"
        updated = self.repository.update_entity(entity_id, entity["version"], entity["status"], data)
        self.audit.record(entity_id, actor, "reconcile", entity["status"], entity["status"],
                          {"result": data["reconcile_status"]})
        return updated

    # --- manual review (cannot overwrite an official interception) ---

    def correct_consignment(self, actor, entity_id, correction):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if entity["kind"] != "consignment":
            raise ValidationError("correct only applies to consignments")
        self.rules.check_role(actor, "consignment", "correct")
        note = (correction or "").strip() if isinstance(correction, str) else ""
        if not note:
            raise ValidationError("correction note is required")
        data = dict(entity["data"])
        corrections = list(data.get("corrections", []))
        corrections.append({"note": note, "by": actor.user_id, "at": utcnow()})
        data["corrections"] = corrections
        # official_hold / shipment_stopped are intentionally left unchanged:
        # manual review can only record a correction, not overwrite an official hold.
        updated = self.repository.update_entity(entity_id, entity["version"], entity["status"], data)
        self.audit.record(entity_id, actor, "correct", entity["status"], entity["status"],
                          {"correction": note})
        return updated

    # --- concurrent credential modification (keep two versions, list conflict) ---

    def modify_credential(self, actor, credential_no, data, port=None, submitted_at=None,
                          expected_version=None, planting_site=None):
        port = port or actor.user_id
        submitted_at = submitted_at or utcnow()
        payload = dict(data or {})
        payload["credential_no"] = credential_no
        if planting_site:
            payload["planting_site"] = planting_site
        existing = self._find_all("credential", "credential_no", credential_no)
        if not existing:
            entity = self.repository.create_entity(
                str(uuid4()), "credential", "valid", payload, actor.user_id
            )
            self.audit.record(entity["id"], actor, "credential_create", None, "valid",
                              {"credential_no": credential_no, "port": port})
            return {"created": True, "credential": entity}
        credential = existing[0]
        current_version = int(credential["version"])
        if expected_version is not None and int(expected_version) != current_version:
            version_a = {
                "data": dict(credential["data"]),
                "port": credential["data"].get("last_port"),
                "submitted_at": credential["data"].get("last_submitted_at"),
                "planting_site": credential["data"].get("planting_site"),
                "version": current_version,
            }
            version_b = {
                "data": payload,
                "port": port,
                "submitted_at": submitted_at,
                "planting_site": planting_site or payload.get("planting_site"),
                "version": None,
            }
            conflict = self.repository.create_conflict(
                "credential", credential["id"], credential_no, version_a, version_b
            )
            held = dict(credential["data"])
            held["has_conflict"] = True
            held["shipment_stopped"] = True
            updated = self.repository.update_entity(
                credential["id"], current_version, credential["status"], held
            )
            self.audit.record(credential["id"], actor, "credential_conflict",
                              credential["status"], credential["status"],
                              {"conflict_id": conflict["id"], "credential_no": credential_no})
            return {"conflict": True, "conflict_id": conflict["id"], "credential": updated}
        merged = dict(credential["data"])
        merged.update(payload)
        merged["last_port"] = port
        merged["last_submitted_at"] = submitted_at
        updated = self.repository.update_entity(
            credential["id"], current_version, credential["status"], merged
        )
        self.audit.record(credential["id"], actor, "credential_modify",
                          credential["status"], credential["status"],
                          {"credential_no": credential_no, "port": port})
        return {"created": False, "credential": updated}

    def list_conflicts(self, status=None):
        return self.repository.list_conflicts(status)

    def resolve_conflict(self, actor, conflict_id, pick):
        conflict = self.repository.get_conflict(conflict_id)
        if not conflict:
            raise NotFoundError("conflict not found: " + str(conflict_id))
        if conflict["status"] != "open":
            raise ValidationError("conflict already resolved: " + str(conflict_id))
        if pick not in ("a", "b"):
            raise ValidationError("pick must be 'a' or 'b'")
        chosen = conflict["version_" + pick]
        credential = self.repository.get_entity(conflict["entity_id"])
        if not credential:
            raise NotFoundError("credential not found: " + conflict["entity_id"])
        data = dict(credential["data"])
        data.update(chosen["data"])
        data["has_conflict"] = False
        data["shipment_stopped"] = False
        data["last_port"] = chosen.get("port")
        data["last_submitted_at"] = chosen.get("submitted_at")
        updated = self.repository.update_entity(
            credential["id"], credential["version"], credential["status"], data
        )
        resolution = {"picked": pick, "by": actor.user_id, "at": utcnow(), "port": chosen.get("port")}
        self.repository.resolve_conflict_record(conflict_id, resolution)
        self.audit.record(credential["id"], actor, "conflict_resolved",
                          credential["status"], credential["status"],
                          {"conflict_id": conflict_id, "picked": pick})
        return {"conflict_id": conflict_id, "picked": pick, "credential": updated}

    # --- credential number backfill (upgrade) with retryable records ---

    def upgrade_backfill(self, actor):
        consignments = self.repository.list_entities(kind="consignment")
        summary = {"processed": 0, "backfilled": 0, "pending": 0, "records": []}
        for consignment in consignments:
            if consignment["data"].get("credential_no"):
                continue
            summary["processed"] += 1
            batch_code = consignment["data"].get("code")
            credentials = self._find_all("credential", "batch_code", batch_code) if batch_code else []
            if credentials:
                data = dict(consignment["data"])
                data["credential_no"] = credentials[0]["data"].get("credential_no")
                self.repository.update_entity(
                    consignment["id"], consignment["version"], consignment["status"], data
                )
                summary["backfilled"] += 1
            else:
                record = self.repository.create_backfill_record(
                    "consignment", consignment["id"], batch_code, "credential_no", "pending",
                    "credential not found for batch code %s" % batch_code,
                )
                summary["pending"] += 1
                summary["records"].append(record)
        return summary

    def retry_backfill(self, actor, record_id=None):
        if record_id is not None:
            record = self.repository.get_backfill_record(record_id)
            if not record:
                raise NotFoundError("backfill record not found: " + str(record_id))
            records = [record]
        else:
            records = self.repository.list_backfill(status="pending")
        summary = {"processed": 0, "backfilled": 0, "pending": 0, "records": []}
        for record in records:
            if record["status"] == "done":
                continue
            summary["processed"] += 1
            consignment = self.repository.get_entity(record["target_id"])
            if not consignment:
                self.repository.update_backfill_record(
                    record["id"], "failed", "consignment not found", increment_attempts=True
                )
                summary["pending"] += 1
                continue
            batch_code = consignment["data"].get("code")
            credentials = self._find_all("credential", "batch_code", batch_code) if batch_code else []
            if credentials:
                data = dict(consignment["data"])
                data["credential_no"] = credentials[0]["data"].get("credential_no")
                self.repository.update_entity(
                    consignment["id"], consignment["version"], consignment["status"], data
                )
                self.repository.update_backfill_record(record["id"], "done", None)
                summary["backfilled"] += 1
            else:
                self.repository.update_backfill_record(
                    record["id"], "pending",
                    "credential not found for batch code %s" % batch_code,
                    increment_attempts=True,
                )
                summary["pending"] += 1
                summary["records"].append(self.repository.get_backfill_record(record["id"]))
        return summary

    def list_backfill(self, status=None):
        return self.repository.list_backfill(status)
