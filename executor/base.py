from abc import ABC, abstractmethod


class BaseExecutor(ABC):

    @abstractmethod
    def place_order(self, market: dict, direction: str, size_usdc: float, estimate: dict) -> bool:
        """Open a new position. Returns True if filled, False otherwise."""

    @abstractmethod
    def close_partial(self, trade: dict, sell_pct: float, reason: str):
        """Close a fraction of an open position (0.0 – 1.0)."""

    @abstractmethod
    def close_full(self, trade: dict, reason: str):
        """Close an entire open position."""

    @abstractmethod
    def place_extended_order(self, market: dict, direction: str, size_usdc: float,
                              estimate: dict, parent_trade_id: int, leg_number: int):
        """Place an add-on leg for an existing extended position."""

    @abstractmethod
    def close_position(self, trade: dict, reason: str):
        """Close all legs of an extended position (or a single trade)."""

    @abstractmethod
    def settle_resolved(self, trade: dict, resolved_yes: bool):
        """Settle a trade at market resolution."""

    @abstractmethod
    def update_open_positions(self):
        """Refresh current_price and peak_price for all open positions."""

    def reconcile_positions(self):
        """
        On bot restart, sync open positions with the exchange.

        Paper mode:  no-op — positions are already persisted in the local DB.
        Live mode:   query the exchange for open positions, import any that
                     are not already tracked in the DB so the bot can manage them.
        """

    def process_pending_claims(self):
        """
        Process on-chain claims for resolved winning trades.

        Paper mode:  no-op — balance is credited immediately at resolution.
        Live mode:   submits CTF redeemPositions() transactions and polls
                     for confirmation before crediting the DB balance.
        """
