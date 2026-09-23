#!/bin/bash
# usage: tests/daykeep-core.sh ./daykeep  -- time library and CLI skeleton
# The clock is pinned with DAYKEEP_NOW and the zone with a POSIX TZ string,
# so the results don't depend on the machine's clock or tzdata.
#
# Copyright (C) 2026 Tacio Medeiros
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

bin=${1:-./daykeep}
fail=0

export DAYKEEP_NOW=09761.4564      # 2026-09-22 10:57:13 UTC, today = 9761
export TZ='<-03>3'                 # fixed UTC-3, no DST
unset POSIXLY_CORRECT DAYKEEP_EPOCH DAYKEEP_DAY_LENGTH

# an empty config directory, so the user's own config can't interfere
tmp=$(mktemp -d) && trap 'rm -rf "$tmp"' EXIT || exit 1
export XDG_CONFIG_HOME=$tmp/config

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
day() { date -d @$(( ($1 + 10957) * 86400 + ${2:-0} )) '+%F %T %z'; }

# --- left completion: most recent day <= today ending in the digits --------
check today-full   "$(day 9761)"  -c 9761
check tail-3       "$(day 9761)"  -c 761
check tail-1       "$(day 9759)"  -c 9
check tail-past    "$(day 8762)"  -c 762
check tail-zeros   "$(day 9061)"  -c 061
check tail-today   "$(day 9761)"  -c 1
check full-4       "$(day 761)"   -c 0761     # as many digits as today: literal
check full-future  "$(day 15000)" -c 15000
check full-past    "$(day 7)"     -c 00007

# --- '+': first day >= today ending in the digits --------------------------
check plus-1       "$(day 9769)"  -c +9
check plus-today   "$(day 9761)"  -c +761
check plus-next    "$(day 10760)" -c +760
check plus-frac    "$(day 9761 43200)" -c +.5

# --- right digits: omitted ones are zeros; rounding to the second ----------
check frac-only    "$(day 9761 43200)" -c .5
check frac-pad     "$(day 9761 43200)" -c .5000
check frac-long    "$(day 9761 39433)" -c 9761.45640000001
check day-dot      "$(day 9759)"       -c 9.
check frac-step    "$(day 9761 9)"     -c .0001

# --- HH:MM: wall-clock time today, local unless -u ------------------------
check hm-local     09761.5417 -c 10:00           # 13:00 UTC
check hm-seconds   09761.5417 -c 10:00:00
check hm-utc       09761.4167 -u -c 10:00
check hm-utc-late  09761.9993 -u -c 23:59
# at 22:12 local the UTC day is already 9762, but "today" is the local date
DAYKEEP_NOW=09762.0500 check hm-local-date 09761.5417 -c 10:00

# --- ISO dates --------------------------------------------------------------
check iso-date     09761.1250 -c 2026-09-22                 # local midnight
check iso-utc      09761.0000 -u -c 2026-09-22
check iso-time     09761.4167 -c 2026-09-22T07:00
check iso-zulu     09761.4167 -c 2026-09-22T10:00Z
check iso-offset   09761.4167 -c '2026-09-22 12:00:00 +0200'
check iso-colon    09761.4167 -c '2026-09-22 12:00 +02:00'
check epoch        00000.0000 -c 2000-01-01T00:00Z
check leap-day     08094.0000 -u -c 2022-02-28
check leap-feb29   08825.0000 -u -c 2024-02-29

# --- round trips ------------------------------------------------------------
for v in 09761.4564 09761.0000 09761.9999 13000.1234 10000.0001 09999.1234 00000.0001; do
	back=$("$bin" -c "$("$bin" -c "$v")")
	if [ "$back" == "$v" ]; then ok "round-trip $v"; else bad "round-trip $v" "got [$back]"; fi
	back=$("$bin" -u -c "$("$bin" -u -c "$v")")
	if [ "$back" == "$v" ]; then ok "round-trip-utc $v"; else bad "round-trip-utc $v" "got [$back]"; fi
done

# --- --now ------------------------------------------------------------------
check now          09761.4564 --now
DAYKEEP_NOW=@1790035200 check now-at 09761.0000 --now
DAYKEEP_NOW=@1790035243 check now-round-up   09761.0005 --now   # 43 s = 4.98 steps
DAYKEEP_NOW=@1790035204 check now-round-down 09761.0000 --now   # 4 s < half a step
# the real clock agrees with date(1) to within one step
real=$(env -u DAYKEEP_NOW "$bin" --now)
want=$(echo "scale=4; ($(date -u +%s) - 946684800) / 86400" | bc)
diff=$(echo "d = ($real - $want) * 10000; if (d < 0) d = -d; d <= 2" | bc)
if [ "$diff" == 1 ]; then ok now-real; else bad now-real "got $real want ~$want"; fi

