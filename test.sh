#!/bin/bash
# usage: test.sh ./binary  -- sum mode: entries come from argv, totals to stdout
bin=${1:-./timekeep}
fail=0

check() { # name, expected full output, args...
	local name=$1 want=$2; shift 2
	local got; got=$("$bin" "$@")
	if [ "$got" == "$want" ]; then echo "ok   $name"; else echo "FAIL $name: got [$got] want [$want]"; fail=1; fi
}

# one big argument (as from "$(cat log)"), newline separated
check example $'1h 49m\n2h 8m\n3h 22m\n4h 45m\n6h 39m' \
	$'11:46 - 13:35\n14:26 - 14:45\n15:15 - 16:29\n16:40 - 18:03\n18:36 - 20:30'
check wrap-in   '1h 28m'            '23:24 - 00:52'
check wrap-btw  $'1h 30m\n3h 0m'    $'22:00 - 23:30\n00:15 - 01:45'
check no-nl     '0h 45m'            '10:00 - 10:45'
check crlf      $'1h 0m\n1h 30m'    $'10:00 - 11:00\r\n12:00 - 12:30\r'
check blanks    $'1h 0m\n1h 30m'    $'\n\n10:00 - 11:00\n\n   \n12:00-12:30  '
check empty     ''                  ''
check long-day  '23h 59m'           '00:00 - 23:59'

# entries split across several arguments (numbers/dashes as separate words)
check split-words $'1h 30m\n2h 15m'  09:00 - 10:30 11:00 - 11:45
check split-pair  $'1h 0m\n1h 30m'   '10:00-11:00' '12:00-12:30'
# a partial entry at the end of one arg continues into the next
check across-args '1h 0m'            '10:00 -' '11:00'

exit $fail
