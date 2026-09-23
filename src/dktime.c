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

/* dktime: decimal time for daykeep (see dktime.h). */
#include "dktime.h"

#include <limits.h>
#include <stdio.h>
#include <string.h>

/* longest day number or fraction we read; more fraction digits are ignored
 * (each further digit represents less than a second) */
#define MAX_DAY_DIGITS  19
#define MAX_FRAC_DIGITS 9

long long dk_epoch = DK_DEFAULT_EPOCH;
long long dk_day_length = DK_DEFAULT_DAY_LENGTH;

static int narrow(__int128 v, dk_secs *out)
{
	if (v < LLONG_MIN || v > LLONG_MAX)
		return -1;
	*out = (dk_secs)v;
	return 0;
}

int dk_add(dk_secs a, dk_secs b, dk_secs *out)
{
	return narrow((__int128)a + b, out);
}

int dk_sub(dk_secs a, dk_secs b, dk_secs *out)
{
	return narrow((__int128)a - b, out);
}

long long dk_floor_div(long long a, long long b)
{
	long long q = a / b;
	if ((a % b != 0) && ((a < 0) != (b < 0)))
		q--;
	return q;
}

static __int128 floor_div_wide(__int128 a, long long b)
{
	return a / b - (a % b < 0);
}

static int is_digit(int c) { return c >= '0' && c <= '9'; }

static int ndigits(__int128 v)
{
	int n = 1;
	while (v >= 10) {
		v /= 10;
		n++;
	}
	return n;
}

static __int128 complete_day(long long typed, int ndig, __int128 ref, int forward)
{
	long long m = 1;
	int i;

	if (ndig == 0)
		return ref;
	if (ref < 0 || ndig >= ndigits(ref))
		return typed;
	for (i = 0; i < ndig; i++)
		m *= 10;
	if (forward)
		return typed - floor_div_wide(typed - ref, m) * m;
	return typed + floor_div_wide(ref - typed, m) * m;
}

long long dk_complete_day(long long typed, int ndig, long long ref, int forward)
{
	dk_secs out;
	return narrow(complete_day(typed, ndig, ref, forward), &out) ? LLONG_MIN : out;
}

static int compose(__int128 day, long long frac, dk_secs *out)
{
	return narrow(day * dk_day_length + frac, out);
}

int dk_set_day_length(const char *s)
{
	long long v = 0;
	if (!*s)
		return -1;
	for (; *s; s++) {
		if (!is_digit(*s) || v > (DK_MAX_DAY_LENGTH - (*s - '0')) / 10)
			return -1;
		v = v * 10 + (*s - '0');
	}
	if (v == 0)
		return -1;
	dk_day_length = v;
	return 0;
}

/* --- parsing ------------------------------------------------------------ */

struct decimal {
	int plus;            /* leading '+' */
	int ndig;            /* day digits typed (0 = none) */
	long long day;       /* their value */
	long long frac;      /* fraction of a day, in seconds, rounded half-up */
};

/* [+][DIGITS][.[DIGITS]], at least one digit in total */
static int parse_decimal(const char *s, struct decimal *d)
{
	long long num = 0, den = 1;
	int nfrac = 0;

	d->plus = (*s == '+');
	s += d->plus;
	d->ndig = 0;
	d->day = 0;
	for (; is_digit(*s); s++) {
		if (++d->ndig > MAX_DAY_DIGITS || d->day > (LLONG_MAX - (*s - '0')) / 10)
			return -1;
		d->day = d->day * 10 + (*s - '0');
	}
	if (*s == '.') {
		for (s++; is_digit(*s); s++) {
			if (nfrac < MAX_FRAC_DIGITS) {
				num = num * 10 + (*s - '0');
				den *= 10;
				nfrac++;
			}
		}
	}
	if (*s != '\0' || d->ndig + nfrac == 0)
		return -1;
	d->frac = (num * dk_day_length + den / 2) / den;
	return 0;
}

