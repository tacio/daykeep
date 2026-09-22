/* libFuzzer harness for daykeep's time library (make fuzz-libfuzzer).
 *
 * Input: one flag byte (bit 0: UTC), eight bytes of "now" (little-endian,
 * folded into |s| < 2^49), then the text to parse.  Every parser runs on
 * the text; every result is formatted every way.  Besides the sanitizers,
 * it traps if a range END comes before its START, or if a stamp does not
 * read back as itself.
 *
 * Copyright (C) 2026 Tacio Medeiros
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
#include "dktime.h"

#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define LIMIT (1LL << 49)

static void fmt_all(dk_secs s, int utc)
{
	char buf[DK_BUFSZ];

	dk_fmt_stamp(buf, s);
	dk_fmt_dur(buf, s);
	dk_fmt_hm(buf, s);
	dk_fmt_minutes(buf, s);
	dk_fmt_clock(buf, s, utc);
}

/* a stamp in 0..999999999 days prints as digits that read back literally */
static void round_trip(dk_secs s)
{
	char a[DK_BUFSZ], b[DK_BUFSZ];
	dk_secs back;

	if (s < 0 || s >= 999999999LL * DK_DAY)
		return;
	dk_fmt_stamp(a, s);
	if (dk_parse_literal(a, &back) != 0)
		__builtin_trap();
	dk_fmt_stamp(b, back);
	if (strcmp(a, b) != 0 || back - s > 4 || s - back > 4)
		__builtin_trap();
}

int LLVMFuzzerInitialize(int *argc, char ***argv)
{
	(void)argc;
	(void)argv;
	setenv("TZ", "Europe/Berlin", 1);      /* DST, so mktime has work to do */
	return 0;
}

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
	uint64_t raw = 0;
	dk_secs now, s, e;
	char *text;
	int utc, i;

	if (size < 9)
		return 0;
	utc = data[0] & 1;
	for (i = 0; i < 8; i++)
		raw |= (uint64_t)data[1 + i] << (8 * i);
	now = (dk_secs)(raw % (2 * (uint64_t)LIMIT)) - LIMIT;
	if (!(text = malloc(size - 8)))
		return 0;
	memcpy(text, data + 9, size - 9);
	text[size - 9] = '\0';

	if (dk_parse_stamp(text, now, utc, &s) == 0) {
		fmt_all(s, utc);
		round_trip(s);
	}
	if (dk_parse_end(text, now, utc, &e) == 0) {
		if (e < now)
			__builtin_trap();
		fmt_all(e - now, utc);
	}
	if (dk_parse_literal(text, &s) == 0)
		round_trip(s);
	if (dk_parse_date(text, utc, &s) == 0)
		fmt_all(s, utc);
	fmt_all(now, utc);
	free(text);
	return 0;
}
