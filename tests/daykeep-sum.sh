#!/bin/bash
# usage: tests/daykeep-sum.sh ./daykeep [./timekeep]  -- sum mode
# Clock and zone are pinned as in daykeep-core.sh.  If a timekeep binary is
# given, daykeep --hm must print exactly what it prints for the old inputs.
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
tk=${2:-}
fail=0

export DAYKEEP_NOW=14301.4564      # 10:57:13 UTC = 07:57:13 local
export TZ='<-03>3'                 # fixed UTC-3, no DST
unset POSIXLY_CORRECT DAYKEEP_EPOCH

# an empty config directory, so the user's own config can't interfere
tmp=$(mktemp -d) && trap 'rm -rf "$tmp"' EXIT || exit 1
export XDG_CONFIG_HOME=$tmp/config

ok()  { echo "ok   $1"; }
bad() { echo "FAIL $1: $2"; fail=1; }

# stdin is /dev/null unless a test pipes something in, so nothing hangs
check() { # name, expected stdout, args...
	local name=$1 want=$2; shift 2
	local got; got=$("$bin" "$@" 2>/dev/null </dev/null)
	if [ "$got" == "$want" ]; then ok "$name"; else bad "$name" "got [$got] want [$want]"; fi
}

err() { # name, expected stderr, expected status, args...
	local name=$1 want=$2 wrc=$3; shift 3
	local got; got=$("$bin" "$@" 2>&1 >/dev/null </dev/null); local rc=$?
	if [ "$got" == "$want" ] && [ $rc -eq "$wrc" ]; then ok "$name"
	else bad "$name" "exit $rc [$got], want exit $wrc [$want]"; fi
}

pipe() { # name, expected stdout, stdin text, args...
	local name=$1 want=$2 in=$3; shift 3
	local got; got=$(printf '%s' "$in" | "$bin" "$@" 2>/dev/null)
	if [ "$got" == "$want" ]; then ok "$name"; else bad "$name" "got [$got] want [$want]"; fi
}

# --- ranges and END completion ---------------------------------------------
check full        .0625              14301.3750 - 14301.4375
check running     $'.0625\n.1250'    14301.3750 - 14301.4375 14301.1 - 14301.1625
check end-short   .0625              14301.375 - .4375        # stays on 14301
check rollover    .2000              .9 - .1
check end-digits  4.0000             .5 - 5.5                 # 14305.5
check end-roll-d  9.2000             1.9 - 1.1                # 14301.9 .. 14311.1
check same        .0000              14301.5 - 14301.5
check multi-day   2.5000             14299.0 - 14301.5
check no-spaces   .0625              14301.3750-14301.4375
check split-args  .0625              14301.3750 - .4375
check start-plus  .1000              +.1 - .2
err   before-full "daykeep: '14301.5 - 14301.4' ends before it starts" 1 14301.5 - 14301.4

# --- open ranges run until now ----------------------------------------------
check open        .0564              .4 -
check open-hm     '0h 57m'           --hm 07:00 -
err   open-future "daykeep: open range '.5 -' starts in the future" 1 .5 -

# --- HH:MM: local wall clock, rolls past midnight ----------------------------
check hm          '1h 30m'           --hm '09:00 - 10:30'
check hm-wrap     '1h 28m'           --hm 23:24 - 00:52
check hm-seconds  '0h 1m'            --hm 10:00:00 - 10:00:59   # rounds to the minute
check hm-decimal  .0417              10:00 - 11:00
check hm-mixed    .2083              14301.5 - 14:00            # 09:00 .. 14:00 local
check hm-local-d  .1250              06:00 - .5                 # 09:00 UTC
check hm-utc      .2500              -u 06:00 - .5

# --- formats and -s ------------------------------------------------------------
check fmt-minutes $'90\n135'         --format=minutes 09:00 - 10:30 11:00 - 11:45
check fmt-hm      $'1h 30m\n2h 15m'  --format=hm 09:00 - 10:30 11:00 - 11:45
check fmt-decimal .0625              --hm --format=decimal 09:00 - 10:30
check permute     '2h 24m'           .1 - .2 --hm
POSIXLY_CORRECT=1 err posixly "daykeep: '-' with no START before it" 1 .1 - .2 --hm
check summarize   '2h 15m'           -s --hm 09:00 - 10:30 11:00 - 11:45
check sum-empty   .0000              -s
check sum-empty-m 0                  -s --format=minutes
check none        ''                 ''
check long        1.0000             -s .0 - .5 .5 - .0

