#!/usr/bin/env bash
# bill_subscription.sh — start billing only when the Python file matches OpenBao.
#
# Usage:
#   bill_subscription.sh [--customer CODE] <setup|charge|self-test>
#
# Exit codes: 0 = success, 2 = usage or checksum mismatch
#
# Requires: bash 3.2+, shasum, python3, the OpenBao billing login

set -euo pipefail

readonly PROG="$(basename "$0")"
readonly ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly SOURCE="${ROOT}/bin/bill_subscription.py"
readonly BILLING_ENV="${HOME}/.config/lumynex/billing.env"
readonly OPENBAO_PATH="secret/lumynex/billing/cl1/processor"

die() {
  echo "error: $*" >&2
  exit 2
}

find_repo_file() {
  local marker dir
  marker="$1"
  dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  while [[ "${dir}" != "/" ]]; do
    if [[ -f "${dir}/${marker}" ]]; then
      printf '%s\n' "${dir}/${marker}"
      return 0
    fi
    dir="$(dirname "${dir}")"
  done
  return 1
}

GETTER="$(find_repo_file "lumynex_iaas/scripts/openbao-get")" || die "not found: lumynex_iaas/scripts/openbao-get"

command -v shasum >/dev/null 2>&1 || die "shasum not found"
command -v python3 >/dev/null 2>&1 || die "python3 not found"
[[ -f "${SOURCE}" ]] || die "not found: ${SOURCE}"
[[ -f "${GETTER}" ]] || die "not found: ${GETTER}"
[[ -f "${BILLING_ENV}" ]] || die "not found: ${BILLING_ENV}"

actual="$(shasum -a 256 "${SOURCE}" | awk 'NR==1 { print $1 }')"
expected="$(
  OPENBAO_ENV_FILE="${BILLING_ENV}" "${GETTER}" "${OPENBAO_PATH}" source_sha256
)"
[[ "${expected}" =~ ^[0-9a-f]{64}$ ]] || die "OpenBao source checksum is missing or malformed"
if [[ "${actual}" != "${expected}" ]]; then
  die "bill_subscription.py checksum does not match OpenBao"
fi

exec python3 "${SOURCE}" "$@"
