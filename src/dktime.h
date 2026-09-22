/* dktime: decimal time for daykeep.
 *
 * A stamp is a day number plus a fraction of a day, counted from day 0 =
 * 1987-07-28 00:00 UTC.  Internally every value is an integer count of
 * seconds since that instant; rounding to 4 decimal places (8.64 s) happens
 * only when formatting, so sums of many entries do not drift.
 */
#ifndef DKTIME_H
#define DKTIME_H

#include <stddef.h>
#include <time.h>

#define DK_DAY        86400LL
#define DK_EPOCH_DAY  6417LL                     /* Unix day of day 0 */
#define DK_EPOCH_UNIX (DK_EPOCH_DAY * DK_DAY)    /* 554428800 */

typedef long long dk_secs;          /* seconds since day 0 (may be negative) */

/* room for any formatted value, including a clock string */
#define DK_BUFSZ 64

long long dk_floor_div(long long a, long long b);
static inline dk_secs dk_from_unix(time_t t) { return (dk_secs)t - DK_EPOCH_UNIX; }
static inline time_t dk_to_unix(dk_secs s) { return (time_t)(s + DK_EPOCH_UNIX); }
static inline long long dk_day(dk_secs s) { return dk_floor_div(s, DK_DAY); }

/* Complete a day number from its last NDIGITS typed digits (value TYPED).
 * With FORWARD == 0 it is the most recent day <= REF ending in those digits;
 * otherwise the first day >= REF.  NDIGITS == 0 means "REF itself".  When
 * NDIGITS is at least the digit count of REF the typed day is literal. */
long long dk_complete_day(long long typed, int ndigits, long long ref, int forward);

/* Parse a stamp relative to NOW:
 *   [+][DIGITS][.[DIGITS]]   decimal, left digits completed, right padded
 *   HH:MM[:SS]               wall-clock time today (local, or UTC if UTC)
 * Returns 0 and sets *OUT, or -1 if S is not a stamp. */
int dk_parse_stamp(const char *s, dk_secs now, int utc, dk_secs *out);

/* Parse a full decimal stamp with no completion ("14301.4564"). */
int dk_parse_literal(const char *s, dk_secs *out);

/* Parse YYYY-MM-DD[( |T)HH:MM[:SS]][ ][Z|(+|-)HH[:]MM].  Without an offset
 * the date is local time (UTC if UTC). */
int dk_parse_date(const char *s, int utc, dk_secs *out);

/* Formatters: write a NUL-terminated string into BUF (DK_BUFSZ bytes) and
 * return its length. */
int dk_fmt_stamp(char *buf, dk_secs s);        /* 14301.4564, 00007.0000 */
int dk_fmt_dur(char *buf, dk_secs d);          /* .0625, 1.2500          */
int dk_fmt_hm(char *buf, dk_secs d);           /* 1h 30m                 */
int dk_fmt_minutes(char *buf, dk_secs d);      /* 90                     */
int dk_fmt_clock(char *buf, dk_secs s, int utc); /* 2026-09-22 07:57:13 -0300 */

#endif
