import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from functools import lru_cache
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
    source_id: Optional[str]
    destination_id: Optional[str]
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
        except (ValueError, AttributeError):
            other_account_iban = None

        type = (
            TransactionType.withdrawal
            if transaction["amount"] < 0
            else TransactionType.deposit
        )

        result = cls(
            type=type,
            date=transaction["date"],
            amount=abs(transaction["amount"]),
            description=transaction["recipient_message"],
            notes=transaction["user_identification"],
            external_id=transaction["instruction_id"],
            source_id=find_account_id_by_iban(
                firefly_client,
                account["iban"]
                if type == TransactionType.withdrawal
                else other_account_iban,
            ),
            destination_id=find_account_id_by_iban(
                firefly_client,
                account["iban"]
                if type == TransactionType.deposit
                else other_account_iban,
            ),
        )

        if result.type == TransactionType.withdrawal:
            result.destination_name = transaction["account_name"]

        if result.type == TransactionType.deposit:
            result.source_name = transaction["account_name"]

        return result


@lru_cache(maxsize=1)
def fetch_accounts(client: FireflyClient):
    response = client.request("get", "accounts")

    try:
        response.raise_for_status()
    except:
        pprint(response.json())
        raise

    return response.json()["data"]


def find_account_id_by_iban(
    firefly_client: FireflyClient, iban: Optional[str]
) -> Optional[str]:
    if iban is not None:
        for account in fetch_accounts(firefly_client):
            if account["attributes"].get("iban") == iban:
                return account["id"]


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

            if not all(
                error.lower().startswith("duplicate")
                for error in flatten(result["errors"].values())
            ):
                pprint(result)
                raise


def fetch_last_transaction_date(firefly_client: FireflyClient) -> Optional[date]:
    response = firefly_client.request("get", "transactions")

    try:
        response.raise_for_status()
    except:
        pprint(response.json())
        raise

    data = response.json()["data"]

    if not data:
        return None

    return datetime.fromisoformat(
        data[0]["attributes"]["transactions"][0]["date"]
    ).date()


def main():
    fio_token = os.environ["FIO_TOKEN"]
    fio_client = FioBank(fio_token)

    firefly_url = os.environ["FIREFLY_URL"]
    firefly_token = os.environ["FIREFLY_TOKEN"]
    firefly_client = FireflyClient(firefly_url, firefly_token)

    last_sync_date = fetch_last_transaction_date(firefly_client)
    account = fio_client.info()

    transactions = [
        Transaction.from_fio_data(account, item, firefly_client)
        for item in fetch_transactions(fio_client, last_sync_date)
    ]

    store_transactions(firefly_client, transactions)


if __name__ == "__main__":
    main()