# --- invalid input and usage ------------------------------------------------
check bad-value   "daykeep: invalid value '.x'" -c .x
check bad-keeps-going $'daykeep: invalid value \'x\'\n'"$(day 9759)" -c x 9
status bad-status      1 -c 9 .x
status bad-hm          1 -c 24:00
status bad-hm-min      1 -c 10:60
status bad-date        1 -c 2026-02-30
status bad-date-feb29  1 -c 2025-02-29
status bad-plus-plus   1 -c ++9
status bad-dot         1 -c .
status bad-empty       1 -c ''
status bad-long-day    1 -c 12345678901234567890
status bad-option      2 --bogus
status now-and-convert 2 --now -c
status now-operand     2 --now 9
status convert-empty   2 -c
status file-with-now   2 --now -f x
DAYKEEP_NOW=garbage status bad-now-env 2 --now
status help            0 --help
status version         0 --version

# getopt permutation: options after operands, and -- ending them
check permute      "$(day 9759)" 9 -c
check dashdash     09761.4167 -c -u -- 10:00
POSIXLY_CORRECT=1 status posixly-correct 1 9 -c    # -c is range text then

# --- the epoch: --epoch, then DAYKEEP_EPOCH, then the config file -----------
check epoch-option  14301.4167 --epoch=1987-07-28 -c 2026-09-22T10:00Z
check epoch-time    09761.1250 --epoch='2000-01-01 00:00 +0300' -c 2026-09-22T00:00Z
check epoch-offset  09761.0000 --epoch='2000-01-01 00:00 -0300' -c 2026-09-22   # local midnight
check epoch-zulu    09761.0000 --epoch=2000-01-01T00:00Z -c 2026-09-22T00:00Z
DAYKEEP_EPOCH=1987-07-28 check epoch-env 14301.4167 -c 2026-09-22T10:00Z
DAYKEEP_EPOCH=1987-07-28 check epoch-option-wins 09761.4167 --epoch=2000-01-01 -c 2026-09-22T10:00Z
# DAYKEEP_NOW's stamp counts from the chosen epoch
check epoch-now     09761.4564 --epoch=1987-07-28 --now
check epoch-bad     "daykeep: invalid argument 'x' for '--epoch'"$'\n'"Try 'daykeep --help' for more information." --epoch=x --now
status epoch-bad-status 2 --epoch=2000-13-01 --now
DAYKEEP_EPOCH=x check epoch-env-bad "daykeep: invalid DAYKEEP_EPOCH 'x'" --now
DAYKEEP_EPOCH=x status epoch-env-bad-status 2 --now

cfg=$XDG_CONFIG_HOME/daykeep/config
mkdir -p "${cfg%/*}"
check config-missing 09761.4167 -c 2026-09-22T10:00Z
printf '# old numbering\n\n  epoch =  1987-07-28 \n' >"$cfg"
check config-epoch   14301.4167 -c 2026-09-22T10:00Z
DAYKEEP_EPOCH=2000-01-01 check config-env-wins 09761.4167 -c 2026-09-22T10:00Z
check config-option-wins 09761.4167 --epoch=2000-01-01 -c 2026-09-22T10:00Z
printf 'epoch = 1987-07-28\nepoch = 2000-01-01 00:00 -0300\n' >"$cfg"
check config-last-wins 09761.0000 -c 2026-09-22
printf '\ncolour = red\n' >"$cfg"
check config-unknown "daykeep: $cfg:2: unknown setting 'colour'" --now
status config-unknown-status 2 --now
printf 'epoch 1987-07-28\n' >"$cfg"
check config-no-eq   "daykeep: $cfg:1: invalid line 'epoch 1987-07-28'" --now
printf 'epoch = soon\n' >"$cfg"
check config-bad     "daykeep: $cfg:1: invalid epoch 'soon'" --now
status config-bad-status 2 --now
DAYKEEP_EPOCH=2000-01-01 check config-bypass 09761.4564 --now   # a broken file can be bypassed
rm "$cfg"; mkdir "$cfg"
status config-dir    2 --now
rmdir "$cfg"
# a relative XDG_CONFIG_HOME is ignored in favour of ~/.config
mkdir -p "$tmp/home/.config/daykeep"
echo 'epoch = 1987-07-28' >"$tmp/home/.config/daykeep/config"
HOME=$tmp/home XDG_CONFIG_HOME=rel check config-home 14301.4167 -c 2026-09-22T10:00Z
HOME=$tmp/home check config-xdg-first 09761.4167 -c 2026-09-22T10:00Z
env -u HOME -u XDG_CONFIG_HOME "$bin" --now >/dev/null 2>&1 && ok config-none || bad config-none "exit $?"

