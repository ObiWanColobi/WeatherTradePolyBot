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
def test_claim_winnings_builds_and_sends_tx(MockWeb3, mock_web3):
    MockWeb3.return_value = mock_web3
    MockWeb3.HTTPProvider = MagicMock()

    # Mock contract
    mock_contract = MagicMock()
    mock_fn = MagicMock()
    mock_fn.build_transaction.return_value = {
        "chainId": 137, "from": "0xwallet", "nonce": 42, "gas": 200000,
    }
    mock_contract.functions.redeemPositions.return_value = mock_fn
    mock_web3.eth.contract.return_value = mock_contract

    # Mock signing + sending
    mock_web3.eth.account.from_key.return_value = MagicMock(address="0xwallet")
    mock_signed = MagicMock()
    mock_web3.eth.account.sign_transaction.return_value = mock_signed
    mock_web3.eth.send_raw_transaction.return_value = b"\xab" * 32

    from chain.claimer import Claimer
    c = Claimer(rpc_url="https://polygon-rpc.com", private_key="0x" + "ab" * 32)
    tx_hash = c.claim_winnings(
        condition_id="0x" + "ab" * 32,
        index_sets=[1],
    )
    assert tx_hash is not None
    mock_contract.functions.redeemPositions.assert_called_once()


@patch("chain.claimer.Web3")
def test_check_tx_status_confirmed(MockWeb3, mock_web3):
    MockWeb3.return_value = mock_web3
    MockWeb3.HTTPProvider = MagicMock()
    mock_web3.eth.get_transaction_receipt.return_value = {"status": 1}
    mock_web3.eth.account.from_key.return_value = MagicMock(address="0xwallet")

    from chain.claimer import Claimer
    c = Claimer(rpc_url="https://polygon-rpc.com", private_key="0x" + "ab" * 32)
    status = c.check_tx_status("0x" + "ab" * 32)
    assert status == "confirmed"


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
