from __future__ import annotations

import argparse
import os
from dataclasses import fields
from datetime import datetime, timezone

from dotenv import load_dotenv

from . import bmw, service
from .store import Database


def _epoch(v: int) -> datetime:
    # CarData doesn't say whether session times are seconds or milliseconds
    return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=timezone.utc)


def main() -> None:
    load_dotenv()
    p = argparse.ArgumentParser(prog="solarsim", description="Intelligent Octopus Go -> solar + battery simulator")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("web", help="Run the web app on http://127.0.0.1:8000")
    u = sub.add_parser("fetch-usage", help="Fetch Octopus consumption into Cosmos DB")
    u.add_argument("--days", type=int, default=365)
    b = sub.add_parser("bmw-login", help="Connect your BMW CarData account (device code login)")
    b.add_argument("--client-id", help="CarData client ID from the My BMW portal (or set BMW_CLIENT_ID)")
    bs = sub.add_parser("bmw-sessions", help="List recent BMW charging sessions")
    bs.add_argument("--days", type=int, default=30)
    sub.add_parser("bmw-sync", help="Save BMW charging sessions into Cosmos DB (BMW only keeps ~2 months)")
    sub.add_parser("bmw-compare", help="Compare BMW charging energy with Octopus's EV readings")
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
    store = Database(os.environ["COSMOS_ENDPOINT"], os.environ.get("COSMOS_DATABASE", "solarsim")).for_user(service.local_user())
    if args.cmd == "fetch-usage":
        print(service.fetch_consumption(store, args.days))
        return
    if args.cmd == "bmw-login":
        client_id = args.client_id or os.environ.get("BMW_CLIENT_ID")
        if not client_id:
            raise SystemExit("Pass --client-id or set BMW_CLIENT_ID")
        flow = bmw.start_device_flow(client_id)
        print(f"\n1. Open {flow['verification_uri']}\n2. Sign in to your BMW account and enter the code: {flow['user_code']}")
        if flow.get("verification_uri_complete"):
            print(f"   (or go straight to {flow['verification_uri_complete']})")
        print("\nWaiting for you to approve...")
        service.bmw_connect(store, client_id, bmw.wait_for_tokens(client_id, flow)["refresh_token"])
        print("Connected. Try: python -m solarsim bmw-sessions")
        return
    if args.cmd == "bmw-sync":
        print(service.sync_bmw(store, force=True))
        return
    if args.cmd == "bmw-compare":
        c = service.bmw_compare(store)
        print(f"{'date':<12}{'BMW kWh':>9}{'Octopus EV kWh':>16}{'difference':>12}")
        for d in c["days"]:
            print(f"{d['date']:<12}{d['bmw']:>9}{d['octopus']:>16}{d['bmw'] - d['octopus']:>12.1f}")
        print(f"\nTotal: BMW {c['bmw_kwh']} kWh, Octopus EV {c['octopus_kwh']} kWh")
        print(f"Half-hours with charging: {c['slots_compared']}, within 0.5 kWh of each other: {c['slots_within_half_kwh']}, "
              f"correlation {c['correlation']}")
        return
    if args.cmd == "bmw-sessions":
        for s in sorted(service.bmw_sessions(store, args.days), key=lambda x: x["startTime"]):
            start = _epoch(s["startTime"])
            end = _epoch(s["endTime"]) if s.get("endTime") else None
            where = (s.get("chargingLocation") or {}).get("municipality", "?")
            span = f"{end:%H:%M}Z, {(end - start).total_seconds() / 3600:.1f} h" if end else "no end time"
            print(f"{start:%Y-%m-%d %H:%M}Z -> {span}  {s.get('energyConsumedFromPowerGridKwh')} kWh from grid, "
                  f"SoC {s['displayedStartSoc']}% -> {s['displayedSoc']}%, {where}"
                  + ("  [BMW flagged an error]" if s.get("businessErrors") else ""))
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
