from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)

# 官方退回/吊销后的批次状态；处于这些状态的批次和所属种植点停止调运
HELD_STATUSES = ("held", "correction_pending")
# 官方对账结论 -> 批次状态
OFFICIAL_HELD_RESULTS = {"returned": "held", "revoked": "held"}
# 同一凭证上口岸可修改的字段；其余字段以凭证库为准
CERTIFICATE_EDITABLE_FIELDS = (
    "inspector",
    "site_conclusion",
    "facility_id",
    "remarks",
)


def _facility_status(lookup, facility_id):
    if not facility_id or lookup is None:
        return None
    rows = lookup("facility", "id", facility_id) or []
    return rows[0]["status"] if rows else None


def _validate_consignment(actor, data, lookup):
    if data.get("origin") == data.get("destination"):
        raise ValidationError("origin and destination must differ")
    # 批次登记必须记种植点；停调运中的种植点不得再承接批次
    if data.get("offline") and not data.get("facility_id"):
        raise ValidationError("offline registration requires facility_id")
    facility_status = _facility_status(lookup, data.get("facility_id"))
    if facility_status == "shipping_suspended":
        raise InvalidTransition("facility shipping is suspended")


def _validate_inspect(actor, entity, data, lookup):
    return {"inspector": data.get("inspector", entity["data"].get("inspector"))}


def _validate_quarantine(actor, entity, data, lookup):
    if not data.get("pest_found"):
        raise ValidationError("pest_found must be true for quarantine")
    return {"quarantined_by": actor.user_id}


def _validate_release(actor, entity, data, lookup):
    # 官方拦截不能被人工放行盖掉
    if entity["status"] in HELD_STATUSES:
        raise InvalidTransition(
            "official interception cannot be released; submit for review instead"
        )
    facility_status = _facility_status(lookup, entity["data"].get("facility_id"))
    if facility_status == "shipping_suspended":
        raise InvalidTransition("facility shipping is suspended")
    if data.get("pest_found"):
        raise ValidationError("pest-positive consignment cannot be released")
    if data.get("treatment") not in ("none", "completed", "certified"):
        raise ValidationError("release requires a valid treatment state")
    return {"released_by": actor.user_id}


def _validate_reconcile(actor, entity, data, lookup):
    """与官方凭证库对账：matched 恢复正常流转；returned/revoked 停止调运。"""
    result = data.get("official_result")
    if result not in ("matched", "returned", "revoked"):
        raise ValidationError(
            "official_result must be one of matched, returned, revoked"
        )
    certificate_no = data.get("certificate_no") or entity["data"].get("certificate_no")
    if not certificate_no:
        raise ValidationError("certificate_no is required for reconciliation")
    patch = {
        "certificate_no": certificate_no,
        "official_result": result,
        "reconciled_by": actor.user_id,
    }
    if result == "matched":
        patch["reconciled"] = True
    return patch


def _validate_review(actor, entity, data, lookup):
    """人工复核只能留待修正：把被拦截批次转入 correction_pending，不能解除拦截。"""
    if entity["status"] != "held":
        raise InvalidTransition("only a held consignment can be submitted for review")
    if not data.get("review_note"):
        raise ValidationError("review_note is required")
    return {"review_note": data["review_note"], "reviewed_by": actor.user_id}


def trace_downstream(consignments, start_id):
    pending = [start_id]
    visited = set()
    result = []
    while pending:
        current = pending.pop(0)
        if current in visited:
            continue
        visited.add(current)
        result.append(current)
        for item in consignments:
            if item.get("parent_id") == current:
                pending.append(item.get("id"))
    return result


CUSTOM_CREATE = {'consignment': _validate_consignment}
CUSTOM_TRANSITIONS = {
    ('consignment', 'inspect'): _validate_inspect,
    ('consignment', 'quarantine'): _validate_quarantine,
    ('consignment', 'release'): _validate_release,
    ('consignment', 'reconcile'): _validate_reconcile,
    ('consignment', 'review'): _validate_review,
}


class RuleEngine:
    ALIASES = {'consignments': 'consignment', 'facilities': 'facility'}
    INITIAL_STATUS = {
        'consignment': lambda data: 'offline_registered' if data.get('offline') else 'declared',
        'facility': 'registered',
    }
    TRANSITIONS = {
        'consignment': {
            'inspect': (('declared', 'reconciled'), 'inspected'),
            'quarantine': (('inspected',), 'quarantined'),
            'release': (('inspected',), 'released'),
            'destroy': (('quarantined',), 'destroyed'),
            'recheck': (('quarantined',), 'inspected'),
            # 离线批次与官方凭证库对账：matched -> reconciled；退回/吊销 -> held
            'reconcile': (
                ('offline_registered', 'declared', 'reconciled', 'held', 'correction_pending'),
                lambda data: 'held' if data.get('official_result') in OFFICIAL_HELD_RESULTS else 'reconciled',
            ),
            # 官方拦截后的人工复核只能留待修正
            'review': (('held',), 'correction_pending'),
        },
        'facility': {
            'trace': (('registered',), 'traced'),
            'suspend_shipping': (('registered', 'traced'), 'shipping_suspended'),
            'resume_shipping': (('shipping_suspended',), 'registered'),
        },
    }
    CREATE_REQUIRED = {
        'consignment': ('code', 'origin', 'destination'),
        'facility': ('name', 'address'),
    }
    ACTION_REQUIRED = {
        ('consignment', 'inspect'): ('inspector', 'inspection_result'),
        ('consignment', 'quarantine'): ('pest_found', 'sample_id'),
        ('consignment', 'release'): ('pest_found', 'treatment'),
        ('consignment', 'destroy'): ('method', 'witnessed_by'),
        ('consignment', 'recheck'): ('sample_id',),
        ('consignment', 'reconcile'): ('official_result',),
        ('consignment', 'review'): ('review_note',),
        ('facility', 'trace'): ('consignment_ids',),
        ('facility', 'suspend_shipping'): ('reason',),
    }
    CREATE_ROLES = {'consignment': ('admin', 'inspector'), 'facility': ('admin', 'quarantine')}
    ROLE_ACTIONS = {
        'inspect': ('admin', 'inspector'),
        'quarantine': ('admin', 'quarantine'),
        'release': ('admin', 'quarantine'),
        'destroy': ('admin', 'quarantine'),
        'recheck': ('admin', 'inspector'),
        'trace': ('admin', 'quarantine'),
        # 对账与停调运以官方凭证库为准
        'reconcile': ('admin', 'official'),
        'review': ('admin', 'inspector', 'quarantine'),
        'suspend_shipping': ('admin', 'official', 'quarantine'),
        'resume_shipping': ('admin', 'official'),
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind, data=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        status = self.INITIAL_STATUS[kind]
        return status(data or {}) if callable(status) else status

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        if callable(next_status):
            next_status = next_status(patch)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
