# lumynex_saas

This repo is the Lumynex application. Database definitions stay in `lumynex_daas`. The billing job is the first program here.

## What the billing job does

`bin/bill_subscription.sh` charges one subscription term through the lab processor stand-in and writes the acknowledgement into `organization.charge` in database `lumynex`. The table definition and the seed stay in `lumynex_daas` at `ddl/organization/01_customer.sql` and `ddl/organization/02_customer_seed.sql`.

The job reads the restricted key from OpenBao at `secret/lumynex/billing/cl1/processor`. The key is not in the source. It sends the payment method whose `revoked_at` is null. The amount is the term's `subscription_amount`, and the period date is the term start. An open trial and an open suspension are skipped. A second run for the same term and period returns the stored charge id. The billing login is the OpenBao token in `~/.config/lumynex/billing.env`. That login cannot read the other Lumynex secrets, and it cannot write the checksum.

## Checksum

OpenBao holds `source_sha256`, the SHA-256 of `bin/bill_subscription.py`, beside the restricted key. The shell launcher compares the file to that checksum and starts Python only when they match. The Python program checks again before it charges. After an intentional edit, `bin/seal_bill_subscription.sh` stores the new checksum with the admin OpenBao login.

A changed file that was not sealed does not run. Someone who can delete the check, skip the launcher, and already holds the billing login can still run a modified file. The admin token in `~/.config/lumynex/openbao.env` can seal a modified file. The charge still requires the restricted key.

## Run it

Run the launcher from anywhere. It finds `lumynex_iaas` by walking up from this repo. The charge commands below replay Northwind when that term has already been charged.

```bash
bin/bill_subscription.sh --customer northwind charge
bin/bill_subscription.sh charge
bin/seal_bill_subscription.sh
```
