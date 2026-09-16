"""In-memory, whole-share portfolio rules; no execution or persistence layer."""

from collections.abc import Mapping
from decimal import Decimal, DecimalException
from types import MappingProxyType


class PortfolioError(ValueError):
    """A portfolio rule rejected an operation."""


class InvalidSymbol(PortfolioError):
    """A symbol must be a non-empty string."""


class InvalidQuantity(PortfolioError):
    """A quantity must be a positive integer (not a boolean)."""


class InvalidMoney(PortfolioError):
    """Money must be finite Decimal values with valid signs."""


class InsufficientCash(PortfolioError):
    """The portfolio cannot fund the purchase."""


class PositionNotFound(PortfolioError):
    """The portfolio does not own the requested symbol."""


class InsufficientShares(PortfolioError):
    """The sale exceeds the owned quantity."""


def _symbol(symbol: str) -> str:
    if not isinstance(symbol, str) or not symbol.strip():
        raise InvalidSymbol("Symbol must be a non-empty string")
    return symbol.strip().upper()


def _quantity(quantity: int) -> None:
    if type(quantity) is not int or quantity <= 0:
        raise InvalidQuantity("Quantity must be a positive integer")


def _money(value: Decimal, *, allow_zero: bool = False) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise InvalidMoney("Money must be a finite Decimal")
    if value < 0 or (value == 0 and not allow_zero):
        raise InvalidMoney("Cash must be non-negative; price must be positive")


def _cash_after(cash: Decimal, quantity: int, price: Decimal) -> Decimal:
    try:
        result = cash + price * quantity
    except DecimalException as error:
        raise InvalidMoney("Trade monetary calculation failed") from error
    if not result.is_finite():
        raise InvalidMoney("Trade cash must be finite")
    return result


class Portfolio:
    """Long-only portfolio initialized with cash and no positions.

    Prices are supplied directly. No fees or currency rounding are applied.
    Mutations occur only after all validation and calculations succeed.
    """

    def __init__(self, cash: Decimal) -> None:
        _money(cash, allow_zero=True)
        self._cash = cash
        self._positions: dict[str, int] = {}

    @classmethod
    def restore(cls, cash: Decimal, positions: Mapping[str, int]) -> "Portfolio":
        """Restore a validated snapshot without replaying trades.

        Stored symbols must already be normalized; reject corrupt state rather
        than silently repairing it. Copy positions to retain domain ownership.
        """
        portfolio = cls(cash)
        restored = dict(positions)
        for symbol, quantity in restored.items():
            if _symbol(symbol) != symbol:
                raise InvalidSymbol("Stored symbols must be normalized")
            _quantity(quantity)
        portfolio._positions = restored
        return portfolio

    @property
    def cash(self) -> Decimal:
        return self._cash

    @property
    def positions(self) -> Mapping[str, int]:
        return MappingProxyType(self._positions)

    def quantity_for(self, symbol: str) -> int:
        """Return owned shares, or zero if absent; raise InvalidSymbol if invalid."""
        return self._positions.get(_symbol(symbol), 0)

    def buy(self, symbol: str, quantity: int, price: Decimal) -> None:
        symbol = _symbol(symbol)
        _quantity(quantity)
        _money(price)
        cash = _cash_after(self._cash, -quantity, price)
        if cash < 0:
            raise InsufficientCash("Purchase exceeds available cash")
        owned = self._positions.get(symbol, 0) + quantity
        self._positions[symbol] = owned
        self._cash = cash

    def sell(self, symbol: str, quantity: int, price: Decimal) -> None:
        symbol = _symbol(symbol)
        _quantity(quantity)
        _money(price)
        if symbol not in self._positions:
            raise PositionNotFound(f"No position for {symbol}")
        owned = self._positions[symbol]
        if quantity > owned:
            raise InsufficientShares("Sale exceeds owned quantity")
        cash = _cash_after(self._cash, quantity, price)
        remaining = owned - quantity
        if remaining:
            self._positions[symbol] = remaining
        else:
            del self._positions[symbol]
        self._cash = cash
