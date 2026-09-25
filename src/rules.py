from collections import deque
from datetime import datetime, timedelta, timezone
from itertools import combinations

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()


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


EXCLUDED_CASE_STATUS = "excluded"


def _parse_date(value):
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except (TypeError, ValueError):
        return None


def _resolve_today(today):
    if today is None:
        return datetime.now(timezone.utc).date()
    if isinstance(today, datetime):
        return today.date()
    return today


def find_overdue_followups(contacts, today=None):
    today = _resolve_today(today)
    overdue = []
    for contact in contacts:
        data = contact.get("data", {})
        if contact.get("status") != "following":
            continue
        due = _parse_date(data.get("due_at"))
        if due is None or due >= today:
            continue
        overdue.append({
            "contact_id": contact.get("id"),
            "person_id": data.get("person_id"),
            "case_id": data.get("case_id"),
            "due_at": data.get("due_at"),
            "days_overdue": (today - due).days,
        })
    return sorted(overdue, key=lambda item: (str(item["due_at"]), str(item["contact_id"])))


def _build_contact_graph(cases, contacts):
    adjacency = {}

    def add_edge(first, second):
        adjacency.setdefault(first, set()).add(second)
        adjacency.setdefault(second, set()).add(first)

    case_ids_by_person = {}
    for case in cases:
        person = case.get("data", {}).get("person_id")
        if person:
            case_ids_by_person.setdefault(person, []).append(case.get("id"))
    for contact in contacts:
        data = contact.get("data", {})
        contact_node = ("contact", contact.get("id"))
        if data.get("case_id"):
            add_edge(contact_node, ("case", data["case_id"]))
        for linked_case_id in case_ids_by_person.get(data.get("person_id"), []):
            add_edge(contact_node, ("case", linked_case_id))
    return adjacency


def _shortest_path(adjacency, sources, targets):
    targets = set(targets)
    parents = {source: None for source in sources}
    queue = deque(sources)
    while queue:
        node = queue.popleft()
        if node in targets:
            path = []
            while node is not None:
                path.append(node)
                node = parents[node]
            path.reverse()
            return path
        for neighbor in sorted(adjacency.get(node, ())):
            if neighbor not in parents:
                parents[neighbor] = node
                queue.append(neighbor)
    return None


def _describe_node(node, cases_by_id, contacts_by_id):
    kind, entity_id = node
    entity = (cases_by_id if kind == "case" else contacts_by_id).get(entity_id)
    data = entity.get("data", {}) if entity else {}
    description = {"kind": kind, "id": entity_id, "person_id": data.get("person_id")}
    if kind == "case":
        description["status"] = entity.get("status") if entity else None
        description["location"] = data.get("location")
        description["onset_date"] = data.get("onset_date")
    else:
        description["case_id"] = data.get("case_id")
    return description


def assess_situation(cases, contacts, today=None, max_days=14):
    today = _resolve_today(today)
    cases_by_id = {case["id"]: case for case in cases}
    contacts_by_id = {contact["id"]: contact for contact in contacts}
    active_cases = [case for case in cases if case.get("status") != EXCLUDED_CASE_STATUS]
    cluster_input = [
        {"id": case["id"], "location": case["data"].get("location"), "onset_date": case["data"].get("onset_date")}
        for case in active_cases
        if case["data"].get("location") and _parse_date(case["data"].get("onset_date"))
    ]
    contacts_by_case = {}
    for contact in contacts:
        contacts_by_case.setdefault(contact.get("data", {}).get("case_id"), []).append(contact)

    clusters = []
    risk_people = set()
    for index, group in enumerate(cluster_cases(cluster_input, max_days=max_days), start=1):
        member_ids = list(group["members"])
        onsets = sorted(str(cases_by_id[case_id]["data"].get("onset_date")) for case_id in member_ids)
        linked = [contact for case_id in member_ids for contact in contacts_by_case.get(case_id, [])]
        persons = {cases_by_id[case_id]["data"].get("person_id") for case_id in member_ids}
        persons.update(contact.get("data", {}).get("person_id") for contact in linked)
        persons.discard(None)
        persons.discard("")
        risk_people.update(persons)
        clusters.append({
            "id": "cluster-%d" % index,
            "location": group["location"],
            "onset_start": onsets[0],
            "onset_end": onsets[-1],
            "case_ids": member_ids,
            "contact_ids": [contact["id"] for contact in linked],
            "risk_persons": len(persons),
        })

    adjacency = _build_contact_graph(cases, contacts)
    risk_paths = []
    for first, second in combinations(clusters, 2):
        path = _shortest_path(
            adjacency,
            [("case", case_id) for case_id in first["case_ids"]],
            [("case", case_id) for case_id in second["case_ids"]],
        )
        if path:
            risk_paths.append({
                "from_cluster": first["id"],
                "to_cluster": second["id"],
                "path": [_describe_node(node, cases_by_id, contacts_by_id) for node in path],
            })

    overdue = find_overdue_followups(contacts, today=today)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "as_of": today.isoformat(),
        "clusters": clusters,
        "risk_paths": risk_paths,
        "overdue_followups": overdue,
        "summary": {
            "cluster_count": len(clusters),
            "risk_path_count": len(risk_paths),
            "overdue_count": len(overdue),
            "risk_person_count": len(risk_people),
        },
    }


CUSTOM_CREATE = {'case': _validate_case}
CUSTOM_TRANSITIONS = {('case', 'lab_positive'): _validate_lab_positive, ('case', 'mark_probable'): _validate_probable}


class RuleEngine:
    ALIASES = {'cases': 'case', 'contacts': 'contact'}
    INITIAL_STATUS = {'case': 'reported', 'contact': 'identified'}
    TRANSITIONS = {'case': {'triage': (('reported',), 'investigating'), 'lab_positive': (('investigating',), 'confirmed'), 'mark_probable': (('investigating',), 'probable'), 'exclude': (('reported', 'investigating'), 'excluded'), 'recover': (('confirmed', 'probable'), 'recovered'), 'close': (('recovered',), 'closed')}, 'contact': {'begin_followup': (('identified',), 'following'), 'complete_followup': (('following',), 'completed')}}
    CREATE_REQUIRED = {'case': ('person_id', 'onset_date', 'location', 'symptoms'), 'contact': ('case_id', 'person_id', 'exposure_start')}
    ACTION_REQUIRED = {('case', 'triage'): ('clinician',), ('case', 'lab_positive'): ('lab_id', 'result'), ('case', 'mark_probable'): ('epi_link',), ('case', 'exclude'): ('reason',), ('case', 'recover'): ('recovered_at',), ('case', 'close'): ('outcome',), ('contact', 'begin_followup'): ('followup_start', 'due_at'), ('contact', 'complete_followup'): ('outcome',)}
    CREATE_ROLES = {'case': ('admin', 'clinician'), 'contact': ('admin', 'investigator')}
    ROLE_ACTIONS = {'triage': ('admin', 'clinician'), 'lab_positive': ('admin', 'lab'), 'mark_probable': ('admin', 'investigator'), 'exclude': ('admin', 'clinician'), 'recover': ('admin', 'clinician'), 'close': ('admin', 'investigator'), 'begin_followup': ('admin', 'investigator'), 'complete_followup': ('admin', 'investigator')}

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

    def assess_situation(self, cases, contacts, today=None):
        return assess_situation(cases, contacts, today=today)


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
