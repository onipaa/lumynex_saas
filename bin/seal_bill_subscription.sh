#!/usr/bin/env bash
# seal_bill_subscription.sh — store the billing program checksum in OpenBao.
#
# Usage:
#   seal_bill_subscription.sh
#
# Exit codes: 0 = stored, 2 = usage or OpenBao error
#
# Requires: bash 3.2+, shasum, bao, the admin OpenBao login.
# The billing login cannot write this field.

set -euo pipefail

readonly PROG="$(basename "$0")"
readonly ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly SOURCE="${ROOT}/bin/bill_subscription.py"
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

login="$(find_repo_file "lumynex_iaas/scripts/openbao-login")" || die "not found: lumynex_iaas/scripts/openbao-login"
readonly IAAS="$(cd "$(dirname "${login}")/.." && pwd)"

command -v shasum >/dev/null 2>&1 || die "shasum not found"
[[ -f "${SOURCE}" ]] || die "not found: ${SOURCE}"
[[ -x "${IAAS}/scripts/openbao-login" ]] || die "not found: ${IAAS}/scripts/openbao-login"

digest="$(shasum -a 256 "${SOURCE}" | awk 'NR==1 { print $1 }')"
[[ "${digest}" =~ ^[0-9a-f]{64}$ ]] || die "checksum was not a SHA-256 hex digest"

(
  cd "${IAAS}"
  # shellcheck disable=SC1090
  eval "$(./scripts/openbao-login)"
  bao kv patch "${OPENBAO_PATH}" "source_sha256=${digest}" >/dev/null
)

echo "stored source_sha256 for bin/bill_subscription.py"
