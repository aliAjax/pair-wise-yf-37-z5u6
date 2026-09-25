from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)

RELEASE_WINDOW_DAYS = 14
CASE_RESOLVED_STATUSES = ("recovered", "closed")


def _to_date(value):
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        raise ValidationError("invalid date: " + str(value))


def _date_ordinal(value):
    return _to_date(value).toordinal()


def _today():
    return datetime.now(timezone.utc).date()


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def contact_case_ids(data):
    ids = list(data.get("case_ids") or [])
    single = data.get("case_id")
    if single and single not in ids:
        ids.append(single)
    return ids


def _validate_case(actor, data, lookup):
    rows = lookup("case", "person_id", data.get("person_id")) or [] if lookup else []
    for row in rows:
        if row["data"].get("onset_date") == data.get("onset_date"):
            raise ConflictError("duplicate case for person and onset date")
    if not data.get("symptoms"):
        raise ValidationError("symptoms are required")


def _validate_contact(actor, data, lookup):
    raw = data.get("case_ids")
    if raw is not None and not isinstance(raw, list):
        raise ValidationError("case_ids must be a list")
    case_ids = contact_case_ids(data)
    if not case_ids:
        raise ValidationError("missing required field: case_id")
    for cid in case_ids:
        if lookup is not None and _find_one(lookup, "case", "id", cid) is None:
            raise ValidationError("linked case not found: " + str(cid))
    start = _to_date(data.get("exposure_start"))
    end = _to_date(data.get("exposure_end")) or start
    if start is not None and end is not None and end < start:
        raise ValidationError("exposure_end must not be before exposure_start")
    data["case_ids"] = case_ids
    data["case_id"] = case_ids[0]
    data["exposure_end"] = end.isoformat() if end else None


def _validate_lab_positive(actor, entity, data, lookup):
    if data.get("result", "").lower() not in ("positive", "detected"):
        raise ValidationError("lab result must be positive or detected")
    return {"confirmed_by": actor.user_id}


def _validate_probable(actor, entity, data, lookup):
    if not data.get("epi_link"):
        raise ValidationError("probable case requires an epidemiological link")


def _validate_link_case(actor, entity, data, lookup):
    case_id = data.get("case_id")
    if lookup is not None and _find_one(lookup, "case", "id", case_id) is None:
        raise ValidationError("linked case not found: " + str(case_id))
    case_ids = contact_case_ids(entity["data"])
    if case_id not in case_ids:
        case_ids.append(case_id)
    extra = {"case_ids": case_ids, "case_id": case_ids[0]}
    if data.get("exposure_end"):
        end = _to_date(data["exposure_end"])
        start = _to_date(entity["data"].get("exposure_start"))
        if start is not None and end < start:
            raise ValidationError("exposure_end must not be before exposure_start")
        extra["exposure_end"] = end.isoformat()
    return extra


def _validate_report_symptoms(actor, entity, data, lookup):
    if not isinstance(data.get("symptoms"), list):
        raise ValidationError("symptoms must be a list")
    if data.get("symptom_onset"):
        data["symptom_onset"] = _to_date(data["symptom_onset"]).isoformat()
    return {"symptoms_reported_by": actor.user_id}


def _validate_complete_followup(actor, entity, data, lookup):
    as_of = data.pop("as_of", None)
    cases = []
    for cid in contact_case_ids(entity["data"]):
        row = _find_one(lookup, "case", "id", cid)
        if row is not None:
            cases.append(row)
    result = evaluate_release(entity, cases, as_of=as_of)
    if not result["releasable"]:
        raise ValidationError("cannot release contact: " + "; ".join(result["reasons"]))
    return {
        "released_by": actor.user_id,
        "released_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "release_check": result,
    }


def cluster_cases(cases, max_days=14):
    groups = []
    for case in sorted(cases, key=lambda item: str(item.get("onset_date", ""))):
        placed = False
        for group in groups:
            same_location = group["location"] == case.get("location")
            delta = abs(_date_ordinal(group["onset_date"]) - _date_ordinal(case.get("onset_date")))
            if same_location and delta <= max_days:
                group["members"].append(case.get("id"))
                placed = True
                break
        if not placed:
            groups.append({"location": case.get("location"), "onset_date": case.get("onset_date"), "members": [case.get("id")]})
    return [group for group in groups if len(group["members"]) > 1]


