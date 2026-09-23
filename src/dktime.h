/* Copyright (C) 2026 Tacio Medeiros
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <https://www.gnu.org/licenses/>.
 */

/* dktime: decimal time for daykeep.
 *
 * A stamp is a day number plus a fraction of a day, counted from day 0,
 * which is 2000-01-01 00:00 UTC unless dk_set_epoch moves it.  Internally
 * every value is an integer count of seconds since that instant. A day is
 * dk_day_length seconds; formatting rounds to four decimal places. Decimal
 * input is rounded to whole seconds, so very short days quantize fractions.
 */
#ifndef DKTIME_H
#define DKTIME_H

#include <stddef.h>
#include <time.h>

#define DK_DEFAULT_DAY_LENGTH 86400LL
#define DK_MAX_DAY_LENGTH 1000000000LL
#define DK_DEFAULT_EPOCH 946684800LL          /* 2000-01-01 00:00 UTC */

extern long long dk_epoch;          /* Unix seconds of day 0 */
extern long long dk_day_length;     /* seconds per personal day */

typedef long long dk_secs;          /* seconds since day 0 (may be negative) */

/* room for any formatted value, including a clock string */
#define DK_BUFSZ 64

long long dk_floor_div(long long a, long long b);
static inline long long dk_day(dk_secs s) { return dk_floor_div(s, dk_day_length); }

/* Checked arithmetic shared by the CLI and tracker. Returns 0 or -1. */
int dk_add(dk_secs a, dk_secs b, dk_secs *out);
int dk_sub(dk_secs a, dk_secs b, dk_secs *out);

/* Complete a day number from its last NDIGITS typed digits (value TYPED).
 * With FORWARD == 0 it is the most recent day <= REF ending in those digits;
 * otherwise the first day >= REF.  NDIGITS == 0 means "REF itself".  When
 * NDIGITS is at least the digit count of REF the typed day is literal.
 * Returns LLONG_MIN if forward completion exceeds the signed range. */
long long dk_complete_day(long long typed, int ndigits, long long ref, int forward);

/* Parse a stamp relative to NOW:
 *   [+][DIGITS][.[DIGITS]]   decimal, left digits completed, right padded
 *   HH:MM[:SS]               wall-clock time today (local, or UTC if UTC)
 * Returns 0 and sets *OUT, or -1 if S is not a stamp. */
int dk_parse_stamp(const char *s, dk_secs now, int utc, dk_secs *out);

/* Parse the END of a range whose START is known.  Missing left digits are
 * completed forward from START's day, and a time that would fall before
 * START moves on to the next matching day. With the default length,
 * ".9 - .1" lasts .2000; "23:00 - 01:00" uses the civil clock.
 * Returns 0, -1 if S is not a stamp or exceeds the seconds range, or -2 if S
 * has a full day number and is still before START. */
int dk_parse_end(const char *s, dk_secs start, int utc, dk_secs *out);

/* Parse a full decimal stamp with no completion ("09761.4564"). */
int dk_parse_literal(const char *s, dk_secs *out);

/* Parse YYYY-MM-DD[( |T)HH:MM[:SS]][ ][Z|(+|-)HH[:]MM].  Without an offset
 * the date is local time (UTC if UTC). */
int dk_parse_date(const char *s, int utc, dk_secs *out);

/* Move day 0 to the instant S, in dk_parse_date's syntax but read as UTC
 * when S has no offset.  Returns 0, or -1 if S is not a date. */
int dk_set_epoch(const char *s);

/* Set seconds per day: ASCII digits, 1..DK_MAX_DAY_LENGTH. On failure,
 * return -1 and leave the current length unchanged. */
int dk_set_day_length(const char *s);

/* Formatters: write a NUL-terminated string into BUF (DK_BUFSZ bytes) and
 * return its length. */
int dk_fmt_stamp(char *buf, dk_secs s);        /* 09761.4564, 00007.0000 */
int dk_fmt_dur(char *buf, dk_secs d);          /* .0625, 1.2500          */
int dk_fmt_hm(char *buf, dk_secs d);           /* 1h 30m                 */
int dk_fmt_minutes(char *buf, dk_secs d);      /* 90                     */
int dk_fmt_clock(char *buf, dk_secs s, int utc); /* 2026-09-22 07:57:13 -0300 */

#endif
