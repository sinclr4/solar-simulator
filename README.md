# Solar + Battery Simulator

A local web app that compares solar and battery installation quotes against your Octopus half-hourly electricity usage and York solar data. Results include estimated bill savings, payback dates, seasonal average-day charts, and monthly breakdowns.

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

Pushing to `main` deploys the app to Azure App Service through [.github/workflows/deploy.yml](.github/workflows/deploy.yml). GitHub signs in to Azure with OpenID Connect, so no Azure credentials are stored in GitHub. The workflow reads these non-secret repository variables: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, and `AZURE_WEBAPP_NAME`.

The hosted app uses free-tier resources: an App Service F1 Linux plan and Azure Cosmos DB free tier with 1000 RU/s of shared database throughput. It connects to Cosmos DB with its managed identity, and Cosmos DB key authentication is disabled. App Service Authentication allows only the configured Microsoft Entra user.

Octopus credentials are App Service application settings, not GitHub secrets. Set them from your local `.env` with:

```sh
az webapp config appsettings set -g rs-solar-finder-rg-swc -n <app-name> \
  --settings $(grep -E '^OCTOPUS_(API_KEY|ACCOUNT|MPAN|SERIAL)=' .env | xargs) -o none
```
