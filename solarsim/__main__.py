from __future__ import annotations

import argparse
import os
from dataclasses import fields

from dotenv import load_dotenv

from . import service
from .store import Store


def main() -> None:
    load_dotenv()
    p = argparse.ArgumentParser(prog="solarsim", description="Intelligent Octopus Go -> solar + battery simulator")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("web", help="Run the web app on http://127.0.0.1:8000")
    u = sub.add_parser("fetch-usage", help="Fetch Octopus consumption into Cosmos DB")
    u.add_argument("--days", type=int, default=365)
    s = sub.add_parser("simulate", help="Run scenarios and payback")
    for f in fields(service.Inputs):
        flag = "--" + f.name.replace("_", "-")
        if f.type == "bool":
            s.add_argument(flag, dest=f.name, action="store_true")
        else:
            s.add_argument(flag, dest=f.name, help=f"default {f.default}")

    args = p.parse_args()
    if args.cmd == "web":
        from .web import main as web_main
        web_main()
        return
    store = Store(os.environ["COSMOS_ENDPOINT"], os.environ.get("COSMOS_DATABASE", "solarsim"))
    if args.cmd == "fetch-usage":
        print(service.fetch_consumption(store, args.days))
        return

    r = service.run(store, {k: v for k, v in vars(args).items() if v is not None and k != "cmd"})
    print(f"Usage {r['data_from']} to {r['data_to']}, solar yield {r['yield_kwh_per_kwp']} kWh/kWp")
    for sc in r["quotes"][0]["scenarios"]:
        a = sc["annual"]
        print(f"\n=== {sc['scenario']} ===")
        print(f"  Saving £{a['saving_gbp']:,.0f}/yr (bill £{a['cost_before_gbp']:,.0f} -> £{a['cost_after_gbp']:,.0f}); "
              f"{a['peak_usage_shifted_to_solar_pct']:.0f}% of daytime usage from solar")
        for pb in sc["payback"]:
            when = f"{pb['breakeven_years']:.1f} yrs -> {pb['profit_from']}" if pb["breakeven_years"] else "> 25 yrs"
            print(f"  Break-even ({pb['price_rise_pct']}% price rise/yr): {when}")


if __name__ == "__main__":
    main()
