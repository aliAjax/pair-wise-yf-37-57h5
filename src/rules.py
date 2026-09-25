from collections import defaultdict, deque
from datetime import date, datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


EXCLUDED_STATUS = "excluded"


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()


def _parse_day(value):
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except (TypeError, ValueError):
        raise ValidationError("invalid date: " + str(value))


def _field(item, name):
    """Read a field from either a flat dict or an entity dict (data payload)."""
    if not isinstance(item, dict):
        return None
    if name in item:
        return item.get(name)
    return (item.get("data") or {}).get(name)


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
    """Group cases at the same location whose onset dates connect within max_days.

    A case joins a group when its onset is within the window of any member, so
    chains (A-B-C where only adjacent onsets are within range) stay together.
    Singleton groups are dropped: a lone case is not an outbreak cluster.
    """
    candidates = [
        case
        for case in cases
        if _field(case, "location") is not None and _field(case, "onset_date")
    ]
    ordered = sorted(candidates, key=lambda item: (str(_field(item, "onset_date")), str(_field(item, "id"))))
    groups = []
    used = set()
    for index, case in enumerate(ordered):
        if id(case) in used:
            continue
        location = _field(case, "location")
        members = [case]
        used.add(id(case))
        frontier = 0
        while frontier < len(members):
            anchor = members[frontier]
            anchor_day = _parse_day(_field(anchor, "onset_date"))
            frontier += 1
            for other in ordered:
                if id(other) in used or _field(other, "location") != location:
                    continue
                other_day = _parse_day(_field(other, "onset_date"))
                if abs((anchor_day - other_day).days) <= max_days:
                    members.append(other)
                    used.add(id(other))
        members.sort(key=lambda item: (str(_field(item, "onset_date")), str(_field(item, "id"))))
        onset_dates = [str(_field(member, "onset_date")) for member in members]
        groups.append(
            {
                "location": location,
                "onset_date": onset_dates[0],
                "first_onset_date": onset_dates[0],
                "last_onset_date": onset_dates[-1],
                "members": [_field(member, "id") for member in members],
            }
        )
    groups.sort(key=lambda group: (group["location"], group["onset_date"]))
    return [group for group in groups if len(group["members"]) > 1]


CUSTOM_CREATE = {'case': _validate_case}
CUSTOM_TRANSITIONS = {('case', 'lab_positive'): _validate_lab_positive, ('case', 'mark_probable'): _validate_probable}


class RuleEngine:
    ALIASES = {'cases': 'case', 'contacts': 'contact'}
    INITIAL_STATUS = {'case': 'reported', 'contact': 'identified'}
    TRANSITIONS = {'case': {'triage': (('reported',), 'investigating'), 'lab_positive': (('investigating',), 'confirmed'), 'mark_probable': (('investigating',), 'probable'), 'recover': (('confirmed', 'probable'), 'recovered'), 'close': (('recovered',), 'closed'), 'rule_out': (('reported', 'investigating', 'probable'), 'excluded')}, 'contact': {'begin_followup': (('identified',), 'following'), 'complete_followup': (('following',), 'completed')}}
    CREATE_REQUIRED = {'case': ('person_id', 'onset_date', 'location', 'symptoms'), 'contact': ('case_id', 'person_id', 'exposure_start')}
    ACTION_REQUIRED = {('case', 'triage'): ('clinician',), ('case', 'lab_positive'): ('lab_id', 'result'), ('case', 'mark_probable'): ('epi_link',), ('case', 'recover'): ('recovered_at',), ('case', 'close'): ('outcome',), ('case', 'rule_out'): ('reason',), ('contact', 'begin_followup'): ('followup_start', 'due_at'), ('contact', 'complete_followup'): ('outcome',)}
    CREATE_ROLES = {'case': ('admin', 'clinician'), 'contact': ('admin', 'investigator')}
    ROLE_ACTIONS = {'triage': ('admin', 'clinician'), 'lab_positive': ('admin', 'lab'), 'mark_probable': ('admin', 'investigator'), 'recover': ('admin', 'clinician'), 'close': ('admin', 'investigator'), 'rule_out': ('admin', 'clinician', 'investigator'), 'begin_followup': ('admin', 'investigator'), 'complete_followup': ('admin', 'investigator')}

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


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()


# ---------------------------------------------------------------------------
# 调查态势（派生只读视图：聚集分组 / 跨组风险路径 / 随访待办）
# ---------------------------------------------------------------------------


def _case_nodes(cases):
    """Return (active_cases, excluded_cases) taken from entity-like dicts."""
    active, excluded = [], []
    for case in cases:
        if case.get("kind") and case["kind"] != "case":
            continue
        if case.get("status") == EXCLUDED_STATUS:
            excluded.append(case)
        else:
            active.append(case)
    return active, excluded