/* read exactly two digits into *v */
static const char *two(const char *s, int *v)
{
	if (!is_digit(s[0]) || !is_digit(s[1]))
		return NULL;
	*v = (s[0] - '0') * 10 + (s[1] - '0');
	return s + 2;
}

/* H[H]:MM[:SS]; returns the end of the match or NULL */
static const char *parse_clock(const char *s, int *h, int *m, int *sec)
{
	if (!is_digit(s[0]))
		return NULL;
	*h = *s++ - '0';
	if (is_digit(*s))
		*h = *h * 10 + (*s++ - '0');
	if (*s++ != ':' || !(s = two(s, m)))
		return NULL;
	*sec = 0;
	if (*s == ':' && !(s = two(s + 1, sec)))
		return NULL;
	if (*h > 23 || *m > 59 || *sec > 59)
		return NULL;
	return s;
}

/* broken-down time -> dk seconds, in local time or UTC */
static int from_tm(struct tm *tm, int utc, dk_secs *out)
{
	time_t t;

	tm->tm_isdst = -1;
	t = utc ? timegm(tm) : mktime(tm);
	if (t == (time_t)-1 && tm->tm_year != 69)   /* 1969-12-31 23:59:59 is -1 */
		return -1;
	return dk_sub((dk_secs)t, dk_epoch, out);
}

static int to_tm(dk_secs s, int utc, struct tm *tm)
{
	dk_secs unix_secs;
	time_t t;
	if (dk_add(s, dk_epoch, &unix_secs))
		return -1;
	t = (time_t)unix_secs;
	return (utc ? gmtime_r(&t, tm) : localtime_r(&t, tm)) ? 0 : -1;
}

/* wall-clock H:M:S on the local (or UTC) date of REF, plus DAYS days */
static int clock_on(dk_secs ref, int days, int h, int m, int sec, int utc, dk_secs *out)
{
	struct tm tm;

	if (to_tm(ref, utc, &tm))
		return -1;
	tm.tm_mday += days;
	tm.tm_hour = h;
	tm.tm_min = m;
	tm.tm_sec = sec;
	return from_tm(&tm, utc, out);
}

/* S is a whole H[H]:MM[:SS] */
static int is_clock(const char *s, int *h, int *m, int *sec)
{
	const char *end = parse_clock(s, h, m, sec);
	return end && *end == '\0';
}

int dk_parse_stamp(const char *s, dk_secs now, int utc, dk_secs *out)
{
	struct decimal d;
	int h, m, sec;

	if (is_clock(s, &h, &m, &sec))
		return clock_on(now, 0, h, m, sec, utc, out);
	if (parse_decimal(s, &d))
		return -1;
	return compose(complete_day(d.day, d.ndig, dk_day(now), d.plus), d.frac, out);
}

int dk_parse_end(const char *s, dk_secs start, int utc, dk_secs *out)
{
	struct decimal d;
	long long ref = dk_day(start);
	int h, m, sec, literal;

	if (is_clock(s, &h, &m, &sec)) {
		if (clock_on(start, 0, h, m, sec, utc, out))
			return -1;
		if (*out < start && clock_on(start, 1, h, m, sec, utc, out))
			return -1;
		return 0;
	}
	if (parse_decimal(s, &d))
		return -1;
	literal = d.ndig > 0 && (ref < 0 || d.ndig >= ndigits(ref));
	if (compose(complete_day(d.day, d.ndig, ref, 1), d.frac, out))
		return -1;
	if (*out < start) {
		if (literal)
			return -2;
		return compose(complete_day(d.day, d.ndig, (__int128)ref + 1, 1), d.frac, out);
	}
	return 0;
}

int dk_parse_literal(const char *s, dk_secs *out)
{
	struct decimal d;

	if (parse_decimal(s, &d) || d.plus || d.ndig == 0)
		return -1;
	return compose(d.day, d.frac, out);
}

static int leap(int y) { return (y % 4 == 0 && y % 100 != 0) || y % 400 == 0; }

static int month_days(int y, int mon)
{
	static const int days[12] = { 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31 };
	return days[mon - 1] + (mon == 2 && leap(y));
}

