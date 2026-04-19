"""Tests for chain/claimer.py — all web3 calls mocked."""
import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture
def mock_web3():
    """Create a mock Web3 instance with standard Polygon responses."""
    w3 = MagicMock()
    w3.is_connected.return_value = True
    w3.eth.chain_id = 137
    w3.eth.gas_price = 30_000_000_000  # 30 gwei
    w3.eth.get_balance.return_value = 1_000_000_000_000_000_000  # 1 MATIC
    w3.eth.get_transaction_count.return_value = 42
    w3.to_checksum_address = lambda x: x
    return w3


@patch("chain.claimer.Web3")
def test_claimer_init_connects(MockWeb3, mock_web3):
    MockWeb3.return_value = mock_web3
    MockWeb3.HTTPProvider = MagicMock()

    from chain.claimer import Claimer
    c = Claimer(rpc_url="https://polygon-rpc.com", private_key="0x" + "ab" * 32)
    assert c._w3 is mock_web3


@patch("chain.claimer.Web3")
def test_claimer_init_fails_on_no_connection(MockWeb3):
    w3 = MagicMock()
    w3.is_connected.return_value = False
    MockWeb3.return_value = w3
    MockWeb3.HTTPProvider = MagicMock()

    from chain.claimer import Claimer
    with pytest.raises(ConnectionError, match="Polygon RPC"):
        Claimer(rpc_url="https://polygon-rpc.com", private_key="0x" + "ab" * 32)


@patch("chain.claimer.Web3")
def test_get_matic_balance(MockWeb3, mock_web3):
    MockWeb3.return_value = mock_web3
    MockWeb3.HTTPProvider = MagicMock()
    mock_web3.eth.get_balance.return_value = 500_000_000_000_000_000  # 0.5 MATIC

    from chain.claimer import Claimer
    c = Claimer(rpc_url="https://polygon-rpc.com", private_key="0x" + "ab" * 32)
    balance = c.get_matic_balance()
    assert abs(balance - 0.5) < 0.001


@patch("chain.claimer.Web3")
def test_claim_winnings_bundles_redeem_and_unwrap(MockWeb3, mock_web3):
    MockWeb3.return_value = mock_web3
    MockWeb3.HTTPProvider = MagicMock()

    mock_ctf = MagicMock()
    mock_redeem_fn = MagicMock()
    mock_redeem_fn._encode_transaction_data.return_value = "0xaaaa"
    mock_ctf.functions.redeemPositions.return_value = mock_redeem_fn

    mock_wcol = MagicMock()
    mock_unwrap_fn = MagicMock()
    mock_unwrap_fn._encode_transaction_data.return_value = "0xbbbb"
    mock_wcol.functions.unwrap.return_value = mock_unwrap_fn

    mock_factory = MagicMock()
    mock_factory.functions.proxy.return_value.build_transaction.return_value = {
        "chainId": 137, "nonce": 1,
    }

    mock_web3.eth.contract.side_effect = [mock_ctf, mock_wcol, mock_factory]
    mock_web3.eth.account.from_key.return_value = MagicMock(address="0xw", key=b"k")
    mock_web3.eth.account.sign_transaction.return_value = MagicMock()
    mock_web3.eth.send_raw_transaction.return_value = b"\xaa" * 32

    from chain.claimer import Claimer, _WCOL_ADDRESS
    c = Claimer(rpc_url="https://polygon-rpc.com", private_key="0x" + "ab" * 32)
    tx = c.claim_winnings(
        condition_id="0x" + "cd" * 32,
        expected_wcol=23_535_813,
    )
    assert tx is not None

    # CTF.redeemPositions(wcol, 0x0, cond, [1,2])
    redeem_args = mock_ctf.functions.redeemPositions.call_args[0]
    assert redeem_args[0].lower() == _WCOL_ADDRESS.lower()
    assert redeem_args[1] == b"\x00" * 32
    assert redeem_args[3] == [1, 2]

    # wcol.unwrap(proxy, 23_535_813)
    unwrap_args = mock_wcol.functions.unwrap.call_args[0]
    assert unwrap_args[1] == 23_535_813

    # Factory.proxy called with TWO inner calls
    calls = mock_factory.functions.proxy.call_args[0][0]
    assert len(calls) == 2


def _make_receipt(status, logs):
    return {"status": status, "logs": logs}


@patch("chain.claimer.Web3")
def test_check_tx_status_confirmed_when_both_events_present(MockWeb3, mock_web3):
    MockWeb3.return_value = mock_web3
    MockWeb3.HTTPProvider = MagicMock()

    ctf_log = {"address": "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045",
               "topics": ["0x" + "aa" * 32], "data": "0x"}
    mock_web3.eth.get_transaction_receipt.return_value = _make_receipt(1, [ctf_log])

    mock_ctf = MagicMock()
    mock_ctf_event = MagicMock()
    mock_ctf_event.process_log.return_value = {"args": {"payout": 23_535_813}}
    mock_ctf.events.PayoutRedemption.return_value = mock_ctf_event
    mock_web3.eth.contract.side_effect = [mock_ctf, MagicMock(), MagicMock()]

    from chain.claimer import Claimer
    c = Claimer(rpc_url="https://polygon-rpc.com", private_key="0x" + "ab" * 32)
    assert c.check_tx_status("0x" + "aa" * 32) == "confirmed"


