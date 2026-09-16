"""Run with: uv run pytest."""

import unittest
from decimal import Decimal

from monster_light.domain.portfolio import (
    InsufficientCash,
    InsufficientShares,
    InvalidMoney,
    InvalidQuantity,
    InvalidSymbol,
    Portfolio,
    PositionNotFound,
)


class PortfolioTests(unittest.TestCase):
    def setUp(self):
        self.portfolio = Portfolio(Decimal("100.00"))

    def assert_rejected(self, error, operation, *args):
        before = (self.portfolio.cash, dict(self.portfolio.positions))
        with self.assertRaises(error):
            operation(*args)
        self.assertEqual(
            (self.portfolio.cash, dict(self.portfolio.positions)), before
        )

    def test_buy_reduces_cash_and_accumulates_normalized_position(self):
        self.portfolio.buy(" aapl ", 2, Decimal("10.25"))
        self.assertIsInstance(self.portfolio.cash, Decimal)
        self.assertEqual(self.portfolio.cash, Decimal("79.50"))
        self.assertEqual(self.portfolio.positions, {"AAPL": 2})
        self.portfolio.buy("AaPl", 1, Decimal("0.10"))
        self.assertEqual(self.portfolio.cash, Decimal("79.40"))
        self.assertEqual(self.portfolio.positions, {"AAPL": 3})

    def test_oversell_rejected_without_mutation(self):
        self.portfolio.buy("AAPL", 2, Decimal("10"))
        self.assert_rejected(
            InsufficientShares, self.portfolio.sell, "AAPL", 3, Decimal("12")
        )

    def test_quantity_for_normalizes_symbols_without_mutation(self):
        self.portfolio.buy("AAPL", 2, Decimal("10"))
        before = (self.portfolio.cash, dict(self.portfolio.positions))
        for symbol, expected in (("AAPL", 2), (" aApL \t", 2), ("MSFT", 0), (" msft ", 0)):
            with self.subTest(symbol=symbol):
                quantity = self.portfolio.quantity_for(symbol)
                self.assertIs(type(quantity), int)
                self.assertEqual(quantity, expected)
        self.assertEqual((self.portfolio.cash, dict(self.portfolio.positions)), before)
        self.portfolio.sell("AAPL", 2, Decimal("10"))
        self.assertEqual(self.portfolio.quantity_for(" aapl "), 0)

    def test_quantity_for_rejects_invalid_symbols_without_mutation(self):
        self.portfolio.buy("AAPL", 2, Decimal("10"))
        for symbol in ("", " \t", None, 123, []):
            with self.subTest(symbol=symbol):
                self.assert_rejected(InvalidSymbol, self.portfolio.quantity_for, symbol)

    def test_insufficient_cash_rejected_without_mutation(self):
        self.portfolio.buy("AAPL", 1, Decimal("10"))
        for symbol in ("AAPL", "MSFT"):
            with self.subTest(symbol=symbol):
                self.assert_rejected(
                    InsufficientCash, self.portfolio.buy, symbol, 10, Decimal("10")
                )

    def test_partial_and_full_sell(self):
        self.portfolio.buy("AAPL", 3, Decimal("10"))
        self.portfolio.sell(" aapl ", 1, Decimal("12.25"))
        self.assertEqual(self.portfolio.cash, Decimal("82.25"))
        self.assertEqual(self.portfolio.positions, {"AAPL": 2})
        self.portfolio.sell("AAPL", 2, Decimal("9"))
        self.assertEqual(self.portfolio.cash, Decimal("100.25"))
        self.assertEqual(self.portfolio.positions, {})

    def test_unknown_position_rejected(self):
        self.assert_rejected(
            PositionNotFound, self.portfolio.sell, "MSFT", 1, Decimal("1")
        )

    def test_invalid_inputs_leave_state_unchanged(self):
        self.portfolio.buy("AAPL", 2, Decimal("10"))
        for operation in (self.portfolio.buy, self.portfolio.sell):
            cases = [
                *((InvalidSymbol, symbol, 1, Decimal("1"))
                  for symbol in ("", " \t", None, 123)),
                *((InvalidQuantity, "AAPL", quantity, Decimal("1"))
                  for quantity in (0, -1, True, False, "1", Decimal("1"), 1.5)),
                *((InvalidMoney, "AAPL", 1, price)
                  for price in (Decimal("0"), Decimal("-1"), Decimal("NaN"),
                                Decimal("sNaN"), Decimal("Infinity"),
                                Decimal("-Infinity"), 1.0, 1, "1", None)),
            ]
            for error, symbol, quantity, price in cases:
                with self.subTest(operation=operation.__name__, args=(symbol, quantity, price)):
                    self.assert_rejected(error, operation, symbol, quantity, price)

    def test_initial_cash_validation(self):
        for cash in (Decimal("-1"), Decimal("NaN"), Decimal("Infinity"), 1.0, "1"):
            with self.subTest(cash=cash), self.assertRaises(InvalidMoney):
                Portfolio(cash)
        self.assertEqual(Portfolio(Decimal("0")).cash, Decimal("0"))

    def test_buy_can_use_exact_cash_balance(self):
        self.portfolio.buy("AAPL", 10, Decimal("10"))
        self.assertEqual(self.portfolio.cash, Decimal("0"))

    def test_public_state_cannot_be_mutated_directly(self):
        with self.assertRaises(TypeError):
            self.portfolio.positions["AAPL"] = 1
        with self.assertRaises(AttributeError):
            self.portfolio.cash = Decimal("200")


if __name__ == "__main__":
    unittest.main()
