"""Stow plan occupancy and physical boarding evidence."""
from collections import defaultdict


def valid_plan(plans, reservations, voyage, confirmed_rows, sds_violations=None):
    """方案资格：舱道唯一、尺寸重量可容纳、计划数与已确认车辆数相符，
    且不得命中任何已确认的 SDS 禁配超边（sds_capture 计算结果）。"""
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
            and bool(plans) and len(plans) == len(confirmed_rows)
            and not (sds_violations or []))


def scanned(plans):
    return all(plan.get('boarded') and plan.get('unloaded') for plan in plans)

