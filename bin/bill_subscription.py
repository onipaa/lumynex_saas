#!/usr/bin/env python3
"""Bill one subscription term through the lab processor stand-in.

The restricted key is read from OpenBao at secret/lumynex/billing/cl1/processor.
This file does not contain it. The charge amount is the term's subscription_amount.
OpenBao also holds the SHA-256 of this file. A mismatch stops the run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import secrets
import string
import subprocess
import sys
from pathlib import Path


OPENBAO_PATH = "secret/lumynex/billing/cl1/processor"
OPENBAO_FIELD = "restricted_key"
SOURCE_SHA_FIELD = "source_sha256"
LEADER = "root@192.168.5.84"
CHARGE_ALPHABET = string.ascii_letters + string.digits
CUSTOMER_CODE = re.compile(r"^[a-z0-9]+$")
CHARGE_CODE = re.compile(r"^ch_[A-Za-z0-9]{24}$")
TOKEN_CODE = re.compile(r"^[A-Za-z0-9_]+$")

log = logging.getLogger("bill_subscription")


def billing_env_file() -> Path:
    return Path.home() / ".config" / "lumynex" / "billing.env"


def openbao_get() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "lumynex_iaas" / "scripts" / "openbao-get"
        if candidate.is_file():
            return candidate
    raise SystemExit("error: OpenBao reader is missing. Expected lumynex_iaas/scripts/openbao-get above this repo.")


def read_openbao_field(field: str) -> str:
    """Return one billing-secret field. The value is not logged."""
    env_file = billing_env_file()
    getter = openbao_get()
    if not env_file.is_file():
        raise SystemExit(f"error: billing login is missing at {env_file}")
    if not getter.is_file():
        raise SystemExit(f"error: OpenBao reader is missing at {getter}")
    completed = subprocess.run(
        [str(getter), OPENBAO_PATH, field],
        env={
            **{k: v for k, v in os.environ.items() if k != "BAO_TOKEN"},
            "OPENBAO_ENV_FILE": str(env_file),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "OpenBao read failed"
        raise SystemExit(f"error: {detail}")
    value = completed.stdout.strip()
    if not value:
        raise SystemExit(f"error: OpenBao returned an empty {field}")
    return value


def read_restricted_key() -> str:
    key = read_openbao_field(OPENBAO_FIELD)
    log.info("read restricted key from %s", OPENBAO_PATH)
    return key


def source_digest() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def require_source_match() -> None:
    """Stop when this file's bytes differ from the checksum OpenBao holds."""
    expected = read_openbao_field(SOURCE_SHA_FIELD)
    actual = source_digest()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise SystemExit("error: OpenBao source checksum is missing or malformed")
    if not secrets.compare_digest(expected, actual):
        raise SystemExit("error: bill_subscription.py checksum does not match OpenBao")
    log.info("source checksum matches OpenBao")


def processor_setup_ids() -> tuple[str, str, str]:
    """Mint lab ids shaped like Stripe customer, payment method, and mandate ids."""
    alphabet = CHARGE_ALPHABET
    customer = "cus_" + "".join(secrets.choice(alphabet) for _ in range(14))
    method = "pm_1" + "".join(secrets.choice(alphabet) for _ in range(23))
    mandate = "mandate_1" + "".join(secrets.choice(alphabet) for _ in range(23))
    return customer, method, mandate


def processor_charge(restricted_key: str, presented_key: str, payment_method_code: str) -> str:
    """Return a charge id when the key matches and the method token is shaped."""
    if not secrets.compare_digest(restricted_key, presented_key):
        raise SystemExit("error: processor rejected the restricted key")
    if not payment_method_code.startswith("pm_") or not TOKEN_CODE.fullmatch(payment_method_code):
        raise SystemExit("error: processor rejected the payment method")
    body = "".join(secrets.choice(CHARGE_ALPHABET) for _ in range(24))
    return "ch_" + body


