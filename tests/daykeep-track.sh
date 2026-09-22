#!/bin/bash
# usage: tests/daykeep-track.sh ./daykeep  -- the interactive tracker
# Keystrokes go in over a pipe, so stdin is not a tty: raw mode is skipped
# and keys are read one at a time.  The clock is pinned with DAYKEEP_NOW, so
# every Enter stamps 14301.4564; stamps are then set with the edit key.
# Keystrokes are written as escape-code TEXT (\n, \x15) and expanded once by
# printf %b, so no newline is lost to command substitution.
bin=$(realpath "${1:-./daykeep}")
fail=0

export DAYKEEP_NOW=14301.4564      # 2026-09-22 10:57:13 UTC = 07:57:13 local
export TZ='<-03>3'                 # fixed UTC-3, no DST
unset POSIXLY_CORRECT

tmp=$(mktemp -d) && trap 'rm -rf "$tmp"' EXIT || exit 1

ok()  { echo "ok   $1"; }
bad() { echo "FAIL $1: $2"; fail=1; }

# stamp, then set the new stamp to $1: e, ^U clears the prefilled value
S() { printf '\\ne\\x15%s\\n' "$1"; }

# the last screen, without the help line and the "edit:" echo lines (a tty
# would clear them away, but over a pipe they stay)
screen() { # keystroke-text, args...
	local keys=$1; shift
	printf '%b' "$keys" | "$bin" --track "$@" 2>/dev/null | grep -av '^edit:' \
		| awk '/^\[enter\]/{last=blk; blk=""; next} {blk=blk $0 "\n"} END{printf "%s", last}'
}

check() { # name, keystroke-text, expected last screen, args...
	local name=$1 keys=$2 want=${3%$'\n'} got; shift 3
	got=$(screen "$keys" "$@")
	if [ "$got" == "$want" ]; then ok "$name"; else bad "$name" "got [$got] want [$want]"; fi
}

file() { # name, file, expected content (exact, trailing newline included)
	local got; got=$(cat "$2"; echo .); got=${got%.}
	if [ "$got" == "$3" ]; then ok "$1"; else bad "$1" "got [$got] want [$3]"; fi
}

# --- keys ---------------------------------------------------------------------
check stamp  '\nq' \
	$' 1  14301.4564 - ...          (running)\ntotal .0000'
check two-pairs "$(S .375)$(S .4375)$(S .5)$(S .55)q" \
	$' 1  14301.3750 - 14301.4375   .0625\n 2  14301.5000 - 14301.5500   .0500\ntotal .1125'
check hm "$(S 09:00)$(S 10:30)$(S 11:00)$(S 11:45)q" \
	$' 1  14301.5000 - 14301.5625   1h 30m\n 2  14301.5833 - 14301.6146   0h 45m\ntotal 2h 15m' --hm
check minutes "$(S .375)$(S .4375)q" \
	$' 1  14301.3750 - 14301.4375   90\ntotal 90' --format=minutes
check utc "$(S 09:00)q" \
	$' 1  14301.3750 - ...          (running)\ntotal .0000' -u

check x-open  "$(S .3)xq"          $'total .0000'
check x-end   "$(S .3)$(S .4)xq"   $' 1  14301.3000 - ...          (running)\ntotal .0000'
check x-empty 'xxq'                $'total .0000'
check e-empty 'eq'                 $'total .0000'
check quit-eof "$(S .3)"           $' 1  14301.3000 - ...          (running)\ntotal .0000'
check quit-ctrl-c "$(S .3)\\x03\\n" $' 1  14301.3000 - ...          (running)\ntotal .0000'
check quit-ctrl-d "$(S .3)\\x04\\n" $' 1  14301.3000 - ...          (running)\ntotal .0000'
check other-keys "$(S .3)zZ?q"      $' 1  14301.3000 - ...          (running)\ntotal .0000'

