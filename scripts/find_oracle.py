"""
Reverse-lookup: find which oracle registered our conditionId on CTF.

For a CTF condition:
    conditionId = keccak256(abi.encodePacked(oracle, questionId, outcomeSlotCount))

We know conditionId and questionId (from CLOB API). outcomeSlotCount=2 for
binary markets. Enumerating candidate oracle addresses, the one whose
keccak hash matches the conditionId is the reporter — and is also the
redemption entry point for these tokens.
"""
import sqlite3
import sys
from web3 import Web3

from config import WEATHER
from py_clob_client.client import ClobClient
from config import (
    POLYMARKET_CLOB_API, WALLET_PRIVATE_KEY,
    WALLET_SIGNATURE_TYPE, WALLET_FUNDER_ADDRESS,
)

# Candidate Polymarket adapter / oracle addresses on Polygon
CANDIDATES = [
    ("UmaCtfAdapter_v1",            "0xCB1822859cEF82Cd2Eb4E6276C7BC60B91A96c22"),
    ("UmaCtfAdapter_v2",            "0x6A9D222616C90FcA5754cd1333cFD9b7fb6a4F74"),
    ("NegRiskAdapter_guess",        "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"),
    ("NegRiskCtfExchange",          "0xC5d563A36AE78145C45a50134d48A1215220f80a"),
    ("NegRiskOperator_guess1",      "0x71523d6aE8D01731f2C80CeE8d58DdD7B8cE6B67"),
    ("NegRiskOperator_guess2",      "0xcB92B6d1E4Ab1A48e67b7cc518d59b3c51B2f23e"),
    ("UmaOptimisticOracleV2",       "0xeE3Afe347D5C74317041E2618C49534dAf887c24"),
    ("Polymarket_Proxy_Factory",    "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"),
]


def compute_condition_id(oracle, question_id_hex, outcome_slots):
    """conditionId = keccak256(abi.encodePacked(oracle, questionId, outcomeSlotCount))"""
    oracle_bytes = bytes.fromhex(oracle.replace("0x", "").rjust(40, "0"))
    question_bytes = bytes.fromhex(question_id_hex.replace("0x", "").rjust(64, "0"))
    slots_bytes = outcome_slots.to_bytes(32, "big")
    return "0x" + Web3.keccak(oracle_bytes + question_bytes + slots_bytes).hex()


def main():
    # Fetch a few markets from CLOB to get questionIds
    kwargs = {
        "host": POLYMARKET_CLOB_API,
        "key": WALLET_PRIVATE_KEY,
        "chain_id": 137,
        "signature_type": WALLET_SIGNATURE_TYPE,
    }
    if WALLET_FUNDER_ADDRESS:
        kwargs["funder"] = WALLET_FUNDER_ADDRESS
    client = ClobClient(**kwargs)

    conn = sqlite3.connect("weather_bot.db")
    cur = conn.cursor()
    cur.execute(
        "SELECT id, market_name, market_id FROM trades "
        "WHERE market_id IS NOT NULL ORDER BY id DESC LIMIT 4"
    )
    rows = cur.fetchall()
    conn.close()

    for tid, name, cond in rows:
        print(f"#{tid} {name[:55]}")
        print(f"  conditionId: {cond}")
        try:
            mkt = client.get_market(cond)
        except Exception as e:
            print(f"  CLOB error: {e}\n")
            continue

        qid = mkt.get("question_id")
        print(f"  questionId:  {qid}")

        target = cond.lower().replace("0x", "")
        for label, oracle in CANDIDATES:
            for slots in (2, 1):
                computed = compute_condition_id(oracle, qid, slots).lower().replace("0x", "")
                if computed == target:
                    print(f"  >> MATCH: oracle={label} ({oracle}) slots={slots}")
        else:
            pass
        # Also show what each candidate computes (first 2) for debugging
        print(f"  Checked {len(CANDIDATES)} candidates × [slots=2,1]. See MATCH line above if found.\n")


if __name__ == "__main__":
    main()