def idempotency_code(subscription_id: str, period_date: str) -> str:
    compact = subscription_id.replace("-", "")
    day = period_date.replace("-", "")
    if not re.fullmatch(r"[0-9a-f]{32}", compact) or not re.fullmatch(r"\d{8}", day):
        raise SystemExit("error: term id or period date is not usable as an idempotency key")
    return f"term_{compact}_{day}"


def psql(sql: str) -> str:
    remote = "runuser -u postgres -- psql -d lumynex -v ON_ERROR_STOP=1 -At -q"
    completed = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", LEADER, remote],
        input=sql,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "psql failed"
        raise SystemExit(f"error: {detail}")
    return completed.stdout.strip()


def fetch_terms(customer_code: str) -> list[dict[str, object]]:
    if customer_code and not CUSTOMER_CODE.fullmatch(customer_code):
        raise SystemExit("error: customer code must be lowercase letters and digits")
    where = ""
    if customer_code:
        where = f"where c.customer_code = '{customer_code}'"
    sql = f"""
select coalesce(json_agg(row_to_json(b)), '[]'::json)::text
from (
    select c.customer_code
        , c.customer_processor_code
        , s.subscription_id::text as subscription_id
        , s.subscription_start_date::text as charge_period_date
        , s.license_count
        , s.subscription_amount::text as charge_amount
        , (s.suspended_at is not null and s.lifted_at is null) as open_suspension
        , (s.trial_end_date is not null and s.trial_end_date >= current_date) as open_trial
        , (current_date < s.subscription_start_date) as not_started
        , (current_date > s.subscription_end_date) as expired
        , pm.payment_method_id::text as payment_method_id
        , pm.payment_method_code
    from organization.subscription s
    inner join organization.customer c
        on c.customer_id = s.customer_id
    left join organization.payment_method pm
        on pm.customer_id = c.customer_id
        and pm.revoked_at is null
    {where}
    order by c.customer_code
) b
;
"""
    payload = json.loads(psql(sql) or "[]")
    if not isinstance(payload, list):
        raise SystemExit("error: term query did not return a list")
    return payload


def skip_reason(term: dict[str, object]) -> str | None:
    if term.get("open_suspension"):
        return "open suspension"
    if term.get("open_trial"):
        return "open trial"
    if term.get("not_started"):
        return "term has not started"
    if term.get("expired"):
        return "term has expired"
    if not term.get("payment_method_code"):
        return "no live payment method"
    return None


def sql_literal(value: str, pattern: re.Pattern[str]) -> str:
    if not pattern.fullmatch(value):
        raise SystemExit("error: a charge field failed its format check before the insert")
    return "'" + value + "'"


def stored_charge(subscription_id: str, period: str) -> str:
    uuid_re = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    sql = f"""
select coalesce(
    (
        select ch.processor_charge_code
        from organization.charge ch
        where ch.subscription_id = {sql_literal(subscription_id, uuid_re)}::uuid
        and ch.charge_period_date = {sql_literal(period, date_re)}::date
    )
    , ''
)
;
"""
    return psql(sql)


def acknowledge(term: dict[str, object], charge_code: str) -> str:
    subscription_id = str(term["subscription_id"])
    payment_method_id = str(term["payment_method_id"])
    period = str(term["charge_period_date"])
    amount = str(term["charge_amount"])
    key = idempotency_code(subscription_id, period)
    uuid_re = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    amount_re = re.compile(r"^\d+\.\d{2}$")
    key_re = re.compile(r"^term_[0-9a-f]{32}_\d{8}$")
    sql = f"""
with existing as (
    select ch.processor_charge_code
    from organization.charge ch
    where ch.subscription_id = {sql_literal(subscription_id, uuid_re)}::uuid
    and ch.charge_period_date = {sql_literal(period, date_re)}::date
),
inserted as (
    insert into organization.charge (
        subscription_id
        , payment_method_id
        , charge_period_date
        , charge_amount
        , charge_idempotency_code
        , processor_charge_code
        , charged_at
    )
    select {sql_literal(subscription_id, uuid_re)}::uuid
        , {sql_literal(payment_method_id, uuid_re)}::uuid
        , {sql_literal(period, date_re)}::date
        , {sql_literal(amount, amount_re)}::numeric
        , {sql_literal(key, key_re)}
        , {sql_literal(charge_code, CHARGE_CODE)}
        , current_timestamp
    where not exists (
        select 1
        from existing
    )
    returning processor_charge_code
)
select coalesce(
    (select processor_charge_code from inserted)
    , (select processor_charge_code from existing)
)
;
"""
    return psql(sql)


