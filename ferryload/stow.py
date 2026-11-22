"""Stow plan occupancy and physical boarding evidence."""
from collections import defaultdict


def valid_plan(plans, reservations, voyage, confirmed_rows):
    occupancy = defaultdict(list)
    for plan in plans:
        occupancy[plan['lane']].append(plan)
    return (all(len(rows) == 1
                and rows[0]['vin'] in reservations
                and rows[0]['lane'] in voyage.get('lanes', {})
                and reservations[rows[0]['vin']].get('measured_kg', 0) <= voyage['lanes'][rows[0]['lane']]['weight_kg']
                and reservations[rows[0]['vin']].get('length_cm', 0) <= voyage['lanes'][rows[0]['lane']]['length_cm']
                and reservations[rows[0]['vin']].get('height_cm', 0) <= voyage['lanes'][rows[0]['lane']]['height_cm']
                for rows in occupancy.values())
            and bool(plans) and len(plans) == len(confirmed_rows))


def scanned(plans):
    return all(plan.get('boarded') and plan.get('unloaded') for plan in plans)

