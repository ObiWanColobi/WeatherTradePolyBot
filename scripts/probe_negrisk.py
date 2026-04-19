"""
Probe the NegRiskAdapter to confirm our markets are neg-risk and
identify the correct redemption interface.

Polymarket's NegRiskAdapter at 0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296
exposes a getPositionId(conditionId, isYes) helper. If it returns the
stored token_id for one of (isYes=true, isYes=false), we've confirmed
the market type and the indexSet mapping.

Also queries Polymarket Gamma API for the negRisk flag.
"""
import json
import sqlite3
import sys
import urllib.request
from web3 import Web3

from config import WEATHER, WALLET_FUNDER_ADDRESS

_NEG_RISK_ADAPTER = Web3.to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296")

# Minimal ABI — Polymarket's NegRiskAdapter
_NR_ABI = [
    {"inputs": [{"type": "bytes32"}, {"type": "bool"}],
     "name": "getPositionId", "outputs": [{"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "bytes32"}],
     "name": "getDetermined", "outputs": [{"type": "bool"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "bytes32"}],
     "name": "getPayout", "outputs": [{"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
]


def gamma_market(cond_id):
    """Fetch market info from Polymarket Gamma API."""
    url = f"https://gamma-api.polymarket.com/markets?condition_ids={cond_id}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        if isinstance(data, list) and data:
            return data[0]
    except Exception as e:
        return {"error": str(e)}
    return None


def main():
    rpc = WEATHER.get("polygon_rpc_url", "https://polygon-rpc.com")
    w3 = Web3(Web3.HTTPProvider(rpc))
    if not w3.is_connected():
        print(f"cannot connect to {rpc}")
        sys.exit(1)

    adapter = w3.eth.contract(address=_NEG_RISK_ADAPTER, abi=_NR_ABI)

    conn = sqlite3.connect("weather_bot.db")
    cur = conn.cursor()
    cur.execute(
        "SELECT id, market_name, direction, token_id, market_id "
        "FROM trades WHERE token_id IS NOT NULL "
        "ORDER BY id DESC LIMIT 6"
    )
    rows = cur.fetchall()
    conn.close()

    for tid, name, direction, token_id_str, cond_hex in rows:
        token_id = int(token_id_str)
        cond_bytes = bytes.fromhex(cond_hex.replace("0x", ""))

        print(f"#{tid} {direction} {name[:55]}")
        print(f"  conditionId: {cond_hex}")
        print(f"  token_id:    {token_id_str}")

        # Gamma API check
        gm = gamma_market(cond_hex)
        if gm and "error" not in gm:
            neg = gm.get("negRisk", gm.get("neg_risk", "?"))
            closed = gm.get("closed", "?")
            print(f"  Gamma: negRisk={neg}  closed={closed}  umaResolutionStatuses={gm.get('umaResolutionStatuses', '?')}")
        elif gm:
            print(f"  Gamma error: {gm.get('error')}")
        else:
            print(f"  Gamma: no data")

        # Try NegRiskAdapter
        try:
            pid_yes = adapter.functions.getPositionId(cond_bytes, True).call()
            pid_no  = adapter.functions.getPositionId(cond_bytes, False).call()
            print(f"  NegRiskAdapter.getPositionId(cond, YES)={pid_yes}")
            print(f"  NegRiskAdapter.getPositionId(cond, NO) ={pid_no}")
            if pid_yes == token_id:
                print(f"  >> MATCH: this is neg-risk, token is YES side")
            elif pid_no == token_id:
                print(f"  >> MATCH: this is neg-risk, token is NO side")
            else:
                print(f"  >> no match from adapter")
        except Exception as e:
            print(f"  NegRiskAdapter call failed: {e}")

        try:
            determined = adapter.functions.getDetermined(cond_bytes).call()
            print(f"  NegRiskAdapter.getDetermined(cond)={determined}")
        except Exception as e:
            print(f"  getDetermined failed: {e}")

        print()


if __name__ == "__main__":
    main()