def setup_customer(customer_code: str) -> int:
    terms = fetch_terms(customer_code)
    if not terms:
        raise SystemExit(f"error: no term for {customer_code}")
    term = terms[0]
    method = term.get("payment_method_code")
    customer_processor = term.get("customer_processor_code")
    if method and customer_processor:
        print(
            f"{customer_code} customer={customer_processor} payment_method={method} live=yes"
        )
        return 0
    minted = processor_setup_ids()
    print(
        f"{customer_code} has no live method. A new setup would return "
        f"customer={minted[0]} payment_method={minted[1]} mandate={minted[2]}"
    )
    print("Those ids were not stored. Card setup still belongs at the processor.")
    return 2


def charge_terms(customer_code: str, restricted_key: str) -> int:
    terms = fetch_terms(customer_code)
    if customer_code and not terms:
        raise SystemExit(f"error: no term for {customer_code}")
    exit_code = 0
    for term in terms:
        code = str(term["customer_code"])
        reason = skip_reason(term)
        if reason is not None:
            log.info("%s skipped: %s", code, reason)
            print(f"{code} skipped: {reason}")
            if customer_code:
                exit_code = 2
            continue
        already = stored_charge(str(term["subscription_id"]), str(term["charge_period_date"]))
        if already:
            state = "replayed"
            stored = already
        else:
            minted = processor_charge(
                restricted_key,
                restricted_key,
                str(term["payment_method_code"]),
            )
            stored = acknowledge(term, minted)
            state = "replayed" if stored != minted else "charged"
        log.info(
            "%s %s seats=%s amount=%s period=%s charge=%s",
            code,
            state,
            term["license_count"],
            term["charge_amount"],
            term["charge_period_date"],
            stored,
        )
        print(
            f"{code} {state} seats={term['license_count']} "
            f"amount={term['charge_amount']} period={term['charge_period_date']} "
            f"charge={stored}"
        )
    return exit_code


def self_test(restricted_key: str) -> int:
    customer, method, mandate = processor_setup_ids()
    for label, value in (
        ("customer", customer),
        ("payment_method", method),
        ("mandate", mandate),
    ):
        if not TOKEN_CODE.fullmatch(value):
            raise SystemExit(f"error: minted {label} failed the token shape check")
    print(f"setup customer={customer} payment_method={method} mandate={mandate}")
    try:
        processor_charge(restricted_key, "rk_test_wrong", method)
    except SystemExit as exc:
        if "rejected the restricted key" not in str(exc):
            raise
        print("wrong key rejected")
    else:
        raise SystemExit("error: a wrong key was accepted")
    accepted = processor_charge(restricted_key, restricted_key, method)
    if not CHARGE_CODE.fullmatch(accepted):
        raise SystemExit("error: minted charge id failed the ch_ shape check")
    print(f"restricted key accepted charge={accepted}")
    print("The sample ids above were not stored.")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bill a Lumynex subscription term in the lab.")
    parser.add_argument("--customer", default="", help="One customer code. Default: every term.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="Show the live processor ids for a customer.")
    sub.add_parser("charge", help="Charge billable terms, or replay the stored charge id.")
    sub.add_parser("self-test", help="Mint sample ids and reject a wrong restricted key.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    require_source_match()
    args = parse_args(argv)
    if args.command == "setup":
        if not args.customer:
            raise SystemExit("error: setup requires --customer")
        return setup_customer(args.customer)
    restricted_key = read_restricted_key()
    if args.command == "self-test":
        return self_test(restricted_key)
    if args.command == "charge":
        return charge_terms(args.customer, restricted_key)
    raise SystemExit("error: unknown command")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
