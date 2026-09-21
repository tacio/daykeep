#!/bin/bash
# usage: test-track.sh ./binary  -- drives the interactive tracker over a pipe.
# stdin is not a tty here, so raw mode is skipped and keystrokes are read one at
# a time.  Times are made deterministic by stamping, then editing to a fixed
# value.  Keystrokes are assembled as escape-code TEXT (\n, \x7f) and expanded
# once by `printf %b`, so no real newline is lost to command substitution.
cd "$(dirname "$0")" || exit 1
bin=${1:-./timekeep}
fail=0

# stamp, then set the just-stamped entry to $1 via the edit line editor
# (5 backspaces clear the pre-filled HH:MM, then type the value, then Enter)
S() { printf '\\ne\\x7f\\x7f\\x7f\\x7f\\x7f%s\\n' "$1"; }

check() { # name, keystroke-text, expected last screen (entries+total+msg)
	local name=$1 keys=$2 want=${3%$'\n'} got
	# drop the inline "edit:" echo line (a real tty overwrites it via the screen
	# clear, but over a pipe it stays); keep only the last rendered screen.
	got=$(printf '%b' "$keys" | "$bin" | grep -av '^edit:' \
		| awk '/^\[enter\]/{last=blk; blk=""; next} {blk=blk $0 "\n"} END{printf "%s", last}')
	if [ "$got" == "$want" ]; then echo "ok   $name"; else echo "FAIL $name: got [$got] want [$want]"; fail=1; fi
}

check two-pairs "$(S 09:00)$(S 10:30)$(S 11:00)$(S 11:45)q" \
	$' 1  09:00 - 10:30   1h 30m\n 2  11:00 - 11:45   0h 45m\ntotal 2h 15m\n'

check open-start "$(S 09:00)q" \
	$' 1  09:00 - ...     (running)\ntotal 0h 0m\n'

check x-open "$(S 09:00)xq" $'total 0h 0m\n'
check x-end  "$(S 09:00)$(S 10:00)xq" \
	$' 1  09:00 - ...     (running)\ntotal 0h 0m\n'
check x-empty 'xxq' $'total 0h 0m\n'

check midnight "$(S 23:30)$(S 00:15)q" \
	$' 1  23:30 - 00:15   0h 45m\ntotal 0h 45m\n'

# invalid edit keeps the previous value and reports it
check bad-edit "$(S 09:00)$(S 10:00)e\\x7f\\x7f\\x7f\\x7f\\x7f25:00\\nq" \
	$' 1  09:00 - 10:00   1h 0m\ntotal 1h 0m\ninvalid time \'25:00\' (want HH:MM), kept 10:00\n'

# --- live check: a bare stamp equals the wall clock (retry once on rollover) ---
live() {
	local want got
	want=$(date +%H:%M)
	got=$(printf '\nq' | "$bin" | awk '/running/{print $2}')
	[ "$got" == "$want" ]
}
if live || live; then echo "ok   live-clock"; else echo "FAIL live-clock"; fail=1; fi

exit $fail
