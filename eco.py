"""Carbon footprint calculator.

Provides calculate_carbon_footprint(miles_driven, kwh_used) -> kg CO2.
Emission factors: 0.404 kg CO2 per mile driven, 0.92 kg CO2 per kWh used.
"""


def calculate_carbon_footprint(miles_driven: float, kwh_used: float) -> float:
    """Return total carbon footprint in kg CO2.

    Args:
        miles_driven: Miles driven.
        kwh_used: Electricity used in kWh.

    Returns:
        Total kg CO2 (sum of driving + electricity emissions).
    """
    driving_emissions = miles_driven * 0.404
    electricity_emissions = kwh_used * 0.92
    return driving_emissions + electricity_emissions


if __name__ == "__main__":
    # Quick smoke tests
    result = calculate_carbon_footprint(0, 0)
    print(f"Zero usage: {result:.2f} kg CO2 (expected 0.00)")

    result = calculate_carbon_footprint(100, 50)
    print(f"100 miles + 50 kWh: {result:.2f} kg CO2 (expected {100*0.404 + 50*0.92:.2f})")

    result = calculate_carbon_footprint(250, 200)
    print(f"250 miles + 200 kWh: {result:.2f} kg CO2 (expected {250*0.404 + 200*0.92:.2f})")
