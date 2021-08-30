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
from schwifty import IBAN


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
    def from_fio_data(cls, account: dict, transaction: dict):
        try:
            other_account_iban = str(
                IBAN.generate(
                    "CZ",
                    transaction["bank_code"],
                    transaction["account_number"].replace("-", ""),
                )
            )
        except ValueError:
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
                account["iban"]
                if type == TransactionType.withdrawal
                else other_account_iban
            ),
            destination_id=find_account_id_by_iban(
                account["iban"]
                if type == TransactionType.deposit
                else other_account_iban
            ),
        )

        if result.type == TransactionType.withdrawal:
            result.destination_name = transaction["account_name"]

        if result.type == TransactionType.deposit:
            result.source_name = transaction["account_name"]

        return result


@lru_cache(maxsize=1)
def fetch_accounts():
    url = os.environ.get("FIREFLY_URL")
    token = os.environ.get("FIREFLY_TOKEN")

    response = requests.get(
        f"{url}/api/v1/accounts",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )

    try:
        response.raise_for_status()
    except:
        pprint(response.json())
        raise

    return response.json()["data"]


def find_account_id_by_iban(iban: Optional[str]) -> Optional[str]:
    if iban is not None:
        for account in fetch_accounts():
            if account["attributes"].get("iban") == iban:
                return account["id"]


def fetch_transactions(client: FioBank):
    to_date = datetime.now(ZoneInfo(key="Europe/Prague"))
    from_date = to_date - timedelta(days=3000)
    return client.period(from_date, to_date)


def store_transactions(transactions: List[Transaction]) -> None:
    url = os.environ.get("FIREFLY_URL")
    token = os.environ.get("FIREFLY_TOKEN")

    for transaction in transactions:
        response = requests.post(
            f"{url}/api/v1/transactions",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
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


def main():
    token = os.environ.get("FIO_TOKEN")
    client = FioBank(token)
    account = client.info()
    transactions = [
        Transaction.from_fio_data(account, item) for item in fetch_transactions(client)
    ]
    store_transactions(transactions)


if __name__ == "__main__":
    main()
