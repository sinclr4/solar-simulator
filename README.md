# Solar + Battery Simulator

A local web app that compares solar and battery installation quotes against your Octopus half-hourly electricity usage and York solar data. Results include estimated bill savings, payback dates, seasonal average-day charts, and monthly breakdowns.

## What the simulator does

The simulator replays your real electricity usage, half-hour by half-hour, as if each quoted solar and battery system had been installed. It then compares the modelled bill with what you actually would have paid on the same tariff.

For every quote, it runs three operating strategies:

| Strategy | Battery charged from | Surplus solar goes to |
| --- | --- | --- |
| Solar + battery (charged from solar only) | Solar only | Battery, then export |
| Solar + battery + off-peak grid charging | Solar, plus cheap off-peak grid electricity | Battery, then export |
| Full off-peak charge, sell surplus at export rate | Solar, plus a full off-peak grid charge | Battery, then export. From 21:30, any battery energy beyond what the rest of that evening needs is also sold at the export rate, just before off-peak starts |

The last option only pays when the export rate, after the battery's round-trip loss, beats the off-peak rate. With 6.6p off-peak, 12p export and 89% efficiency, each kWh sold earns about 10.7p for 6.6p of electricity. That is a gain of roughly 4p, before counting extra battery wear from cycling daily.

Quotes differ in solar size, battery, export limit and cost. The roof, tariff and finance settings are shared, so quotes are compared on equal terms.

## How the results are calculated

### 1. Usage

Half-hourly import readings come from the Octopus API. Readings from replaced meters on the same MPAN are included. Where meters overlap, the current meter's reading is used. Each half-hour is labelled off-peak (23:30–05:30 UK time) or peak.

### 2. Solar generation

Hourly plane-of-array irradiance for York comes from the Open-Meteo archive for the chosen roof pitch. The archive provides it for east, south and west faces.

- **East / West:** irradiance is a weighted mix of the east and west faces, using the east-share setting (default 60% east).
- **North / South:** all panels face south.

Each half-hour's generation is:

```text
generation (kWh) = irradiance (W/m²) ÷ 1000 × performance ratio × array size (kWp) × 0.5 h
```

Both half-hours in an hour use that hour's average irradiance. If **Typical year** is selected, the whole solar profile is scaled so its annual yield matches PVGIS's long-term average for the same location, orientation, pitch and losses. This corrects for an unusually sunny or dull year, but keeps the half-hourly shape of the recorded weather.

### 3. Half-hourly energy flows

Every half-hour is processed in this order:

1. **Direct use:** solar supplies the house first. The car's charging is excluded: it always comes from the grid, never from solar or the battery.
2. **Battery charging from solar:** surplus solar charges the battery. This is limited by battery power (kW), free capacity and charging losses. Round-trip efficiency is split equally between charging and discharging (√efficiency each way).
3. **Export:** remaining surplus is exported up to the export limit. Anything above the limit is counted as **curtailed**.
4. **Battery discharge:** during peak hours only, the battery covers any remaining demand, limited by its power and stored energy. It never discharges in the off-peak window, because grid electricity is cheap then. It never exports to the grid.
5. **Grid import:** any demand still unmet, plus all the car's charging, is imported.
6. **Off-peak grid charging** (grid-charging strategies only): during 23:30–05:30, the battery is topped up from the grid. It charges only to the level needed for the following day: the peak-time shortfall after solar, less surplus solar that could refill the battery, capped at battery capacity.

The simulator tracks how much stored energy came from solar. This means **daytime usage shifted to solar** counts only solar used directly or via the battery, and excludes grid-charged energy.

**EV charging:** the car's charging in each half-hour comes from Octopus's modelled EV readings. A half-hour with EV charging outside 23:30–05:30 is treated as an Intelligent Octopus smart-charge slot and billed at the off-peak rate, and the battery does not discharge in it.

### 4. Bills and savings

```text
bill before = Σ usage × rate for that half-hour
bill after  = Σ grid import × rate for that half-hour − Σ export × export rate
saving      = bill before − bill after
```

Grid import includes off-peak battery charging, which is charged at the off-peak rate.

### 5. Annual figures

The usage period may not be exactly one year, or may miss some months. Annual totals are therefore built as a typical year:

1. Total each day.
2. Average the daily totals for each calendar month.
3. Interpolate any month with no data from neighbouring months, wrapping round the year.
4. Multiply each monthly daily average by the number of days in that month, and add the 12 months together.

The same method is used for usage, generation, export, costs and savings, so a short or uneven history is not biased towards one season.

### 6. Payback

The annual saving is projected over 25 years. Each year's saving is adjusted for energy price changes and panel degradation:

```text
saving in year n = annual saving × (1 + price rise)^(n−1) × (1 − degradation)^(n−1)
```

Cumulative savings start at minus the quote cost.

- **Break-even:** the point where cumulative savings reach zero, interpolated within the year. It is shown for flat prices and for the selected price rise.
- **In profit from:** the install date (or today) plus the break-even time.
- **25-year net gain:** cumulative savings after 25 years.
- **Return on cost:** 25-year net gain divided by the quote cost.

If the cost is not recovered within 25 years, the result shows "> 25 yrs".

### Three-day forecast

