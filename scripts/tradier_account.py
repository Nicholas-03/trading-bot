"""Look at the Tradier account behind TRADIER_ACCESS_TOKEN (read-only).

    .venv/bin/python scripts/tradier_account.py [--save]

Tries the token against the sandbox (paper) and the live API, lists the accounts it can see (/v1/user/profile),
then balances, positions and recent orders of each. --save writes TRADIER_ACCOUNT_ID and TRADIER_PAPER to .env when
exactly one account is found. The token itself comes from the Tradier dashboard (Settings -> API Access); personal
accounts cannot mint tokens through the API (OAuth is for approved partners only).
"""
import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
HOSTS = {"sandbox (paper)": "https://sandbox.tradier.com/v1", "live": "https://api.tradier.com/v1"}


def get(base: str, path: str, token: str):
    req = urllib.request.Request(base + path, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"error": e.code}


def as_list(x):
    return x if isinstance(x, list) else [x] if x else []


def main():
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args()
    token = os.environ.get("TRADIER_ACCESS_TOKEN")
    if not token:
        raise SystemExit("TRADIER_ACCESS_TOKEN is not in .env")
    found = []
    for name, base in HOSTS.items():
        prof = get(base, "/user/profile", token)
        if "error" in prof:
            print(f"{name}: token rejected (HTTP {prof['error']})")
            continue
        for acct in as_list(prof.get("profile", {}).get("account")):
            num = acct["account_number"]
            found.append((name, num))
            print(f"\n{name}: account {num} type {acct.get('type')} status {acct.get('status')} "
                  f"classification {acct.get('classification')}")
            bal = get(base, f"/accounts/{num}/balances", token).get("balances", {})
            print("  equity", bal.get("total_equity"), "cash", bal.get("total_cash"), "type", bal.get("account_type"),
                  "margin:", {k: v for k, v in (bal.get("margin") or {}).items() if "buying_power" in k})
            for p in as_list((get(base, f"/accounts/{num}/positions", token).get("positions") or {}).get("position")):
                print(f"  position {p['symbol']:8s} qty {p['quantity']} cost {p['cost_basis']}")
            orders = as_list((get(base, f"/accounts/{num}/orders", token).get("orders") or {}).get("order"))
            for o in orders[-10:]:
                print(f"  order {o.get('create_date', '')[:16]} {o.get('side'):12s} {o.get('symbol'):8s} "
                      f"{o.get('type')} {o.get('status')} qty {o.get('quantity')} avg {o.get('avg_fill_price')}")
    if args.save and len(found) == 1:
        name, num = found[0]
        env = ROOT / ".env"
        keep = [l for l in env.read_text().splitlines() if l.split("=", 1)[0] not in ("TRADIER_ACCOUNT_ID", "TRADIER_PAPER")]
        env.write_text("\n".join(keep + [f"TRADIER_ACCOUNT_ID={num}", f"TRADIER_PAPER={'true' if 'sandbox' in name else 'false'}"]) + "\n")
        print(f"\nsaved TRADIER_ACCOUNT_ID and TRADIER_PAPER ({name}) to .env")


if __name__ == "__main__":
    main()
