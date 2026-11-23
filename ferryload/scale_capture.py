"""Offline weighbridge ticket and calibration capture with shared-drift certificates.

Vehicles weighed on the same scale inside the same calibration period share one
unknown zero-drift bias ``b``.  Every ticket weight is therefore an affine
function ``reading_kg - b`` of the *same* variable, never an independent
interval.  This module resolves ticket revisions/voids, groups tickets by
``(scale_id, calibration_id)`` and issues recomputable weight/stability bound
certificates whose extremes are evaluated exactly over the shared variables
(the center of gravity is a linear-fractional function of the biases, so its
extrema over the bias box are attained at vertices).
"""
import json
from datetime import datetime
from itertools import product


def _instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def _canon(event):
    return json.dumps({k: v for k, v in event.items() if k != 'id'},
                      ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _require(event, fields, what):
    missing = [f for f in fields if event.get(f) is None]
    if missing:
        raise ValueError(f'{what} missing fields: {",".join(missing)}')


class ScaleBook:
    """Resolved calibration records and effective (deduplicated) tickets."""

    def __init__(self):
        self.calibrations = {}   # calibration_id -> calibration event (highest version)
        self.tickets = {}        # ticket_no -> effective ticket event (highest revision)
        self.retired = set()     # ticket_no voided or superseded by a replacement

    @classmethod
    def from_events(cls, events):
        book = cls()
        cal_seen = {}
        ticket_seen = {}
        for event in events:
            if event.get('kind') != 'scale':
                continue
            role = event.get('role')
            if role == 'calibration':
                _require(event, ['scale_id', 'calibration_id', 'valid_from', 'valid_to',
                                 'zero_bias_kg'], 'calibration record')
                bias = event['zero_bias_kg']
                if (not isinstance(bias, list) or len(bias) != 2
                        or not all(isinstance(x, (int, float)) for x in bias)
                        or bias[0] > bias[1]):
                    raise ValueError('calibration zero_bias_kg must be an ordered [lo,hi] pair')
                cid = event['calibration_id']
                version = event.get('version', 1)
                old = cal_seen.get(cid)
                if old is not None and old.get('version', 1) == version:
                    if _canon(old) != _canon(event):
                        raise ValueError(f'conflicting calibration version {cid} v{version}')
                    continue  # identical re-capture: keep the first event as evidence
                if old is None or version > old.get('version', 1):
                    cal_seen[cid] = event
            elif role == 'ticket':
                _require(event, ['ticket_no', 'revision'], 'scale ticket')
                no = event['ticket_no']
                revision = event['revision']
                if not event.get('void'):
                    _require(event, ['vin', 'scale_id', 'calibration_id', 'reading_kg',
                                     'weighed_at'], 'scale ticket')
                    if event['reading_kg'] <= 0:
                        raise ValueError(f'scale ticket {no} reading must be positive')
                old = ticket_seen.get((no, revision))
                if old is not None:
                    if _canon(old) != _canon(event):
                        raise ValueError(f'conflicting ticket revision {no} r{revision}')
                    continue  # identical re-capture: keep the first event as evidence
                ticket_seen[(no, revision)] = event
            else:
                raise ValueError(f'scale event {event.get("id")} has unknown role {role!r}')
        book.calibrations = cal_seen
        for (no, revision), event in ticket_seen.items():
            current = book.tickets.get(no)
            if current is None or revision > current['revision']:
                book.tickets[no] = event
        for no, event in book.tickets.items():
            if event.get('void'):
                book.retired.add(no)
        for event in ticket_seen.values():
            target = event.get('supersedes')
            if target:
                book.retired.add(target)
        return book

    def ticket(self, ticket_no):
        """Effective ticket for ``ticket_no`` or None when retired/unknown."""
        if ticket_no in self.retired:
            return None
        return self.tickets.get(ticket_no)

    def group_key(self, ticket):
        return (ticket['scale_id'], ticket['calibration_id'])

    def calibration_of(self, group_key):
        return self.calibrations.get(group_key[1])

    def bias_interval(self, group_key):
        cal = self.calibration_of(group_key)
        lo, hi = cal['zero_bias_kg']
        return lo, hi

    def ticket_valid(self, ticket):
        cal = self.calibrations.get(ticket['calibration_id'])
        if cal is None or cal['scale_id'] != ticket['scale_id']:
            return False
        lo, hi = cal['zero_bias_kg']
        weighed = _instant(ticket['weighed_at'])
        if not (_instant(cal['valid_from']) <= weighed < _instant(cal['valid_to'])):
            return False
        # the true weight reading-b must stay positive over the whole bias interval
        return ticket['reading_kg'] - hi > 0

    def vehicle_valid(self, vehicle):
        ticket = self.ticket(vehicle.get('weigh_ticket'))
        return ticket is not None and self.ticket_valid(ticket)


class WeightTerm:
    """One vehicle weight as an affine function of its group's shared bias."""
    __slots__ = ('kind', 'vin', 'const', 'group', 'reading', 'ticket_no', 'revision', 'event')

    def __init__(self, kind, vin, const=0.0, group=None, reading=0.0,
                 ticket_no=None, revision=None, event=None):
        self.kind = kind
        self.vin = vin
        self.const = const
        self.group = group
        self.reading = reading
        self.ticket_no = ticket_no
        self.revision = revision
        self.event = event

    @classmethod
    def point(cls, vin, kg):
        return cls('point', vin, const=float(kg))

    @classmethod
    def ticket(cls, vin, group, reading, ticket_no, revision, event):
        return cls('ticket', vin, group=group, reading=float(reading),
                   ticket_no=ticket_no, revision=revision, event=event)

    def value(self, bias):
        if self.kind == 'point':
            return self.const
        return self.reading - bias[self.group]


def group_name(group_key):
    return f'{group_key[0]}|{group_key[1]}'


def _affine_bounds(terms, book):
    """Exact [lo,hi] of a sum of terms plus the bias assignments attaining them."""
    const = 0.0
    coefs = {}
    for term in terms:
        if term.kind == 'point':
            const += term.const
        else:
            const += term.reading
            coefs[term.group] = coefs.get(term.group, 0.0) - 1.0
    lo = hi = const
    lo_assignment = {}
    hi_assignment = {}
    for group, coef in coefs.items():
        glo, ghi = book.bias_interval(group)
        if coef >= 0:
            lo += coef * glo
            hi += coef * ghi
            lo_assignment[group] = glo
            hi_assignment[group] = ghi
        else:
            lo += coef * ghi
            hi += coef * glo
            lo_assignment[group] = ghi
            hi_assignment[group] = glo
    return lo, hi, lo_assignment, hi_assignment


def _witness(assignment, terms):
    return {
        'bias': {group_name(g): v for g, v in sorted(assignment.items(), key=lambda kv: group_name(kv[0]))},
        'ticket_weights': {t.ticket_no: t.reading - assignment[t.group]
                           for t in sorted(terms, key=lambda z: z.ticket_no or '')
                           if t.kind == 'ticket'},
    }


def _center_bounds(weighted_terms, book):
    """Exact center-of-gravity extremes over the shared-bias box.

    CG(b) = (M0 + sum a_g b_g) / (W0 + sum w_g b_g) is linear-fractional, so
    with a positive denominator its extrema over the bias box are attained at
    vertices; enumerating vertices is exact, not a sampling approximation.
    Returns None when no assignment keeps the boarded weight positive.
    """
    groups = sorted({t.group for t, _ in weighted_terms if t.kind == 'ticket'})
    if not groups:
        weight = sum(t.value({}) for t, _ in weighted_terms)
        if weight <= 0:
            return None
        center = sum(t.value({}) * arm for t, arm in weighted_terms) / weight
        return center, center, {}, {}
    intervals = [book.bias_interval(g) for g in groups]
    lo = hi = None
    lo_assignment = hi_assignment = None
    for combo in product(*[(glo, ghi) for glo, ghi in intervals]):
        assignment = dict(zip(groups, combo))
        weight = sum(t.value(assignment) for t, _ in weighted_terms)
        if weight <= 0:
            continue
        moment = sum(t.value(assignment) * arm for t, arm in weighted_terms)
        center = moment / weight
        if lo is None or center < lo:
            lo, lo_assignment = center, assignment
        if hi is None or center > hi:
            hi, hi_assignment = center, assignment
    if lo is None:
        return None
    return lo, hi, lo_assignment, hi_assignment


def voyage_certificate(book, voyage, confirmed_terms, boarded_weighted, capacity):
    """Recomputable weight & stability bound certificate for one voyage.

    ``confirmed_terms`` are WeightTerms of confirmed vehicles (quota view);
    ``boarded_weighted`` are (WeightTerm, lever_arm_cm) pairs of boarded
    vehicles (stability view).  Bounds come with witnesses: the shared-bias
    assignment and the per-ticket weights attaining each extreme.
    """
    groups = {}
    for term in confirmed_terms:
        if term.kind == 'ticket':
            groups.setdefault(term.group, {})[term.ticket_no] = term
    for term, _ in boarded_weighted:
        if term.kind == 'ticket':
            groups.setdefault(term.group, {})[term.ticket_no] = term

    bias_groups = []
    for group in sorted(groups):
        cal = book.calibration_of(group)
        lo, hi = book.bias_interval(group)
        tickets = [groups[group][no] for no in sorted(groups[group])]
        bias_groups.append({
            'key': group_name(group),
            'scale_id': group[0],
            'calibration_id': group[1],
            'bias_kg': [lo, hi],
            'calibration_event': cal['id'],
            'calibration_version': cal.get('version', 1),
            'tickets': [{'ticket_no': t.ticket_no, 'revision': t.revision, 'vin': t.vin,
                         'reading_kg': t.reading, 'event': t.event} for t in tickets],
        })

    w_lo, w_hi, w_lo_a, w_hi_a = _affine_bounds(confirmed_terms, book)
    b_lo, b_hi, b_lo_a, b_hi_a = _affine_bounds([t for t, _ in boarded_weighted], book)
    boarded_terms = [t for t, _ in boarded_weighted]
    center = _center_bounds(boarded_weighted, book)

    smin = voyage.get('stability_min_cm', -30)
    smax = voyage.get('stability_max_cm', 30)
    if center is None:
        stability = 'empty'
        c_lo = c_hi = c_lo_a = c_hi_a = None
    else:
        c_lo, c_hi, c_lo_a, c_hi_a = center
        if c_lo >= smin and c_hi <= smax:
            stability = 'within'
        elif c_hi < smin or c_lo > smax:
            stability = 'outside'
        else:
            stability = 'straddling'

    if w_hi <= capacity:
        quota = 'within'
    elif w_lo > capacity:
        quota = 'exceeded'
    else:
        quota = 'straddling'

    def bound_section(lo, hi, lo_a, hi_a, terms):
        return {
            'lower': lo,
            'upper': hi,
            'lower_witness': _witness(lo_a, terms),
            'upper_witness': _witness(hi_a, terms),
        }

    certificate = {
        'voyage_id': voyage['voyage_id'],
        'generated_from': {
            'calibrations': {g['calibration_id']: {'event': g['calibration_event'],
                                                   'version': g['calibration_version']}
                             for g in bias_groups},
            'tickets': {t['ticket_no']: {'event': t['event'], 'revision': t['revision'],
                                        'reading_kg': t['reading_kg'], 'bias_group': g['key']}
                        for g in bias_groups for t in g['tickets']},
        },
        'bias_groups': bias_groups,
        'confirmed_weight_kg': bound_section(w_lo, w_hi, w_lo_a, w_hi_a, confirmed_terms),
        'boarded_weight_kg': bound_section(b_lo, b_hi, b_lo_a, b_hi_a, boarded_terms),
        'center_of_gravity_cm': {
            'lower': c_lo,
            'upper': c_hi,
            'lower_witness': _witness(c_lo_a, boarded_terms) if c_lo_a is not None else None,
            'upper_witness': _witness(c_hi_a, boarded_terms) if c_hi_a is not None else None,
            'limits': [smin, smax],
            'status': stability,
        },
        'quota': {'capacity_kg': capacity, 'status': quota},
    }
    return certificate
