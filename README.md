# Solar + Battery Simulator

A local web app that compares solar and battery installation quotes against your Octopus half-hourly electricity usage and York solar data. Results include estimated bill savings, payback dates, seasonal average-day charts, and monthly breakdowns.

## What the simulator does

The simulator replays your real electricity usage, half-hour by half-hour, as if each quoted solar and battery system had been installed. It then compares the modelled bill with what you actually would have paid on the same tariff.

For every quote, it runs three operating strategies:

| Strategy | Battery charged from | Surplus solar goes to |
| --- | --- | --- |
| Solar + battery (charged from solar only) | Solar only | Battery, then export |
| Solar + battery + off-peak grid charging | Solar, plus cheap off-peak grid electricity | Battery, then export |
| Grid charging + summer surplus to EV | Solar, plus cheap off-peak grid electricity | Battery, then the electric car (April–September), then export |

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

1. **Direct use:** solar supplies the house first.
2. **Battery charging from solar:** surplus solar charges the battery. This is limited by battery power (kW), free capacity and charging losses. Round-trip efficiency is split equally between charging and discharging (√efficiency each way).
3. **EV diversion** (summer-EV strategy only, April–September, peak hours): remaining surplus of at least 1.4 kW goes to the car, up to 7 kW. The car can only take as much as it would otherwise have charged overnight. Each kWh diverted is removed from that night's off-peak import.
4. **Export:** remaining surplus is exported up to the export limit. Anything above the limit is counted as **curtailed**.
5. **Battery discharge:** during peak hours only, the battery covers any remaining demand, limited by its power and stored energy. It never discharges in the off-peak window, because grid electricity is cheap then. It never exports to the grid.
6. **Grid import:** any demand still unmet is imported.
7. **Off-peak grid charging** (grid-charging strategies only): during 23:30–05:30, the battery is topped up from the grid. It charges only to the level needed for the following day: the peak-time shortfall after solar, less surplus solar that could refill the battery, capped at battery capacity.

The simulator tracks how much stored energy came from solar. This means **daytime usage shifted to solar** counts only solar used directly or via the battery, and excludes grid-charged energy.

**Estimating EV charging:** the baseline overnight house load is the 20th percentile of off-peak half-hour usage. Off-peak usage above that baseline is treated as EV (or other flexible) charging. Summed per night, this sets how much solar the car could have absorbed that day.

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

Edit `.env` and set `OCTOPUS_API_KEY`, `OCTOPUS_ACCOUNT`, `OCTOPUS_MPAN`, and `OCTOPUS_SERIAL`. Set `COSMOS_ENDPOINT` and `COSMOS_DATABASE` for your Cosmos DB account and database. Do not commit `.env`; it is gitignored.

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

On the page, use **Fetch usage data** to retrieve more Octopus history, adjust the shared roof/tariff assumptions and quote-specific equipment/costs, then press **Calculate**. The first fetch can take a little while. The app checks the MPAN's import meters so it can include historic readings from replaced meters.

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

The hosted app uses free-tier resources: an App Service F1 Linux plan and Azure Cosmos DB free tier with 1000 RU/s of shared database throughput. It connects to Cosmos DB with its managed identity, and Cosmos DB key authentication is disabled. App Service Authentication allows only the configured Microsoft Entra user.

Octopus credentials are App Service application settings, not GitHub secrets. Set them from your local `.env` with:

```sh
az webapp config appsettings set -g rs-solar-finder-rg-swc -n solar-simulator \
  --settings $(grep -E '^OCTOPUS_(API_KEY|ACCOUNT|MPAN|SERIAL)=' .env | xargs) -o none
```