# --- edits: the first stamp is completed from today, later ones from the
# stamp above them, like a range END -----------------------------------------------
check rollover "$(S .9)$(S .1)q"   $' 1  14301.9000 - 14302.1000   .2000\ntotal .2000'
check midnight "$(S 23:30)$(S 00:15)q" \
	$' 1  14302.1042 - 14302.1354   .0313\ntotal .0313'
check next-start "$(S .9)$(S .95)$(S .05)q" \
	$' 1  14301.9000 - 14301.9500   .0500\n 2  14302.0500 - ...          (running)\ntotal .0500'
check first-back "$(S 299.5)q"     $' 1  14299.5000 - ...          (running)\ntotal .0000'
check plus     "$(S +9.5)q"        $' 1  14309.5000 - ...          (running)\ntotal .0000'
check backspace "\\ne\\x7f\\x7f\\x7f\\x7f3\\nq" \
	$' 1  14301.3000 - ...          (running)\ntotal .0000'
check bad-edit "$(S .3)$(S .4)e\\x15x\\nq" \
	$' 1  14301.3000 - 14301.4000   .1000\ntotal .1000\ninvalid stamp \'x\', kept 14301.4000'
check before   "$(S .3)$(S .4)e\\x15""14301.2\\nq" \
	$' 1  14301.3000 - 14301.4000   .1000\ntotal .1000\n\'14301.2\' is before the stamp above it, kept 14301.4000'
check cancel   "$(S .3)e\\x15.5\\x1bq" \
	$' 1  14301.3000 - ...          (running)\ntotal .0000\nedit cancelled'
check cancel-c "$(S .3)e\\x15.5\\x03q" \
	$' 1  14301.3000 - ...          (running)\ntotal .0000\nedit cancelled'

# --- the log (-a) ---------------------------------------------------------------------
got=$(printf 'sq' | "$bin" -t | grep -a 'no log')
if [ "$got" == "no log to save to (start with -a LOG)" ]; then ok s-no-log-msg; else bad s-no-log-msg "[$got]"; fi

cd "$tmp" || exit 1
printf '%b' "$(S .375)$(S .4375)\\nq" | "$bin" -t -a log >/dev/null
file log-write log $'14301.3750 - 14301.4375\n14301.4564 -\n'
got=$("$bin" -s -f log); [ "$got" == .0625 ] && ok log-reads-back || bad log-reads-back "[$got]"

# the open entry at the end is resumed and closed; older lines stay
check resume "$(S .55)q" \
	$' 1  14301.4564 - 14301.5500   .0936\ntotal .0936' -a log
file resume-log log $'14301.3750 - 14301.4375\n14301.4564 - 14301.5500\n'
got=$(printf 'q' | "$bin" -t -a log | grep -ac resumed)
[ "$got" == 0 ] && ok no-resume-closed || bad no-resume-closed "resumed a closed entry"

printf '# notes\n.6 -\n' > log2          # any stamp form resumes
check resume-short "$(S .7)q" \
	$' 1  14301.6000 - 14301.7000   .1000\ntotal .1000' -a log2
file resume-short-log log2 $'# notes\n14301.6000 - 14301.7000\n'
got=$(printf 'q' | "$bin" -t -a log2 >/dev/null; printf '.8 -' >> log2; printf 'q' | "$bin" -t -a log2 | grep -a resumed)
[ "$got" == "resumed the open entry in log2" ] && ok resume-msg || bad resume-msg "[$got]"

# x on the resumed entry takes it out of the log
printf 'xq' | "$bin" -t -a log2 >/dev/null
file x-resumed log2 $'# notes\n14301.6000 - 14301.7000\n'

# edits and removals rewrite the session's lines; a missing final newline is added
printf '14301.1 - 14301.2' > log3
printf '%b' "$(S .3)$(S .4)$(S .5)$(S .6)xxx$(S .7)q" | "$bin" -t -a log3 >/dev/null
file rewrite log3 $'14301.1 - 14301.2\n14301.3000 - 14301.7000\n'
printf 'xxq' | "$bin" -t -a log3 >/dev/null     # nothing open: x has nothing to remove
file keep-old log3 $'14301.1 - 14301.2\n14301.3000 - 14301.7000\n'
printf 'q' | "$bin" -t -a new >/dev/null
file empty-session new ''

