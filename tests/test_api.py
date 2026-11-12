import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from bank import ledger
from bank.api import COOKIE, create_app
from bank.money import Money


@pytest.fixture
def bank(tmp_path, at):
    """Two customers: owner holds EUR and JPY accounts, other holds one EUR account."""
    path = tmp_path / "api.db"
    conn = ledger.open_db(path)
    owner = ledger.create_customer(conn, "Owner Test")
    other = ledger.create_customer(conn, "Other Test")
    eur = ledger.open_account(conn, owner, "EUR")
    jpy = ledger.open_account(conn, owner, "JPY")
    foreign = ledger.open_account(conn, other, "EUR")
    ledger.deposit(conn, eur.id, Money.of("100.00", "EUR"), at=at("2026-05-01"))
    ledger.deposit(conn, jpy.id, Money.of("5000", "JPY"), at=at("2026-05-01"))
    ledger.deposit(conn, foreign.id, Money.of("10.00", "EUR"), at=at("2026-05-01"))
    conn.close()
    app = create_app(path, seed_if_empty=False)
    return {"app": app, "path": path, "owner": owner, "other": other,
            "eur": eur, "jpy": jpy, "foreign": foreign}  # fmt: skip


def login(app, customer_id):
    client = TestClient(app)
    assert client.post("/api/session", json={"customer_id": customer_id}).status_code == 200
    return client


def test_session_cookie_and_unknown_customer(bank):
    client = TestClient(bank["app"])
    assert client.get("/api/accounts").status_code == 401
    assert client.post("/api/session", json={"customer_id": 999}).status_code == 404
    response = client.post("/api/session", json={"customer_id": bank["owner"]})
    assert response.status_code == 200
    assert client.cookies.get(COOKIE) == str(bank["owner"])
    assert client.get("/api/session").json()["name"] == "Owner Test"
    assert client.delete("/api/session").status_code == 204
    assert client.get("/api/accounts").status_code == 401


def test_customer_list_for_demo_chooser(bank):
    names = [c["name"] for c in TestClient(bank["app"]).get("/api/customers").json()]
    assert names == ["Owner Test", "Other Test"]


def test_list_accounts_shows_only_own(bank):
    client = login(bank["app"], bank["owner"])
    accounts = client.get("/api/accounts").json()
    assert [a["number"] for a in accounts] == [bank["eur"].number, bank["jpy"].number]
    assert accounts[0]["balance"] == "100.00" and accounts[0]["balance_minor"] == 10000
    assert accounts[1]["balance"] == "5000"


def account_routes(app):
    """Every route under /api/accounts/ that takes an account id, with each method."""
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith("/api/accounts/"):
            params = set(re.findall(r"{(\w+)}", route.path))
            assert params == {"account_id"}, f"walker cannot fill {route.path}"
            for method in sorted(route.methods):
                yield method, route.path


def test_every_account_route_checks_the_owner(bank):
    routes = list(account_routes(bank["app"]))
    assert ("GET", "/api/accounts/{account_id}") in routes
    assert ("GET", "/api/accounts/{account_id}/statement") in routes
    owner = login(bank["app"], bank["owner"])
    anonymous = TestClient(bank["app"])
    clearing = ledger.get_account_by_number(ledger.open_db(bank["path"]), "YMS-CLR-EUR")
    for method, template in routes:

        def url(account_id, template=template):
            return template.format(account_id=account_id)

        mine = owner.request(method, url(bank["eur"].id))
        assert mine.status_code == 200, (method, template, mine.text)
        for foreign_id in (bank["foreign"].id, 9999):
            response = owner.request(method, url(foreign_id))
            assert response.status_code == 404, (method, template, foreign_id)
        assert owner.request(method, url(clearing.id)).status_code == 404
        assert anonymous.request(method, url(bank["eur"].id)).status_code == 401


def test_statement_end_date_is_inclusive(bank, at):
    conn = ledger.open_db(bank["path"])
    ledger.deposit(conn, bank["eur"].id, Money.of("1.00", "EUR"), at=at("2026-05-10", "23:59:59"))
    ledger.deposit(conn, bank["eur"].id, Money.of("2.00", "EUR"), at=at("2026-05-11", "00:00:00"))
    client = login(bank["app"], bank["owner"])
    url = f"/api/accounts/{bank['eur'].id}/statement?from=2026-05-02&to=2026-05-10"
    body = client.get(url).json()
    assert body["opening_balance"] == "100.00"
    assert [line["amount"] for line in body["lines"]] == ["1.00"]
    assert body["closing_balance"] == "101.00"
    assert body["lines"][0]["balance"] == "101.00"


def test_statement_rejects_bad_dates(bank):
    client = login(bank["app"], bank["owner"])
    base = f"/api/accounts/{bank['eur'].id}/statement"
    assert client.get(base + "?from=2026-05-10&to=2026-05-01").status_code == 400
    assert client.get(base + "?from=yesterday").status_code == 422


def transfer(client, from_id, to_number, amount, **extra):
    body = {"from_account_id": from_id, "to_account_number": to_number, "amount": amount}
    return client.post("/api/transfers", json=body | extra)


def test_transfer_success(bank):
    client = login(bank["app"], bank["owner"])
    response = transfer(client, bank["eur"].id, bank["foreign"].number, "25.50", reference="Rent")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["amount"] == "25.50" and body["from_account"]["balance"] == "74.50"
    other = login(bank["app"], bank["other"])
    assert other.get(f"/api/accounts/{bank['foreign'].id}").json()["balance"] == "35.50"


def test_transfer_from_foreign_account_is_404(bank):
    client = login(bank["app"], bank["owner"])
    response = transfer(client, bank["foreign"].id, bank["eur"].number, "1.00")
    assert response.status_code == 404
    assert (
        transfer(TestClient(bank["app"]), bank["eur"].id, "YMS-0000-0003", "1").status_code == 401
    )


@pytest.mark.parametrize(
    "amount, to, status",
    [
        ("100.01", "YMS-0000-0003", 409),
        ("1.001", "YMS-0000-0003", 400),
        ("0", "YMS-0000-0003", 400),
        ("1.00", "YMS-9999-9999", 404),
        ("1.00", "YMS-CLR-EUR", 404),
        ("1.00", "YMS-0000-0002", 400),
        ("1.00", "YMS-0000-0001", 400),
        ("-5", "YMS-0000-0003", 422),
        ("1e3", "YMS-0000-0003", 422),
    ],
)
def test_transfer_errors(bank, amount, to, status):
    client = login(bank["app"], bank["owner"])
    assert transfer(client, bank["eur"].id, to, amount).status_code == status
    assert client.get(f"/api/accounts/{bank['eur'].id}").json()["balance"] == "100.00"
