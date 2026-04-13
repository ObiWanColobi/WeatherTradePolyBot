from config import TRADING_MODE


def create_executor():
    """
    Create the appropriate executor based on TRADING_MODE config.

    Returns PaperExecutor for paper mode (default), LiveExecutor for live mode.
    Imports are deferred to avoid circular imports.
    """
    if TRADING_MODE == "live":
        from executor.live import LiveExecutor
        return LiveExecutor()
    else:
        from executor.paper import PaperExecutor
        return PaperExecutor()