# --- configurable personal days ------------------------------------------
for length in 1 3 9999 10000 10001 43200 86400 88775 90000 1000000000; do
	DAYKEEP_NOW=@$((946684800 + length)) check "length-boundary-$length" 00001.0000 --day-length="$length" --now
	DAYKEEP_NOW=@946684800 check "length-epoch-$length" 00000.0000 --day-length="$length" --now
	DAYKEEP_NOW=@$((946684800 - length)) check "length-negative-$length" -0001.0000 --day-length="$length" --now
	DAYKEEP_NOW=@946684800 check "length-convert-$length" "$(date -u -d @$((946684800 + length)) '+%F %T %z')" --day-length="$length" -u -c 00001
done
for value in '' 0 -1 +1 1.5 86400s ' 86400' '86400 ' 1e5 1000000001 999999999999999999999; do
	status "length-invalid-$value" 2 --day-length="$value" --now
done
check length-leading-zeros 09761.4564 --day-length=00086400 --now
check length-last-option 09761.4564 --day-length=x --day-length=86400 --now
DAYKEEP_NOW=@946728000 check length-halfday 00001.0000 --day-length=43200 --now
DAYKEEP_NOW=@946728000 DAYKEEP_DAY_LENGTH=43200 check length-env 00001.0000 --now
DAYKEEP_NOW=@946728000 DAYKEEP_DAY_LENGTH=x check length-option-wins 00000.5000 --day-length=86400 --now
DAYKEEP_DAY_LENGTH=x status length-invalid-env 2 --now
DAYKEEP_DAY_LENGTH= check length-empty-env 09761.4564 --now
check length-rollover .2000 --day-length=90000 .9 - .1
check length-hm '5h 0m' --day-length=90000 --hm .9 - .1
check length-minutes 300 --day-length=90000 --format=minutes .9 - .1
check length-clock-decimal .0800 --day-length=90000 -u 23:00 - 01:00
check length-clock-hm '2h 0m' --day-length=43200 --hm -u 23:00 - 01:00
DAYKEEP_NOW=@946684800 check length-whole-second 00001.0000 --day-length=1 -u -c 2000-01-01T00:00:01Z
DAYKEEP_NOW=@946684800 check length-short-quantization '2000-01-01 00:00:01 +0000' --day-length=1 -u -c .5
DAYKEEP_NOW=@946684800 check length-custom-epoch 00002.0000 --epoch=1999-12-31 --day-length=43200 --now
DAYKEEP_NOW=@946684799 check length-negative-fraction -0001.6667 --day-length=3 --now
DAYKEEP_NOW=@946684800 check length-short-hm -39600.0000 --day-length=1 -c 10:00
DAYKEEP_NOW=@1790035200 check length-long-number 1235606400.0000 --epoch=1987-07-28 --day-length=1 --now
DAYKEEP_NOW=1235606400.0000 check length-long-roundtrip '2026-09-22 00:00:00 +0000' --epoch=1987-07-28 --day-length=1 -u -c 1235606400.0000
DAYKEEP_NOW=9223372036854775807 check length-max-stamp 9223372036854775807.0000 --day-length=1 --now
DAYKEEP_NOW=9223372036854775808 status length-overflow-now 2 --day-length=1 --now
DAYKEEP_NOW=@-9223372036854775808 status length-overflow-unix 2 --now
status length-overflow-day 1 --day-length=1000000000 -c 9223372036854775807
status length-overflow-frac 1 --day-length=1 -c 9223372036854775807.5
DAYKEEP_NOW=9223372036854775807 status length-overflow-completion 1 --day-length=1 -c +9
DAYKEEP_NOW=0 check length-large-format 999999999.0000 --day-length=1000000000 0 - 999999999
DAYKEEP_NOW=0 status length-overflow-total 1 --day-length=1 '0 - 9223372036854775807 0 - 1'

printf 'epoch = 2000-01-01\nday_length = 43200\n' >"$cfg"
DAYKEEP_NOW=@946728000 check length-config 00001.0000 --now
DAYKEEP_NOW=@946728000 check length-config-with-epoch-option 00003.0000 --epoch=1999-12-31 --now
DAYKEEP_NOW=@946728000 DAYKEEP_EPOCH=1999-12-31 check length-config-with-epoch-env 00003.0000 --now
DAYKEEP_NOW=@946728000 DAYKEEP_DAY_LENGTH=86400 check length-env-over-config 00000.5000 --now
DAYKEEP_NOW=@946728000 check length-option-keeps-config-epoch 00000.5000 --day-length=86400 --now
printf 'day_length = nope\n day_length = 86400 \n' >"$cfg"
check length-config-last 09761.4564 --now
printf 'day_length = nope\n' >"$cfg"
status length-config-invalid 2 --now
check length-config-bypass 09761.4564 --day-length=86400 --now
printf 'unknown = value\n' >"$cfg"
status length-config-unknown 2 --epoch=2000-01-01 --now
check length-config-bypass-both 09761.4564 --epoch=2000-01-01 --day-length=86400 --now
rm "$cfg"

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
