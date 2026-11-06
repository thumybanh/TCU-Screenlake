#!/usr/bin/env bash
# Researcher tool — pre-reserve a code for a participant before they sign up.
#
# Usage:
#   ./pre-reserve.sh <email>                   # real TCU participant (code in 1000-9999)
#   ./pre-reserve.sh --test <email>            # researcher mock account (code in 0001-0999)
#   ./pre-reserve.sh --list                    # show all pre-reservations
#   ./pre-reserve.sh --batch <file.txt>        # one email per line, all TCU
#   ./pre-reserve.sh --batch-test <file.txt>   # one email per line, all TEST
#
# Example:
#   ./pre-reserve.sh jessie.wang@tcu.edu       # -> TCU-4821
#   ./pre-reserve.sh --test me@example.com     # -> TEST-0042
#
# Idempotent: running for the same email returns the same code (including role).

set -euo pipefail

FUNCTION_NAME="screenlake-assign-tcu"
REGION="us-east-2"

invoke_lambda() {
    local payload="$1"
    local tmpfile
    tmpfile=$(mktemp)
    trap "rm -f $tmpfile" RETURN

    aws lambda invoke \
        --function-name "$FUNCTION_NAME" \
        --payload "$payload" \
        --cli-binary-format raw-in-base64-out \
        --region "$REGION" \
        "$tmpfile" >/dev/null

    cat "$tmpfile"
}

reserve_one() {
    local email="$1"
    local role="${2:-TCU}"
    local response
    response=$(invoke_lambda "{\"action\":\"pre_reserve\",\"email\":\"${email}\",\"role\":\"${role}\"}")

    local code assigned_role
    if command -v jq >/dev/null 2>&1; then
        code=$(echo "$response" | jq -r .tcu_code)
        assigned_role=$(echo "$response" | jq -r .role)
    else
        code=$(echo "$response" | sed -n 's/.*"tcu_code": *"\([^"]*\)".*/\1/p')
        assigned_role=$(echo "$response" | sed -n 's/.*"role": *"\([^"]*\)".*/\1/p')
    fi

    if [ -z "${code:-}" ] || [ "$code" = "null" ]; then
        echo "ERROR for ${email}:" >&2
        echo "$response" >&2
        return 1
    fi

    echo "  ${email}  ->  ${assigned_role}-${code}"
}

list_all() {
    local response
    response=$(invoke_lambda '{"action":"list_pre_reservations"}')

    if command -v jq >/dev/null 2>&1; then
        echo ""
        echo "  email                                         role    code    claimed"
        echo "  --------------------------------------------  ------  ------  -------"
        echo "$response" | jq -r '.reservations[] | "  \(.email | tostring + "                                              " | .[0:44])  \(.role | . + "    " | .[0:4])    \(.tcu_code)    \(.claimed)"'
        echo ""
        echo "  Total: $(echo "$response" | jq '.reservations | length')"
    else
        echo "$response"
    fi
}

if [ $# -lt 1 ]; then
    cat >&2 <<EOF
Usage: $0 <email>                     # TCU participant
       $0 --test <email>              # TEST researcher mock
       $0 --list
       $0 --batch <file.txt>          # all TCU
       $0 --batch-test <file.txt>     # all TEST

Example:
   $0 jessie.wang@tcu.edu             # -> TCU-4821
   $0 --test myemail@example.com      # -> TEST-0042
EOF
    exit 1
fi

case "$1" in
    --list)
        list_all
        ;;
    --test)
        if [ $# -lt 2 ]; then
            echo "ERROR: --test requires an email argument" >&2
            exit 1
        fi
        reserve_one "$2" "TEST"
        ;;
    --batch)
        if [ $# -lt 2 ] || [ ! -f "$2" ]; then
            echo "ERROR: --batch requires a file path with one email per line" >&2
            exit 1
        fi
        echo "Reserving TCU codes for emails in $2..."
        while IFS= read -r line || [ -n "$line" ]; do
            line=$(echo "$line" | tr -d '[:space:]')
            [ -z "$line" ] && continue
            [[ "$line" == \#* ]] && continue
            reserve_one "$line" "TCU"
        done < "$2"
        ;;
    --batch-test)
        if [ $# -lt 2 ] || [ ! -f "$2" ]; then
            echo "ERROR: --batch-test requires a file path with one email per line" >&2
            exit 1
        fi
        echo "Reserving TEST codes for emails in $2..."
        while IFS= read -r line || [ -n "$line" ]; do
            line=$(echo "$line" | tr -d '[:space:]')
            [ -z "$line" ] && continue
            [[ "$line" == \#* ]] && continue
            reserve_one "$line" "TEST"
        done < "$2"
        ;;
    *)
        reserve_one "$1" "TCU"
        ;;
esac