# --- files and stdin ------------------------------------------------------------
printf '14301.3750 - 14301.4375\n\n14301.1000 - 14301.1625\r\n' > "$tmp/a"
printf '09:00 - 10:30\n' > "$tmp/b"
check file        $'.0625\n.1250'     -f "$tmp/a"
check files       $'.0625\n.1250\n.1875' -f "$tmp/a" --file="$tmp/b"
check operands-first $'.0625\n.1250\n.1875\n.2500' 09:00 - 10:30 -f "$tmp/a" -f "$tmp/b"
pipe  stdin       $'.0625\n.1250'     $'.375 - .4375\n.1 - .1625\n'
pipe  stdin-dash  .1250               $'.375 - .4375\n' -f "$tmp/b" -f - -s --format=decimal
pipe  stdin-nonl  '1h 30m'            '09:00 - 10:30' --hm

# the log format written by -a reads back, open entry included
printf '14301.3750 - 14301.4375\n14301.4500 -\n' > "$tmp/log"
check log         .0689               -s -f "$tmp/log"

# --- diagnostics: FILE:LINE, bad lines are skipped, exit 1 ------------------------
printf '.1 - .2\nx - .3\n.3 .4\n- .5\n.6 -  -\n.7\n14301.8 - 14301.7\n.8 - .9\n' > "$tmp/bad"
want="daykeep: $tmp/bad:2: invalid stamp 'x'
daykeep: $tmp/bad:3: missing '-' after '.3'
daykeep: $tmp/bad:4: '-' with no START before it
daykeep: $tmp/bad:5: missing END after '.6 -'
daykeep: $tmp/bad:6: missing '-' after '.7'
daykeep: $tmp/bad:7: '14301.8 - 14301.7' ends before it starts"
err   file-errors "$want" 1 -f "$tmp/bad"
check file-errors-sum .2000 -s -f "$tmp/bad"
err   bad-operand "daykeep: invalid stamp '10:6'" 1 10:6 - 11:00
err   bad-end     "daykeep: invalid stamp 'y'" 1 .1 - y
pipe  stdin-total .0000 $'x\n' -s                         # -s prints a total even so
got=$(printf 'x\n' | "$bin" 2>&1 >/dev/null)
if [ "$got" == "daykeep: -:1: invalid stamp 'x'" ]; then ok stdin-errname; else bad stdin-errname "[$got]"; fi

err   no-file     "daykeep: $tmp/none: No such file or directory" 2 -f "$tmp/none"
err   bad-and-io  "daykeep: invalid stamp 'x'
daykeep: $tmp/none: No such file or directory" 2 x - -f "$tmp/none"
err   bad-format  "daykeep: invalid argument 'hours' for '--format'
Valid arguments are: 'decimal', 'hm', 'minutes'
Try 'daykeep --help' for more information." 2 --format=hours
got=$("$bin" -f 2>&1); rc=$?
if [ $rc -eq 2 ] && [[ $got == *"requires an argument -- 'f'"* ]]; then ok no-f-arg
else bad no-f-arg "exit $rc [$got]"; fi
if [ -d /proc/self ]; then
	err read-error "daykeep: $tmp: read error: Is a directory" 2 -f "$tmp"
fi

# write errors on stdout are reported
if [ -w /dev/full ]; then
	err=$("$bin" .1 - .2 2>&1 >/dev/full); rc=$?
	if [ $rc -eq 2 ] && [[ $err == *"write error"* ]]; then ok full-sum
	else bad full-sum "exit $rc [$err]"; fi
fi

# --- old timekeep inputs: same output as timekeep, with --hm -----------------------
same() { # name, args...
	local name=$1; shift
	local want got
	want=$("$tk" "$@")
	got=$("$bin" --hm -- "$@" 2>&1 </dev/null)
	if [ "$got" == "$want" ]; then ok "timekeep $name"; else bad "timekeep $name" "got [$got] want [$want]"; fi
}
if [ -n "$tk" ]; then
	same example   $'11:46 - 13:35\n14:26 - 14:45\n15:15 - 16:29\n16:40 - 18:03\n18:36 - 20:30'
	same wrap-in   '23:24 - 00:52'
	same wrap-btw  $'22:00 - 23:30\n00:15 - 01:45'
	same no-nl     '10:00 - 10:45'
	same crlf      $'10:00 - 11:00\r\n12:00 - 12:30\r'
	same blanks    $'\n\n10:00 - 11:00\n\n   \n12:00-12:30  '
	same empty     ''
	same long-day  '00:00 - 23:59'
	same split-words 09:00 - 10:30 11:00 - 11:45
	same split-pair  '10:00-11:00' '12:00-12:30'
	same across-args '10:00 -' '11:00'
	same one-digit   '9:05 - 10:00'
	same midnight    '00:00 - 00:00'
fi

exit $fail
