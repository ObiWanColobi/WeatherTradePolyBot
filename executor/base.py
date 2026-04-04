from abc import ABC, abstractmethod


class BaseExecutor(ABC):

    @abstractmethod
    def place_order(self, market: dict, direction: str, size_usdc: float, estimate: dict):
        """Open a new position."""

    @abstractmethod
    def close_partial(self, trade: dict, sell_pct: float, reason: str):
        """Close a fraction of an open position (0.0 – 1.0)."""

    @abstractmethod
    def close_full(self, trade: dict, reason: str):
        """Close an entire open position."""

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
