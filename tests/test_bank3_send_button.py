"""Acceptance tests for BANK-3: the Send button on small screens."""

from test_web import WEB_DIR, hiding_rules, send_button_tokens

from bank import ledger
from bank.money import Money

PHONE_WIDTH_PX = 390


def test_BANK3_send_button_visible_small_screen():
    css = (WEB_DIR / "app.css").read_text(encoding="utf-8")
    assert hiding_rules(css, send_button_tokens(), PHONE_WIDTH_PX) == []


def test_BANK3_balance_after_send(conn):
    customer = ledger.create_customer(conn, "Scenario Customer")
    source = ledger.open_account(conn, customer, "EUR")
    target = ledger.open_account(conn, customer, "EUR")
    ledger.deposit(conn, source.id, Money.of("100.00", "EUR"))
    ledger.transfer(conn, source.id, target.id, Money.of("10.00", "EUR"))
    assert ledger.get_account(conn, source.id).balance_minor == 9000