def evaluate_release(contact, linked_cases, as_of=None):
    data = contact.get("data", {})
    as_of_date = _to_date(as_of) if as_of else _today()
    reasons = []
    earliest = None
    exposure_end = _to_date(data.get("exposure_end") or data.get("exposure_start"))
    if exposure_end is None:
        reasons.append("缺少暴露时间，无法判定观察期")
    else:
        earliest = exposure_end + timedelta(days=RELEASE_WINDOW_DAYS)
        if as_of_date < earliest:
            reasons.append("最近一次暴露结束未满14天观察期")
    symptoms = data.get("symptoms") or []
    if symptoms:
        reasons.append("本人已出现症状: " + ", ".join(str(item) for item in symptoms))
        onset = _to_date(data.get("symptom_onset"))
        if onset is not None:
            symptom_end = onset + timedelta(days=RELEASE_WINDOW_DAYS)
            if earliest is None or symptom_end > earliest:
                earliest = symptom_end
    found = set()
    pending = []
    for case in linked_cases:
        found.add(case["id"])
        if case["status"] not in CASE_RESOLVED_STATUSES:
            pending.append(case["id"])
    if pending:
        reasons.append("关联病例仍未康复或关闭: " + ", ".join(str(cid) for cid in pending))
    missing = [cid for cid in contact_case_ids(data) if cid not in found]
    if missing:
        reasons.append("关联病例记录缺失: " + ", ".join(str(cid) for cid in missing))
    return {
        "releasable": not reasons,
        "reasons": reasons,
        "earliest_release_at": earliest.isoformat() if earliest else None,
        "as_of": as_of_date.isoformat(),
        "linked_cases": [{"id": case["id"], "status": case["status"]} for case in linked_cases],
    }


CUSTOM_CREATE = {'case': _validate_case, 'contact': _validate_contact}
CUSTOM_TRANSITIONS = {
    ('case', 'lab_positive'): _validate_lab_positive,
    ('case', 'mark_probable'): _validate_probable,
    ('contact', 'link_case'): _validate_link_case,
    ('contact', 'report_symptoms'): _validate_report_symptoms,
    ('contact', 'complete_followup'): _validate_complete_followup,
}


class RuleEngine:
    ALIASES = {'cases': 'case', 'contacts': 'contact'}
    INITIAL_STATUS = {'case': 'reported', 'contact': 'identified'}
    TRANSITIONS = {'case': {'triage': (('reported',), 'investigating'), 'lab_positive': (('investigating',), 'confirmed'), 'mark_probable': (('investigating',), 'probable'), 'recover': (('confirmed', 'probable'), 'recovered'), 'close': (('recovered',), 'closed')}, 'contact': {'begin_followup': (('identified',), 'following'), 'complete_followup': (('following',), 'completed'), 'link_case': (('identified', 'following'), None), 'report_symptoms': (('identified', 'following'), None)}}
    CREATE_REQUIRED = {'case': ('person_id', 'onset_date', 'location', 'symptoms'), 'contact': ('person_id', 'exposure_start')}
    ACTION_REQUIRED = {('case', 'triage'): ('clinician',), ('case', 'lab_positive'): ('lab_id', 'result'), ('case', 'mark_probable'): ('epi_link',), ('case', 'recover'): ('recovered_at',), ('case', 'close'): ('outcome',), ('contact', 'begin_followup'): ('followup_start', 'due_at'), ('contact', 'complete_followup'): ('outcome',), ('contact', 'link_case'): ('case_id',), ('contact', 'report_symptoms'): ('symptoms',)}
    CREATE_ROLES = {'case': ('admin', 'clinician'), 'contact': ('admin', 'investigator')}
    ROLE_ACTIONS = {'triage': ('admin', 'clinician'), 'lab_positive': ('admin', 'lab'), 'mark_probable': ('admin', 'investigator'), 'recover': ('admin', 'clinician'), 'close': ('admin', 'investigator'), 'begin_followup': ('admin', 'investigator'), 'complete_followup': ('admin', 'investigator'), 'link_case': ('admin', 'investigator'), 'report_symptoms': ('admin', 'investigator', 'clinician')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

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
        if next_status is None:
            next_status = entity["status"]
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
        return next_status, patch
