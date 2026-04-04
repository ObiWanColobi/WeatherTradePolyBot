from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class LayerEstimate:
    probability: float          # estimated true probability (0.0 – 1.0)
    confidence:  float          # how much weight to give this estimate (0.0 – 1.0)
    source:      str            # layer name, for logging
    raw_data:    dict = field(default_factory=dict)   # whatever the layer used


class BaseLayer(ABC):
    @abstractmethod
    def can_handle(self, market: dict) -> bool:
        """Return True if this layer has relevant signal for this market type."""

    @abstractmethod
    def estimate(self, market: dict) -> LayerEstimate | None:
        """
        Return a probability estimate, or None if no signal is available.
        None means "pass" — the aggregator will skip this layer for this market.
        """

    def refresh(self):
        """Optional: refresh any external data caches. Called periodically by main loop."""