int dk_parse_date(const char *s, int utc, dk_secs *out)
{
	struct tm tm;
	int y = 0, mon, day, h = 0, m = 0, sec = 0, i;
	int have_off = 0, off = 0, oh, om, sign;

	for (i = 0; i < 4; i++, s++) {
		if (!is_digit(*s))
			return -1;
		y = y * 10 + (*s - '0');
	}
	if (*s++ != '-' || !(s = two(s, &mon)) || *s++ != '-' || !(s = two(s, &day)))
		return -1;
	if (mon < 1 || mon > 12 || day < 1 || day > month_days(y, mon))
		return -1;
	if ((*s == 'T' || *s == ' ') && is_digit(s[1])) {
		if (!(s = parse_clock(s + 1, &h, &m, &sec)))
			return -1;
	}
	if (*s == ' ')
		s++;
	if (*s == 'Z') {
		have_off = 1;
		s++;
	} else if (*s == '+' || *s == '-') {
		sign = (*s++ == '-') ? -1 : 1;
		if (!(s = two(s, &oh)))
			return -1;
		if (*s == ':')
			s++;
		if (!(s = two(s, &om)) || oh > 23 || om > 59)
			return -1;
		have_off = 1;
		off = sign * (oh * 3600 + om * 60);
	}
	if (*s != '\0')
		return -1;

	memset(&tm, 0, sizeof tm);
	tm.tm_year = y - 1900;
	tm.tm_mon = mon - 1;
	tm.tm_mday = day;
	tm.tm_hour = h;
	tm.tm_min = m;
	tm.tm_sec = sec;
	if (from_tm(&tm, utc || have_off, out))
		return -1;
	return dk_sub(*out, off, out);
}

int dk_set_epoch(const char *s)
{
	dk_secs v;

	if (dk_parse_date(s, 1, &v))
		return -1;
	return dk_add(dk_epoch, v, &dk_epoch);
}

/* --- formatting --------------------------------------------------------- */

/* seconds -> 1/10000ths of a day, rounded half-up */
static __int128 ticks(__int128 s)
{
	return floor_div_wide(s * 10000 + dk_day_length / 2, dk_day_length);
}

int dk_fmt_stamp(char *buf, dk_secs s)
{
	__int128 t = ticks(s);
	long long day = (long long)floor_div_wide(t, 10000);
	long long frac = (long long)(t - (__int128)day * 10000);
	return snprintf(buf, DK_BUFSZ, "%05lld.%04lld",
	                day, frac);
}

/* durations format their magnitude, with a '-' in front if negative */
static __int128 magnitude(dk_secs d)
{
	return d < 0 ? -(__int128)d : d;
}

int dk_fmt_dur(char *buf, dk_secs d)
{
	const char *sg = d < 0 ? "-" : "";
	__int128 t = ticks(magnitude(d));

	if (t < 10000)
		return snprintf(buf, DK_BUFSZ, "%s.%04lld", sg, (long long)t);
	return snprintf(buf, DK_BUFSZ, "%s%llu.%04lld", sg,
	                (unsigned long long)(t / 10000), (long long)(t % 10000));
}

int dk_fmt_hm(char *buf, dk_secs d)
{
	const char *sg = d < 0 ? "-" : "";
	long long min = (long long)((magnitude(d) + 30) / 60);
	return snprintf(buf, DK_BUFSZ, "%s%lldh %lldm", sg, min / 60, min % 60);
}

int dk_fmt_minutes(char *buf, dk_secs d)
{
	const char *sg = d < 0 ? "-" : "";
	return snprintf(buf, DK_BUFSZ, "%s%lld", sg, (long long)((magnitude(d) + 30) / 60));
}

int dk_fmt_clock(char *buf, dk_secs s, int utc)
{
	struct tm tm;

	if (to_tm(s, utc, &tm))
		return snprintf(buf, DK_BUFSZ, "?");
	return (int)strftime(buf, DK_BUFSZ, "%Y-%m-%d %H:%M:%S %z", &tm);
}