The "Yesterday, today & tomorrow" section fetches the Open-Meteo solar forecast for your roof and runs each quote's solar and battery over those three days with the same half-hourly model. Usage is the stored actual reading for a day when it is complete. Otherwise it is your average for that weekday over the last 8 weeks (or all recent days if there are fewer than three of that weekday), and the chart labels it "estimated". Octopus data usually lags by a day or more, so today and tomorrow are always estimated. Yesterday's solar is modelled data rather than a true forecast. The battery starts empty at the beginning of yesterday.

### Limitations

Results are estimates, not guarantees.

- **Perfect forecast:** grid charging plans against the following day's recorded usage and solar, giving the model a forecast no real battery has. Results for the grid-charging strategies are therefore optimistic.
- **Weather data:** solar is based on area-average weather data for York. It does not model shading, panel temperature, inverter clipping or your exact roof.
- **Fixed tariff:** one fixed peak, off-peak and export rate is applied throughout the period, regardless of historic prices.
- **Flexible load:** EV charging is inferred from overnight usage patterns, so other flexible overnight loads are treated the same way.
- **Costs:** battery degradation, maintenance and replacement are not included.

## Requirements

- Python 3.10 or later
- Azure CLI, signed in to an account with data access to the configured Cosmos DB database
- An Octopus API key and account/meter details

## Set up

From the project directory, create and activate a virtual environment, then install dependencies:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create your local environment file from the example:

```sh
cp .env.example .env
```

Edit `.env` and set `COSMOS_ENDPOINT` and `COSMOS_DATABASE` for your Cosmos DB account and database. Do not commit `.env`; it is gitignored. Octopus details are entered in the web page and saved encrypted (see "Accounts and privacy"). When running locally you are the user `local`, and a generated encryption key is kept in `~/.solarsim/credentials.key`. For the command line you can still set `OCTOPUS_API_KEY`, `OCTOPUS_ACCOUNT` and `OCTOPUS_MPAN` in `.env`.

Sign in to Azure CLI using the identity granted Cosmos DB data access:

```sh
az login
```

If necessary, select the subscription containing the account:

```sh
az account set --subscription "<subscription name or ID>"
```

## Run the web app

```sh
.venv/bin/python -m solarsim web
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). The app binds to localhost by default. To use a different port:

```sh
PORT=8001 .venv/bin/python -m solarsim web
```

On the page, first connect your Octopus account (account number and API key, once), then use **Fetch usage data** to retrieve more Octopus history, adjust the shared roof/tariff assumptions and quote-specific equipment/costs, then press **Calculate**. The first fetch can take a little while. The app checks the MPAN's import meters so it can include historic readings from replaced meters.

## Command line

Fetch the last 365 days of consumption into Cosmos DB (or choose another period):

```sh
.venv/bin/python -m solarsim fetch-usage
.venv/bin/python -m solarsim fetch-usage --days 730
```

Run the simulator from the stored data:

```sh
.venv/bin/python -m solarsim simulate
.venv/bin/python -m solarsim simulate --days 365 --orientation east_west --typical-year
```

The web UI supports multiple quotes. The CLI reports the first quote using its default equipment values. Run `.venv/bin/python -m solarsim --help` and `.venv/bin/python -m solarsim simulate --help` to see available options.

## Data sources and storage

- Octopus Energy API: half-hourly meter consumption
- Open-Meteo archive: hourly tilted solar irradiance for York
- PVGIS: optional long-term typical-year solar adjustment
- Azure Cosmos DB for NoSQL: consumption, solar data, and saved simulation results

## Azure deployment

Pushing to `main` deploys the app to Azure App Service at [https://solar-simulator.azurewebsites.net](https://solar-simulator.azurewebsites.net) through [.github/workflows/deploy.yml](.github/workflows/deploy.yml). GitHub signs in to Azure with OpenID Connect, so no Azure credentials are stored in GitHub. The workflow reads these non-secret repository variables: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, and `AZURE_WEBAPP_NAME`.

The hosted app uses free-tier resources: an App Service F1 Linux plan and Azure Cosmos DB free tier with 1000 RU/s of shared database throughput. It connects to Cosmos DB with its managed identity, and Cosmos DB key authentication is disabled. App Service Authentication requires every visitor to sign in with a Microsoft account (work, school or personal).

The app needs these App Service application settings: `COSMOS_ENDPOINT`, `COSMOS_DATABASE`, and `CREDENTIALS_KEY`, the encryption key for saved Octopus details. Generate the key once with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` and keep a copy somewhere safe: if it is lost or changed, users must re-enter their Octopus details. Optionally set `LEGACY_DATA_OWNER_ID` (and `LEGACY_DATA_OWNER`, an email) so data saved before accounts existed is claimed by that one user the first time they sign in.

## Accounts and privacy

- Anyone with a Microsoft account can sign in. Each person connects their own Octopus account in the page: they enter the account number and API key once and the app finds their meter itself. The Octopus address is fixed in code and cannot be supplied by users.
- The API key is encrypted with `CREDENTIALS_KEY` before it is stored, is never sent back to the browser, and can be removed with **Disconnect**.
- Every stored usage day, saved quote, simulation result and setting carries the signed-in user's ID, and every query filters on it, so users cannot see each other's data. Only the public York weather data is shared. This is covered by `tests/test_store.py`.
- Sign-in is handled by App Service Authentication. The app trusts the user ID header it sets, and refuses requests without it when running in Azure. Changing requests must carry an `X-Solar-Client` header, which stops other websites acting on a signed-in user's behalf.

Run the tests with `.venv/bin/python -m pytest tests`.