@patch("chain.claimer.Web3")
def test_check_tx_status_status1_no_events_is_failed(MockWeb3, mock_web3):
    """Phantom pattern: receipt status=1 but no PayoutRedemption -> 'failed'."""
    MockWeb3.return_value = mock_web3
    MockWeb3.HTTPProvider = MagicMock()
    mock_web3.eth.get_transaction_receipt.return_value = _make_receipt(1, [])
    mock_web3.eth.contract.side_effect = [MagicMock(), MagicMock(), MagicMock()]

    from chain.claimer import Claimer
    c = Claimer(rpc_url="https://polygon-rpc.com", private_key="0x" + "ab" * 32)
    assert c.check_tx_status("0x" + "aa" * 32) == "failed"


@patch("chain.claimer.Web3")
def test_check_tx_status_payout_zero_is_failed(MockWeb3, mock_web3):
    MockWeb3.return_value = mock_web3
    MockWeb3.HTTPProvider = MagicMock()
    ctf_log = {"address": "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045",
               "topics": ["0x" + "aa" * 32], "data": "0x"}
    mock_web3.eth.get_transaction_receipt.return_value = _make_receipt(1, [ctf_log])
    mock_ctf = MagicMock()
    mock_ctf_event = MagicMock()
    mock_ctf_event.process_log.return_value = {"args": {"payout": 0}}
    mock_ctf.events.PayoutRedemption.return_value = mock_ctf_event
    mock_web3.eth.contract.side_effect = [mock_ctf, MagicMock(), MagicMock()]

    from chain.claimer import Claimer
    c = Claimer(rpc_url="https://polygon-rpc.com", private_key="0x" + "ab" * 32)
    assert c.check_tx_status("0x" + "aa" * 32) == "failed"


@patch("chain.claimer.Web3")
def test_check_tx_status_failed(MockWeb3, mock_web3):
    MockWeb3.return_value = mock_web3
    MockWeb3.HTTPProvider = MagicMock()
    mock_web3.eth.get_transaction_receipt.return_value = {"status": 0}
    mock_web3.eth.account.from_key.return_value = MagicMock(address="0xwallet")

    from chain.claimer import Claimer
    c = Claimer(rpc_url="https://polygon-rpc.com", private_key="0x" + "ab" * 32)
    status = c.check_tx_status("0x" + "ab" * 32)
    assert status == "failed"


@patch("chain.claimer.Web3")
def test_check_tx_status_pending(MockWeb3, mock_web3):
    MockWeb3.return_value = mock_web3
    MockWeb3.HTTPProvider = MagicMock()
    mock_web3.eth.get_transaction_receipt.return_value = None
    mock_web3.eth.account.from_key.return_value = MagicMock(address="0xwallet")

    from chain.claimer import Claimer
    c = Claimer(rpc_url="https://polygon-rpc.com", private_key="0x" + "ab" * 32)
    status = c.check_tx_status("0x" + "ab" * 32)
    assert status == "pending"


@patch("chain.claimer.Web3")
def test_claimer_init_loads_wcol(MockWeb3, mock_web3):
    MockWeb3.return_value = mock_web3
    MockWeb3.HTTPProvider = MagicMock()
    from chain.claimer import Claimer, _WCOL_ADDRESS
    c = Claimer(rpc_url="https://polygon-rpc.com", private_key="0x" + "ab" * 32)
    assert _WCOL_ADDRESS.lower() == "0x3a3bd7bb9528e159577f7c2e685cc81a765002e2"
    assert c._wcol is not None


@patch("chain.claimer.Web3")
def test_get_token_balance_queries_ctf(MockWeb3, mock_web3):
    MockWeb3.return_value = mock_web3
    MockWeb3.HTTPProvider = MagicMock()
    mock_ctf = MagicMock()
    mock_ctf.functions.balanceOf.return_value.call.return_value = 23_535_813
    # ctf, wcol, factory — three contracts loaded in order
    mock_web3.eth.contract.side_effect = [mock_ctf, MagicMock(), MagicMock()]

    from chain.claimer import Claimer
    c = Claimer(rpc_url="https://polygon-rpc.com", private_key="0x" + "ab" * 32)
    bal = c.get_token_balance(
        proxy_address="0x0E5CaC0fc03f5728ddFb3b0B5BeE0cC0Ec67c55d",
        token_id=12345,
    )
    assert bal == 23_535_813


@patch("chain.claimer.Web3")
def test_is_condition_redeemable_true(MockWeb3, mock_web3):
    MockWeb3.return_value = mock_web3
    MockWeb3.HTTPProvider = MagicMock()
    mock_ctf = MagicMock()
    mock_ctf.functions.payoutDenominator.return_value.call.return_value = 1
    mock_web3.eth.contract.side_effect = [mock_ctf, MagicMock(), MagicMock()]

    from chain.claimer import Claimer
    c = Claimer(rpc_url="https://polygon-rpc.com", private_key="0x" + "ab" * 32)
    assert c.is_condition_redeemable("0x" + "cd" * 32) is True


@patch("chain.claimer.Web3")
def test_is_condition_redeemable_unresolved(MockWeb3, mock_web3):
    MockWeb3.return_value = mock_web3
    MockWeb3.HTTPProvider = MagicMock()
    mock_ctf = MagicMock()
    mock_ctf.functions.payoutDenominator.return_value.call.return_value = 0
    mock_web3.eth.contract.side_effect = [mock_ctf, MagicMock(), MagicMock()]

    from chain.claimer import Claimer
    c = Claimer(rpc_url="https://polygon-rpc.com", private_key="0x" + "ab" * 32)
    assert c.is_condition_redeemable("0x" + "cd" * 32) is False