def _build_contact_edges(contacts, cases):
    """Undirected record-level edges along case/contact relationships.

    - A contact links to its ``case_id`` (and ``linked_case_id`` /
      ``related_case_id``) and to any case whose ``person_id`` matches (the
      contact person later became a case).
    - Two contact records sharing the same ``person_id`` link together (one
      person traced against cases in different groups).
    - Two case records sharing the same ``person_id`` link together.

    Excluded cases keep every link because their contact relationships remain.
    """
    cases_by_person = defaultdict(list)
    for case in cases:
        person_id = _field(case, "person_id")
        if person_id is not None:
            cases_by_person[str(person_id)].append(str(case["id"]))
    contacts_by_person = defaultdict(list)
    for contact in contacts:
        person_id = _field(contact, "person_id")
        if person_id is not None:
            contacts_by_person[str(person_id)].append(contact["id"])

    edges = defaultdict(set)

    def link(left, right):
        if left == right:
            return
        edges[left].add(right)
        edges[right].add(left)

    for person_id, case_ids in cases_by_person.items():
        for index, left in enumerate(case_ids):
            for right in case_ids[index + 1:]:
                link(("case", left), ("case", right))
    for person_id, contact_ids in contacts_by_person.items():
        for index, left in enumerate(contact_ids):
            for right in contact_ids[index + 1:]:
                link(("contact", left), ("contact", right))
        for case_id in cases_by_person.get(person_id, ()):
            for contact_id in contact_ids:
                link(("contact", contact_id), ("case", str(case_id)))
    for contact in contacts:
        source = ("contact", contact["id"])
        for name in ("case_id", "linked_case_id", "related_case_id"):
            value = _field(contact, name)
            if value:
                link(source, ("case", str(value)))
    return edges


def _shortest_connection(edges, sources, targets, blocked):
    """Multi-source BFS returning one shortest node path sources -> targets."""
    parents = {node: None for node in sources}
    queue = deque(sorted(sources, key=lambda item: item[1]))
    hit = None
    while queue and hit is None:
        node = queue.popleft()
        neighbors = sorted(edges.get(node, ()), key=lambda item: (item[0], item[1]))
        for neighbor in neighbors:
            kind, node_id = neighbor
            if neighbor in parents:
                continue
            if neighbor in targets:
                parents[neighbor] = node
                hit = neighbor
                break
            # Intermediate hops stay on contacts, excluded cases or ungrouped
            # (lone) active cases; members of other clusters are not traversed.
            if kind == "case" and (node_id in blocked):
                continue
            parents[neighbor] = node
            queue.append(neighbor)
    if hit is None:
        return None
    path = []
    current = hit
    while current is not None:
        path.append(current)
        current = parents[current]
    path.reverse()
    return path


def _node_desc(node, case_by_id, contact_by_id):
    kind, node_id = node
    entity = (case_by_id if kind == "case" else contact_by_id).get(node_id)
    status = entity.get("status") if entity else None
    person_id = _field(entity, "person_id") if entity else None
    desc = {"kind": kind, "id": node_id}
    if person_id is not None:
        desc["person_id"] = person_id
    if status is not None:
        desc["status"] = status
    return desc


def find_risk_paths(cases, contacts):
    """Paths along case/contact relationships connecting two cluster groups."""
    active, excluded = _case_nodes(cases)
    groups = cluster_cases(active)
    for index, group in enumerate(groups, start=1):
        group["cluster_id"] = "C-%d" % index
    case_by_id = {case["id"]: case for case in cases if case.get("kind", "case") == "case"}
    contact_by_id = {contact["id"]: contact for contact in contacts}
    edges = _build_contact_edges(contacts, cases)

    clustered_cases = {member for group in groups for member in group["members"]}
    paths = []
    for index, source_group in enumerate(groups):
        source_nodes = [("case", member) for member in source_group["members"]]
        for target_group in groups[index + 1:]:
            target_ids = set(target_group["members"])
            target_nodes = [("case", member) for member in target_group["members"]]
            blocked = clustered_cases - set(source_group["members"]) - target_ids
            path = _shortest_connection(edges, source_nodes, target_nodes, blocked)
            if path:
                paths.append(
                    {
                        "from_cluster": source_group["cluster_id"],
                        "to_cluster": target_group["cluster_id"],
                        "length": len(path) - 1,
                        "nodes": [
                            _node_desc(node, case_by_id, contact_by_id) for node in path
                        ],
                    }
                )
    return groups, paths


def overdue_followups(contacts, as_of=None):
    """Contacts whose follow-up is due at/before as_of but not completed."""
    reference = as_of if isinstance(as_of, date) else _parse_day(as_of or date.today().isoformat())
    overdue = []
    for contact in contacts:
        if contact.get("kind") and contact["kind"] != "contact":
            continue
        if contact.get("status") == "completed":
            continue
        due = _field(contact, "due_at")
        if not due:
            continue
        if _parse_day(due) <= reference:
            overdue.append(
                {
                    "id": contact["id"],
                    "person_id": _field(contact, "person_id"),
                    "case_id": _field(contact, "case_id"),
                    "status": contact.get("status"),
                    "due_at": str(due),
                    "days_overdue": (reference - _parse_day(due)).days,
                }
            )
    overdue.sort(key=lambda item: (item["due_at"], item["id"]))
    return overdue


def build_situation(cases, contacts, as_of=None):
    """Assemble the investigation situation read model from current records."""
    active, excluded = _case_nodes(cases)
    groups, paths = find_risk_paths(cases, contacts)
    for group in groups:
        group["risk_count"] = len(group["members"])
    overdue = overdue_followups(contacts, as_of=as_of)
    excluded_view = [
        {
            "id": case["id"],
            "person_id": _field(case, "person_id"),
            "location": _field(case, "location"),
            "onset_date": _field(case, "onset_date"),
            "status": case.get("status"),
            "reason": _field(case, "reason"),
        }
        for case in sorted(excluded, key=lambda item: item["id"])
    ]
    return {
        "as_of": (as_of.isoformat() if isinstance(as_of, date) else str(as_of or date.today().isoformat())),
        "cluster_count": len(groups),
        "risk_case_count": sum(group["risk_count"] for group in groups),
        "clusters": groups,
        "risk_paths": paths,
        "excluded_cases": excluded_view,
        "overdue_followups": overdue,
        "overdue_count": len(overdue),
    }
