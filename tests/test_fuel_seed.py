from ferryload import fuel, emissions, rules
from ferryload.store import connect, import_file, report, audit
from pathlib import Path

def test_voyage_fuel_to_carbon_business_flow():
    voyage={
        'kind':'voyage','id':'FUEL-1','voyage_id':'FUEL-1',
        'departure_at':'2026-06-01T10:00:00Z','tide_windows':[
            {'start':'2026-06-01T08:00:00Z','end':'2026-06-01T12:00:00Z','level_cm':220}],
        'required_level_cm':180,'lanes':{},'fuel_meter':{
            'start_liters':900,'end_liters':1000,
            'density_kg_per_liter':0.84,'co2_kg_per_fuel_kg':3.16}
    }
    assert fuel.consumed_liters(voyage)==100
    assert abs(emissions.co2_kg(voyage)-265.44)<0.001
    decisions,_=rules.calculate([voyage])
    assert decisions['航次碳排归集']['result']=='265.44千克'

def test_fuel_fixture_import_and_audit():
    db=connect(':memory:')
    fixture=Path(__file__).resolve().parents[1]/'fixtures'/'voyage-fuel-ok.json'
    import_file(db,'voyage',fixture)
    assert report(db)['decisions']['航次碳排归集']['result']=='265.44千克'
    assert audit(db)['ok']
    db.close()
