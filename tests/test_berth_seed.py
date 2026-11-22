import json
from pathlib import Path
from ferryload import berth, turnaround

def test_berth_to_turnaround_business_flow():
    data = json.loads((Path(__file__).resolve().parents[1]/'fixtures'/'berth-slot-ok.json').read_text(encoding='utf-8'))
    assert berth.free(data['request'], data['reservations'])
    assert turnaround.assign(data['request'], data['reservations']) == {'slot': 'F1-A', 'result': '可用'}
