from datetime import date, datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


OBSERVATION_DAYS = 14
RESOLVED_CASE_STATUSES = ("recovered", "closed")
KEEP_STATUS = "="


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()


def _day(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        raise ValidationError("invalid date: %s" % value)


def linked_case_ids(data):
    """All cases associated with a contact, de-duplicated in link order."""
    raw = list(data.get("case_ids") or [])
    if data.get("case_id"):
        raw.insert(0, data["case_id"])
    ids = []
    for case_id in raw:
        if case_id and case_id not in ids:
            ids.append(case_id)
    return ids


def contact_links(data):
    links = data.get("links")
    if links:
        return links
    fallback_end = data.get("exposure_end") or data.get("exposure_start")
    return [
        {
            "case_id": case_id,
            "exposure_start": data.get("exposure_start"),
            "exposure_end": fallback_end,
        }
        for case_id in linked_case_ids(data)
    ]


def _symptom_list(value):
    if isinstance(value, str):
        value = [value]
    items = [str(item).strip() for item in (value or []) if str(item).strip()]
    if not items:
        raise ValidationError("symptoms must not be empty")
    return items


def _validate_case(actor, data, lookup):
    rows = lookup("case", "person_id", data.get("person_id")) or [] if lookup else []
    for row in rows:
        if row["data"].get("onset_date") == data.get("onset_date"):
            raise ConflictError("duplicate case for person and onset date")
    if not data.get("symptoms"):
        raise ValidationError("symptoms are required")


def _validate_lab_positive(actor, entity, data, lookup):
    if data.get("result", "").lower() not in ("positive", "detected"):
        raise ValidationError("lab result must be positive or detected")
    return {"confirmed_by": actor.user_id}


def _validate_probable(actor, entity, data, lookup):
    if not data.get("epi_link"):
        raise ValidationError("probable case requires an epidemiological link")


def _require_cases(case_ids, lookup):
    for case_id in case_ids:
        rows = lookup("case", "id", case_id) if lookup else None
        if not rows:
            raise ValidationError("linked case does not exist: " + str(case_id))


def _exposure_window(data, default_start=None, default_end=None):
    start_value = data.get("exposure_start") or default_start
    end_value = data.get("exposure_end") or default_end or start_value
    start = _day(start_value)
    end = _day(end_value)
    if end < start:
        raise ValidationError("exposure_end must not be before exposure_start")
    return start, end


def _validate_contact_create(actor, data, lookup):
    case_ids = linked_case_ids(data)
    if not case_ids:
        raise ValidationError("contact requires at least one linked case")
    start, end = _exposure_window(data)
    _require_cases(case_ids, lookup)
    links = [
        {
            "case_id": case_id,
            "exposure_start": start.isoformat(),
            "exposure_end": end.isoformat(),
        }
        for case_id in case_ids
    ]
    return {
        "case_id": case_ids[0],
        "case_ids": case_ids,
        "exposure_start": start.isoformat(),
        "exposure_end": end.isoformat(),
        "links": links,
    }


def _validate_link_case(actor, entity, data, lookup):
    case_id = data["case_id"]
    rows = lookup("case", "id", case_id) if lookup else None
    if not rows:
        raise ValidationError("linked case does not exist: " + str(case_id))
    current = entity["data"]
    case_ids = linked_case_ids(current)
    if case_id in case_ids:
        raise ConflictError("case already linked to contact: " + str(case_id))
    link_start, link_end = _exposure_window(
        data,
        default_start=current.get("exposure_start"),
        default_end=current.get("exposure_end") or current.get("exposure_start"),
    )
    overall_start = min(_day(current.get("exposure_start")), link_start)
    overall_end = max(
        _day(current.get("exposure_end") or current.get("exposure_start")), link_end
    )
    links = contact_links(current)
    links.append(
        {
            "case_id": case_id,
            "exposure_start": link_start.isoformat(),
            "exposure_end": link_end.isoformat(),
        }
    )
    case_ids.append(case_id)
    return {
        "case_id": case_ids[0],
        "case_ids": case_ids,
        "exposure_start": overall_start.isoformat(),
        "exposure_end": overall_end.isoformat(),
        "links": links,
    }


def _validate_report_symptoms(actor, entity, data, lookup):
    symptoms = _symptom_list(data.get("symptoms"))
    onset = _day(data.get("symptom_onset") or date.today()).isoformat()
    reports = list(entity["data"].get("symptom_reports") or [])
    reports.append(
        {"symptoms": symptoms, "onset": onset, "reported_by": actor.user_id}
    )
    all_symptoms = sorted(
        set(list(entity["data"].get("symptoms") or []) + symptoms)
    )
    earliest = min(_day(report["onset"]) for report in reports)
    patch = {
        "symptoms": all_symptoms,
        "symptomatic": True,
        "symptom_onset": earliest.isoformat(),
        "symptom_reports": reports,
    }
    if entity["status"] == "identified":
        start = (data.get("followup_start") or onset)
        due = data.get("due_at") or (
            _day(start) + timedelta(days=OBSERVATION_DAYS)
        ).isoformat()
        patch["followup_start"] = str(start)[:10]
        patch["due_at"] = str(due)[:10]
    if entity["status"] == "completed":
        revoked = list(entity["data"].get("release_revoked") or [])
        revoked.append(
            {
                "at": datetime.now().isoformat(timespec="seconds"),
                "reason": "symptoms_reported",
                "symptom_onset": onset,
            }
        )
        patch["release_revoked"] = revoked
    return patch


def evaluate_contact_release(contact, cases, as_of=None):
    """Decide whether a contact may be released.

    Release requires all of:
      * 14 full days since the last exposure ended;
      * every linked case is recovered or closed;
      * the contact has not reported symptoms.

    ``cases`` is a mapping (or list) of case entities keyed by id.
    """
    data = contact.get("data", {})
    if as_of is None:
        as_of = date.today()
    elif not isinstance(as_of, date):
        as_of = _day(as_of)

    case_ids = linked_case_ids(data)
    if isinstance(cases, (list, tuple)):
        cases = {case["id"]: case for case in cases if case}

    links = contact_links(data)
    exposure_ends = [
        _day(link.get("exposure_end") or link.get("exposure_start")) for link in links
    ]
    if not exposure_ends:
        exposure_ends = [
            _day(data.get("exposure_end") or data.get("exposure_start"))
        ]
    last_exposure_end = max(exposure_ends)
    window_ends_at = last_exposure_end + timedelta(days=OBSERVATION_DAYS)

    blockers = []
    if as_of < window_ends_at:
        blockers.append(
            {
                "code": "observation_window",
                "message": "最近一次暴露结束于 %s，至 %s 未满 %d 天观察期"
                % (last_exposure_end.isoformat(), as_of.isoformat(), OBSERVATION_DAYS),
                "earliest_release_date": window_ends_at.isoformat(),
            }
        )

    pending_cases = []
    for case_id in case_ids:
        case = cases.get(case_id) if cases else None
        if case is None:
            blockers.append(
                {
                    "code": "case_missing",
                    "case_id": case_id,
                    "message": "关联病例 %s 不存在，无法确认转归" % case_id,
                }
            )
        elif case["status"] not in RESOLVED_CASE_STATUSES:
            pending_cases.append({"case_id": case_id, "status": case["status"]})
            blockers.append(
                {
                    "code": "case_open",
                    "case_id": case_id,
                    "case_status": case["status"],
                    "message": "关联病例 %s 状态为 %s，尚未康复或关闭"
                    % (case_id, case["status"]),
                }
            )

    if data.get("symptomatic") or data.get("symptoms"):
        blockers.append(
            {
                "code": "symptomatic",
                "message": "接触者本人已于 %s 报告症状，需转病例排查，不能解除观察"
                % data.get("symptom_onset", as_of.isoformat()),
            }
        )

    hard_blocked = any(
        blocker["code"]
        in ("case_open", "case_missing", "symptomatic")
        for blocker in blockers
    )
    # A pending case or symptoms have no determinable resolution date; the
    # observation window alone gives a concrete earliest date.
    earliest = None if hard_blocked else window_ends_at.isoformat()

    return {
        "eligible": not blockers,
        "as_of": as_of.isoformat(),
        "observation_days": OBSERVATION_DAYS,
        "linked_cases": case_ids,
        "last_exposure_end": last_exposure_end.isoformat(),
        "window_ends_at": window_ends_at.isoformat(),
        "pending_cases": pending_cases,
        "blockers": blockers,
        "earliest_release_date": earliest,
    }


def _validate_complete_followup(actor, entity, data, lookup):
    case_ids = linked_case_ids(entity["data"])
    cases = {}
    for case_id in case_ids:
        rows = lookup("case", "id", case_id) if lookup else None
        cases[case_id] = rows[0] if rows else None
    as_of = _day(data["as_of"]) if data.get("as_of") else date.today()
    result = evaluate_contact_release(entity, cases, as_of)
    if not result["eligible"]:
        detail = "；".join(blocker["message"] for blocker in result["blockers"])
        earliest = result["earliest_release_date"]
        if earliest:
            detail += "；最早可解除时间：%s" % earliest
        else:
            detail += "；最早可解除时间待阻塞因素消除后确定"
        raise InvalidTransition("cannot release contact: " + detail)
    return {"released_at": as_of.isoformat()}


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


CUSTOM_CREATE = {'case': _validate_case, 'contact': _validate_contact_create}
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
    TRANSITIONS = {'case': {'triage': (('reported',), 'investigating'), 'lab_positive': (('investigating',), 'confirmed'), 'mark_probable': (('investigating',), 'probable'), 'recover': (('confirmed', 'probable'), 'recovered'), 'close': (('recovered',), 'closed'), 'reopen': (('recovered', 'closed'), 'investigating')}, 'contact': {'begin_followup': (('identified',), 'following'), 'report_symptoms': (('identified', 'following', 'completed'), 'following'), 'link_case': (('identified', 'following', 'completed'), KEEP_STATUS), 'complete_followup': (('following',), 'completed')}}
    CREATE_REQUIRED = {'case': ('person_id', 'onset_date', 'location', 'symptoms'), 'contact': ('person_id', 'exposure_start')}
    ACTION_REQUIRED = {('case', 'triage'): ('clinician',), ('case', 'lab_positive'): ('lab_id', 'result'), ('case', 'mark_probable'): ('epi_link',), ('case', 'recover'): ('recovered_at',), ('case', 'close'): ('outcome',), ('case', 'reopen'): ('reason',), ('contact', 'begin_followup'): ('followup_start', 'due_at'), ('contact', 'link_case'): ('case_id',), ('contact', 'report_symptoms'): ('symptoms',), ('contact', 'complete_followup'): ('outcome',)}
    CREATE_ROLES = {'case': ('admin', 'clinician'), 'contact': ('admin', 'investigator')}
    ROLE_ACTIONS = {'triage': ('admin', 'clinician'), 'lab_positive': ('admin', 'lab'), 'mark_probable': ('admin', 'investigator'), 'recover': ('admin', 'clinician'), 'close': ('admin', 'investigator'), 'reopen': ('admin', 'clinician'), 'begin_followup': ('admin', 'investigator'), 'link_case': ('admin', 'investigator'), 'report_symptoms': ('admin', 'investigator'), 'complete_followup': ('admin', 'investigator')}

    def __init__(self, today=None):
        self.today = today or date.today

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
        extra = custom(actor, data, lookup) if custom else {}
        payload = dict(data)
        if extra:
            payload.update(extra)
        return payload

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
        data.pop("as_of", None)
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None
