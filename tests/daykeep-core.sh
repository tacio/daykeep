#!/bin/bash
# usage: tests/daykeep-core.sh ./daykeep  -- time library and CLI skeleton
# The clock is pinned with DAYKEEP_NOW and the zone with a POSIX TZ string,
# so the results don't depend on the machine's clock or tzdata.
bin=${1:-./daykeep}
fail=0

export DAYKEEP_NOW=14301.4564      # 2026-09-22 10:57:13 UTC, today = 14301
export TZ='<-03>3'                 # fixed UTC-3, no DST
unset POSIXLY_CORRECT

ok()  { echo "ok   $1"; }
bad() { echo "FAIL $1: $2"; fail=1; }

check() { # name, expected stdout, args...
	local name=$1 want=$2; shift 2
	local got; got=$("$bin" "$@" 2>&1)
	if [ "$got" == "$want" ]; then ok "$name"; else bad "$name" "got [$got] want [$want]"; fi
}

status() { # name, expected exit status, args...
	local name=$1 want=$2; shift 2
	"$bin" "$@" >/dev/null 2>&1
	local rc=$?
	if [ $rc -eq "$want" ]; then ok "$name"; else bad "$name" "exit $rc want $want"; fi
}

# clock string of day $1 plus $2 seconds in the local zone, from date(1)
day() { date -d @$(( ($1 + 6417) * 86400 + ${2:-0} )) '+%F %T %z'; }

# --- left completion: most recent day <= today ending in the digits --------
check today-full   "$(day 14301)" -c 14301
check tail-3       "$(day 14301)" -c 301
check tail-1       "$(day 14299)" -c 9
check tail-past    "$(day 13302)" -c 302
check tail-zeros   "$(day 10301)" -c 0301
check tail-today   "$(day 14301)" -c 1
check full-future  "$(day 15000)" -c 15000
check full-past    "$(day 7)"     -c 00007

# --- '+': first day >= today ending in the digits --------------------------
check plus-1       "$(day 14309)" -c +9
check plus-today   "$(day 14301)" -c +301
check plus-next    "$(day 15300)" -c +300
check plus-frac    "$(day 14301 43200)" -c +.5

# --- right digits: omitted ones are zeros; rounding to the second ----------
check frac-only    "$(day 14301 43200)" -c .5
check frac-pad     "$(day 14301 43200)" -c .5000
check frac-long    "$(day 14301 39433)" -c 14301.45640000001
check day-dot      "$(day 14299)"       -c 9.
check frac-step    "$(day 14301 9)"     -c .0001

# --- HH:MM: wall-clock time today, local unless -u ------------------------
check hm-local     14301.5417 -c 10:00           # 13:00 UTC
check hm-seconds   14301.5417 -c 10:00:00
check hm-utc       14301.4167 -u -c 10:00
check hm-utc-late  14301.9993 -u -c 23:59
# at 22:12 local the UTC day is already 14302, but "today" is the local date
DAYKEEP_NOW=14302.0500 check hm-local-date 14301.5417 -c 10:00

# --- ISO dates --------------------------------------------------------------
check iso-date     14301.1250 -c 2026-09-22                 # local midnight
check iso-utc      14301.0000 -u -c 2026-09-22
check iso-time     14301.4167 -c 2026-09-22T07:00
check iso-zulu     14301.4167 -c 2026-09-22T10:00Z
check iso-offset   14301.4167 -c '2026-09-22 12:00:00 +0200'
check iso-colon    14301.4167 -c '2026-09-22 12:00 +02:00'
check epoch        00000.0000 -c 1987-07-28T00:00Z
check leap-day     12634.0000 -u -c 2022-02-28
check leap-feb29   13365.0000 -u -c 2024-02-29

# --- round trips ------------------------------------------------------------
for v in 14301.4564 14301.0000 14301.9999 13000.1234 10000.0001 09999.1234 00000.0001; do
	back=$("$bin" -c "$("$bin" -c "$v")")
	if [ "$back" == "$v" ]; then ok "round-trip $v"; else bad "round-trip $v" "got [$back]"; fi
	back=$("$bin" -u -c "$("$bin" -u -c "$v")")
	if [ "$back" == "$v" ]; then ok "round-trip-utc $v"; else bad "round-trip-utc $v" "got [$back]"; fi
done

# --- --now ------------------------------------------------------------------
check now          14301.4564 --now
DAYKEEP_NOW=@1790035200 check now-at 14301.0000 --now
DAYKEEP_NOW=@1790035243 check now-round-up   14301.0005 --now   # 43 s = 4.98 steps
DAYKEEP_NOW=@1790035204 check now-round-down 14301.0000 --now   # 4 s < half a step
# the real clock agrees with date(1) to within one step
real=$(env -u DAYKEEP_NOW "$bin" --now)
want=$(echo "scale=4; ($(date -u +%s) - 554428800) / 86400" | bc)
diff=$(echo "d = ($real - $want) * 10000; if (d < 0) d = -d; d <= 2" | bc)
if [ "$diff" == 1 ]; then ok now-real; else bad now-real "got $real want ~$want"; fi

# --- invalid input and usage ------------------------------------------------
check bad-value   "daykeep: invalid value '.x'" -c .x
check bad-keeps-going $'daykeep: invalid value \'x\'\n'"$(day 14299)" -c x 9
status bad-status      1 -c 9 .x
status bad-hm          1 -c 24:00
status bad-hm-min      1 -c 10:60
status bad-date        1 -c 2026-02-30
status bad-date-feb29  1 -c 2025-02-29
status bad-plus-plus   1 -c ++9
status bad-dot         1 -c .
status bad-empty       1 -c ''
status bad-long-day    1 -c 1234567890
status no-args         2
status bad-option      2 --bogus
status now-and-convert 2 --now -c
status now-operand     2 --now 9
status convert-empty   2 -c
status sum-not-yet     2 .25 - .5
DAYKEEP_NOW=garbage status bad-now-env 2 --now
status help            0 --help
status version         0 --version

# getopt permutation: options after operands, and -- ending them
check permute      "$(day 14299)" 9 -c
check dashdash     14301.4167 -c -u -- 10:00
POSIXLY_CORRECT=1 status posixly-correct 2 9 -c

# write errors on stdout are reported
if [ -w /dev/full ]; then
	for args in --now --help --version '-c 9'; do
		# shellcheck disable=SC2086
		err=$("$bin" $args 2>&1 >/dev/full); rc=$?
		if [ $rc -eq 2 ] && [[ $err == *"write error"* ]]; then ok "full $args"
		else bad "full $args" "exit $rc [$err]"; fi
	done
fi

exit $fail
