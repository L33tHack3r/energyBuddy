# Data dictionary

Intended schema; raw exports can violate these expectations. Empty fields and N/A denote unavailable measurements. No participant row is labeled as a planted issue.

## flats.csv — 8 rows

| Column | Meaning |
|---|---|
| flat_id | Fictional stable identifier |
| residents | Number of residents |
| floor_area_m2 | Floor area in square metres |
| price_area | Synthetic price area, SE3 for all flats |
| main_heating | Main heat is district heat, outside electricity scope |
| tariff_id | Retained tariff reference; no tariff lookup is included |

## hourly_context.csv — 5,376 rows

| Column | Meaning |
|---|---|
| flat_id | Flat identifier |
| timestamp_utc | Interval start UTC |
| timestamp_local | Local start with UTC offset |
| outdoor_temp_c | Outdoor air temperature Celsius |
| indoor_temp_c | Indoor temperature Celsius |
| occupants_home | Estimated residents home, integer 0 to resident count |

## hourly_prices.csv — 672 rows

| Column | Meaning |
|---|---|
| timestamp_utc | Interval start UTC |
| price_area | Join via flats.price_area |
| spot_sek_per_kwh | Synthetic spot SEK/kWh excluding VAT |
| carbon_gco2e_per_kwh | Synthetic operational emissions gCO2e/kWh |

## energy_readings.csv — 37,715 rows

| Column | Meaning |
|---|---|
| reading_id | Source record identifier; may repeat |
| flat_id | Reported flat identifier |
| appliance_id | Reported appliance/circuit identifier; no type, power or shiftability lookup is included |
| timestamp_utc | Interval start, canonically ISO UTC |
| energy_value | Interval energy, nominal numeric; raw defects retained |
| energy_unit | kWh or Wh; normalize before summing |

