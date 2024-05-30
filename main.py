import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from pprint import pprint
from typing import List, Optional
from zoneinfo import ZoneInfo

import requests
from fiobank import FioBank
from more_itertools import flatten
from pydantic.json import pydantic_encoder
from requests.models import Response
from schwifty import IBAN


class FireflyClient:
    def __init__(self, url: str, token: str):
        self.url = url
        self.token = token

    def request(self, method: str, url: str, data=None) -> Response:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }

        if data is not None:
            headers["Content-Type"] = "application/json"

        return requests.request(
            method, f"{self.url}/api/v1/{url}", data=data, headers=headers
        )


class TransactionType(str, Enum):
    withdrawal = "withdrawal"
    deposit = "deposit"
    transfer = "transfer"
    reconciliation = "reconciliation"
    opening_balance = "opening balance"


@dataclass
class Transaction:
    type: TransactionType
    date: date
    amount: float
    description: str
    notes: str
    external_id: int
    source_id: Optional[str] = None
    destination_id: Optional[str] = None
    source_name: Optional[str] = None
    destination_name: Optional[str] = None

    @classmethod
    def from_fio_data(
        cls, account: dict, transaction: dict, firefly_client: FireflyClient
    ):
        try:
            other_account_iban = str(
                IBAN.generate(
                    "CZ",
                    transaction["bank_code"],
                    transaction["account_number"].replace("-", ""),
                )
            )
            other_account_data = find_account_by_iban(
                firefly_client, other_account_iban
            )
        except (ValueError, AttributeError):
            other_account_data = None

        account_data = find_account_by_iban(firefly_client, account["iban"])

        type = (
            TransactionType.transfer
            if other_account_data is not None
            and other_account_data.type == AccountType.asset
            else TransactionType.withdrawal
            if transaction["amount"] < 0
            else TransactionType.deposit
        )

        result = cls(
            type=type,
            date=transaction["date"],
            amount=abs(transaction["amount"]),
            description=transaction.get("recipient_message") or "-",
            notes=transaction.get("user_identification") or "-",
            external_id=transaction["instruction_id"],
        )

        if result.type == TransactionType.withdrawal:
            result.destination_name = transaction["account_name"]

            if account_data is not None:
                result.source_id = account_data.id

            if other_account_data is not None:
                result.destination_id = other_account_data.id

        if result.type == TransactionType.deposit:
            result.source_name = transaction["account_name"]

            if other_account_data is not None:
                result.source_id = other_account_data.id

            if account_data is not None:
                result.destination_id = account_data.id

        if result.type == TransactionType.transfer:
            if transaction["amount"] > 0:
                if account_data is not None:
                    result.destination_id = account_data.id

                if other_account_data is not None:
                    result.source_id = other_account_data.id
            else:
                result.notes = "-"  # The user identification is redundant in this case

                if account_data is not None:
                    result.source_id = account_data.id

                if other_account_data is not None:
                    result.destination_id = other_account_data.id

        return result


class AccountType(str, Enum):
    asset = "asset"
    expense = "expense"
    revenue = "revenue"
    cash = "cash"


@dataclass
class Account:
    id: str
    type: AccountType


def find_account_by_iban(
    firefly_client: FireflyClient, iban: Optional[str]
) -> Optional[Account]:
    if iban is None:
        return None
    response = firefly_client.request("get", f"search/accounts?field=iban&query={iban}")

    accounts = response.json()["data"]

    if response.status_code == 404 or len(accounts) == 0:
        return None

    try:
        response.raise_for_status()
    except:
        pprint(response.json())
        raise

    return Account(
        id=accounts[0]["id"], type=AccountType(accounts[0]["attributes"]["type"])
    )


def fetch_transactions(client: FioBank, since: Optional[date]):
    to_date = datetime.now(ZoneInfo(key="Europe/Prague"))
    from_date = (
        (since - timedelta(days=1)) if since else (to_date - timedelta(days=3000))
    )
    return client.period(from_date, to_date)


def store_transactions(
    firefly_client: FireflyClient, transactions: List[Transaction]
) -> None:
    for transaction in transactions:
        response = firefly_client.request(
            "post",
            "transactions",
            data=json.dumps(
                {
                    "error_if_duplicate_hash": True,
                    "transactions": [transaction],
                },
                default=pydantic_encoder,
            ),
        )

        try:
            response.raise_for_status()
        except:
            result = response.json()

            if all(
                error.lower().startswith("duplicate")
                for error in flatten(result["errors"].values())
            ):
                logging.info(
                    f"Ignoring existing {transaction.type.value} transaction from {transaction.date.isoformat()}"
                )
            else:
                pprint(result)
                raise


def fetch_last_transaction_date(
    firefly_client: FireflyClient, account_id: str
) -> Optional[date]:
    response = firefly_client.request("get", f"accounts/{account_id}/transactions")

    try:
        response.raise_for_status()
    except:
        pprint(response.json())
        raise

    data = response.json()["data"]

    non_transfers = (
        [
            it
            for it in data
            if it["attributes"]["transactions"][0]["type"]
            != TransactionType.transfer.value
        ]
        if data
        else None
    )

    if not non_transfers:
        return None

    return datetime.fromisoformat(
        non_transfers[0]["attributes"]["transactions"][0]["date"]
    ).date()


def main():
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s : %(levelname)s : %(message)s",
    )

    fio_token = os.environ["FIO_TOKEN"]
    fio_client = FioBank(fio_token)
    fio_client.base_url = "https://fioapi.fio.cz/ib_api/rest/"

    firefly_url = os.environ["FIREFLY_URL"]
    firefly_token = os.environ["FIREFLY_TOKEN"]
    firefly_client = FireflyClient(firefly_url, firefly_token)

    account = fio_client.info()
    account_data = find_account_by_iban(firefly_client, account["iban"])

    if account_data is None:
        logging.error(f"Account '{account['iban']}' not found")
        sys.exit(1)

    last_sync_date = fetch_last_transaction_date(firefly_client, account_data.id)

    transactions = [
        Transaction.from_fio_data(account, item, firefly_client)
        for item in fetch_transactions(fio_client, last_sync_date)
    ]
    logging.info(f"Fetched {len(transactions)} transactions")

    store_transactions(firefly_client, transactions)
    logging.info("Import complete")


if __name__ == "__main__":
    main()
