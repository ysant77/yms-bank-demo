"""HTTP API for YMS Bank.

The session is a demo stand-in: ``POST /api/session`` stores the chosen customer id in the
``yms_customer`` cookie. There are no passwords. Every account route resolves the account
through ``owned_account``, which answers 404 for accounts of other customers.

    uv run uvicorn bank.api:app
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from bank import ledger, seed_data
from bank.db import connect
from bank.money import Money, exponent, from_minor

COOKIE = "yms_customer"
STATEMENT_DAYS = 30


class SessionIn(BaseModel):
    customer_id: int


class TransferIn(BaseModel):
    from_account_id: int
    to_account_number: str = Field(min_length=1, max_length=32)
    amount: str = Field(pattern=r"^\d{1,12}(\.\d{1,3})?$")
    reference: str | None = Field(default=None, max_length=40)


def money_fields(prefix: str, minor: int, currency: str) -> dict[str, object]:
    return {prefix: str(from_minor(minor, currency)), f"{prefix}_minor": minor}


def account_json(account: ledger.Account) -> dict[str, object]:
    return {
        "id": account.id,
        "number": account.number,
        "currency": account.currency,
        **money_fields("balance", account.balance_minor, account.currency),
    }


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


def current_customer(request: Request, conn: Conn) -> int:
    raw = request.cookies.get(COOKIE, "")
    if not raw.isdigit():
        raise HTTPException(401, "choose a demo customer first")
    try:
        return int(ledger.get_customer(conn, int(raw))["id"])
    except ledger.NotFound:
        raise HTTPException(401, "choose a demo customer first") from None


CustomerId = Annotated[int, Depends(current_customer)]


def owned_account(account_id: int, customer_id: CustomerId, conn: Conn) -> ledger.Account:
    """The account, if the session customer owns it. Anything else is a plain 404."""
    try:
        account = ledger.get_account(conn, account_id)
    except ledger.NotFound:
        account = None
    if account is None or account.customer_id != customer_id:
        raise HTTPException(404, "account not found")
    return account


OwnedAccount = Annotated[ledger.Account, Depends(owned_account)]


def parse_amount(text: str, currency: str) -> Money:
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise HTTPException(400, "amount is not a number") from None
    if -value.as_tuple().exponent > exponent(currency):
        raise HTTPException(400, f"too many decimals for {currency}")
    return Money.of(value, currency)


def create_app(db_path: str | Path | None = None, *, seed_if_empty: bool = True) -> FastAPI:
    path = Path(db_path or os.environ.get("BANK_DB", "demo.db"))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        conn = ledger.open_db(path)
        try:
            if seed_if_empty and not ledger.list_customers(conn):
                seed_data.build(conn)
        finally:
            conn.close()
        yield

    app = FastAPI(title="YMS Bank demo", lifespan=lifespan)
    app.state.db_path = path

    @app.get("/api/customers")
    def list_customers(conn: Conn) -> list[dict[str, object]]:
        return [{"id": row["id"], "name": row["name"]} for row in ledger.list_customers(conn)]

    @app.post("/api/session")
    def start_session(body: SessionIn, response: Response, conn: Conn) -> dict[str, object]:
        try:
            customer = ledger.get_customer(conn, body.customer_id)
        except ledger.NotFound:
            raise HTTPException(404, "customer not found") from None
        response.set_cookie(COOKIE, str(customer["id"]), httponly=True, samesite="lax")
        return {"id": customer["id"], "name": customer["name"]}

    @app.get("/api/session")
    def read_session(customer_id: CustomerId, conn: Conn) -> dict[str, object]:
        customer = ledger.get_customer(conn, customer_id)
        return {"id": customer["id"], "name": customer["name"]}

    @app.delete("/api/session", status_code=204)
    def end_session(response: Response) -> None:
        response.delete_cookie(COOKIE)

    @app.get("/api/accounts")
    def list_accounts(customer_id: CustomerId, conn: Conn) -> list[dict[str, object]]:
        return [account_json(a) for a in ledger.list_accounts(conn, customer_id)]

    @app.get("/api/accounts/{account_id}")
    def read_account(account: OwnedAccount) -> dict[str, object]:
        return account_json(account)

    @app.get("/api/accounts/{account_id}/statement")
    def read_statement(
        account: OwnedAccount,
        conn: Conn,
        start: Annotated[date | None, Query(alias="from")] = None,
        end: Annotated[date | None, Query(alias="to")] = None,
    ) -> dict[str, object]:
        end = end or datetime.now(UTC).date()
        start = start or end - timedelta(days=STATEMENT_DAYS - 1)
        try:
            st = ledger.statement(conn, account.id, start, end)
        except ledger.LedgerError as exc:
            raise HTTPException(400, str(exc)) from None
        cur = account.currency
        return {
            "account": account_json(account),
            "from": st.start.isoformat(),
            "to": st.end.isoformat(),
            **money_fields("opening_balance", st.opening_minor, cur),
            **money_fields("closing_balance", st.closing_minor, cur),
            "lines": [
                {
                    "entry_id": line.entry_id,
                    "posted_at": line.posted_at,
                    "kind": line.kind,
                    "description": line.description,
                    "reference": line.reference,
                    **money_fields("amount", line.amount_minor, cur),
                    **money_fields("balance", line.balance_minor, cur),
                }
                for line in st.lines
            ],
        }

    @app.post("/api/transfers", status_code=201)
    def create_transfer(body: TransferIn, customer_id: CustomerId, conn: Conn) -> dict[str, object]:
        source = owned_account(body.from_account_id, customer_id, conn)
        try:
            target = ledger.get_account_by_number(conn, body.to_account_number.strip().upper())
        except ledger.NotFound:
            target = None
        if target is None or target.kind != "customer":
            raise HTTPException(404, "destination account not found")
        amount = parse_amount(body.amount, source.currency)
        try:
            entry_id = ledger.transfer(conn, source.id, target.id, amount, body.reference)
        except ledger.InsufficientFunds as exc:
            raise HTTPException(409, str(exc)) from None
        except ledger.LedgerError as exc:
            raise HTTPException(400, str(exc)) from None
        return {
            "entry_id": entry_id,
            "to_account_number": target.number,
            **money_fields("amount", amount.minor, amount.currency),
            "currency": amount.currency,
            "from_account": account_json(ledger.get_account(conn, source.id)),
        }

    return app


app = create_app()
