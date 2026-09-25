from datetime import datetime, timezone
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import KEEP_STATUS, RuleEngine, evaluate_contact_release, linked_case_ids


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
                    return self._decorate(entity)
        payload = self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return self._decorate(entity)

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
        if next_status == KEEP_STATUS:
            next_status = entity["status"]
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        if updated["kind"] == "case":
            self._reevaluate_contacts_after_case(actor, updated, action)
            updated = self.repository.get_entity(entity_id)
        elif action == "link_case" and updated["status"] == "completed":
            downgraded = self._reevaluate_contact(actor, updated, trigger_action=action)
            if downgraded:
                updated = downgraded
        return self._decorate(updated)

    def _reevaluate_contacts_after_case(self, actor, case_entity, case_action):
        for contact in self.repository.list_entities(kind="contact"):
            if case_entity["id"] not in linked_case_ids(contact["data"]):
                continue
            if contact["status"] != "completed":
                continue
            self._reevaluate_contact(
                actor, contact, trigger_action=case_action, trigger_case_id=case_entity["id"]
            )

    def _reevaluate_contact(self, actor, contact, trigger_action, trigger_case_id=None):
        """Downgrade a released contact back to active follow-up when the
        release prerequisites no longer hold."""
        cases = {}
        for case_id in linked_case_ids(contact["data"]):
            rows = self.repository.find_entities("case", "id", case_id)
            cases[case_id] = rows[0] if rows else None
        result = evaluate_contact_release(contact, cases, as_of=self.rules.today())
        if result["eligible"]:
            return
        merged = dict(contact["data"])
        history = list(merged.get("release_history") or [])
        if merged.get("released_at"):
            history.append(
                {
                    "released_at": merged.get("released_at"),
                    "outcome": merged.get("outcome"),
                }
            )
        revoked = list(merged.get("release_revoked") or [])
        revoked.append(
            {
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "reason": "case_outcome_changed"
                if trigger_case_id
                else "release_prerequisite_lost",
                "trigger_case_id": trigger_case_id,
                "trigger_action": trigger_action,
                "blockers": result["blockers"],
            }
        )
        merged["released_at"] = None
        merged["outcome"] = None
        merged["release_history"] = history
        merged["release_revoked"] = revoked
        updated = self.repository.update_entity(contact["id"], contact["version"], "following", merged)
        self.audit.record(
            contact["id"],
            actor,
            "resume_followup",
            "completed",
            "following",
            {
                "trigger": trigger_action,
                "trigger_case_id": trigger_case_id,
                "blockers": result["blockers"],
                "earliest_release_date": result["earliest_release_date"],
            },
        )
        return updated

    def _release_evaluation(self, contact):
        cases = {}
        for case_id in linked_case_ids(contact["data"]):
            rows = self.repository.find_entities("case", "id", case_id)
            cases[case_id] = rows[0] if rows else None
        return evaluate_contact_release(contact, cases, as_of=self.rules.today())

    @staticmethod
    def _release_summary(entity, evaluation):
        return {
            "eligible": evaluation["eligible"],
            "earliest_release_date": evaluation["earliest_release_date"],
            "window_ends_at": evaluation["window_ends_at"],
            "last_exposure_end": evaluation["last_exposure_end"],
            "linked_cases": evaluation["linked_cases"],
            "pending_cases": evaluation["pending_cases"],
            "blockers": evaluation["blockers"],
        }

    def _decorate(self, entity):
        if entity is None or entity.get("kind") != "contact":
            return entity
        decorated = dict(entity)
        evaluation = self._release_evaluation(entity)
        decorated["data"] = dict(entity["data"])
        decorated["release"] = self._release_summary(entity, evaluation)
        return decorated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return self._decorate(entity)

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        entities = self.repository.list_entities(kind=kind, status=status)
        return [self._decorate(entity) for entity in entities]

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
