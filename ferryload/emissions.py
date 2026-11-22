from . import fuel

def co2_kg(voyage):
    liters = fuel.consumed_liters(voyage)
    if liters is None:
        return None
    meter = voyage['fuel_meter']
    return liters * meter['density_kg_per_liter'] * meter['co2_kg_per_fuel_kg']