got=$(printf '%b' "$(S .3)sq" | "$bin" -t -a log4 | grep -a '^saved')
[ "$got" == "saved log4" ] && ok s-saved || bad s-saved "[$got]"

# --- signals: the log is current, the terminal state restored, and the exit
# status is the signal's ---------------------------------------------------------------
sig() { # name, signal
	rm -f fifo slog; mkfifo fifo
	"$bin" -t -a slog <fifo >/dev/null 2>&1 & local pid=$!
	exec 3>fifo
	printf '%b' "$(S .3)$(S .4)$(S .5)" >&3
	local i; for i in $(seq 50); do
		[ "$(cat slog 2>/dev/null)" == $'14301.3000 - 14301.4000\n14301.5000 -' ] && break
		sleep 0.1
	done
	# a second tracker on the same log is refused while the first runs
	local got; got=$("$bin" -t -a slog 2>&1 </dev/null); local rc=$?
	if [ $rc -eq 2 ] && [ "$got" == "daykeep: slog: in use by another tracker" ]; then ok "$1-lock"
	else bad "$1-lock" "exit $rc [$got]"; fi
	kill -"$2" $pid; { wait $pid; } 2>/dev/null; rc=$?
	exec 3>&-
	file "$1-log" slog $'14301.3000 - 14301.4000\n14301.5000 -\n'
	local want=$((128 + $(kill -l "$2")))
	if [ $rc -eq $want ]; then ok "$1-status"; else bad "$1-status" "exit $rc want $want"; fi
}
sig term TERM
sig hup HUP

# --- errors ------------------------------------------------------------------------------
err() { # name, expected stderr, expected status, args...
	local name=$1 want=$2 wrc=$3; shift 3
	local got; got=$("$bin" "$@" 2>&1 >/dev/null </dev/null); local rc=$?
	if [ "$got" == "$want" ] && [ $rc -eq "$wrc" ]; then ok "$name"
	else bad "$name" "exit $rc [$got], want exit $wrc [$want]"; fi
}
try="Try 'daykeep --help' for more information."
err operand   "daykeep: extra operand '.3'"$'\n'"$try" 2 --track .3
err a-alone   "daykeep: -a works only with --track"$'\n'"$try" 2 -a log .1 - .2
err f-track   "daykeep: -f works only when summing ranges"$'\n'"$try" 2 -t -f log
err exclusive "daykeep: --now, --convert and --track are exclusive"$'\n'"$try" 2 -t --now
err no-dir    "daykeep: nodir/log: No such file or directory" 2 -t -a nodir/log
if [ -w /dev/full ]; then
	got=$(printf 'q' | "$bin" -t 2>&1 >/dev/full); rc=$?
	if [ $rc -eq 2 ] && [[ $got == *"write error"* ]]; then ok full-track
	else bad full-track "exit $rc [$got]"; fi
	got=$(printf '\nq' | "$bin" -t -a /dev/full 2>&1 >/dev/null); rc=$?
	if [ $rc -eq 2 ] && [[ $got == "daykeep: /dev/full: No space left on device" ]]; then ok full-log
	else bad full-log "exit $rc [$got]"; fi
fi

# --- live check: Enter stamps the real clock (within a step or two) ----------------
unset DAYKEEP_NOW
got=$(printf '\nq' | "$bin" -t | awk '/running/{print $2}')
want=$("$bin" --now)
if awk -v a="$got" -v b="$want" 'BEGIN{d=a-b; exit !(a != "" && d < .0003 && d > -.0003)}'
then ok live-clock; else bad live-clock "got [$got] want about [$want]"; fi

exit $fail
