import json
from pathlib import Path
from ferryload import duty, crew

def test_duty_to_crew_business_flow():
    data = json.loads((Path(__file__).resolve().parents[1]/'fixtures'/'crew-duty-ok.json').read_text(encoding='utf-8'))
    assert duty.worked_minutes(data['entries']) == 420
    assert crew.eligible(data['entries']) == {'worked_minutes': 420, 'result': '可排班'}
