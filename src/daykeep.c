/* daykeep: a decimal-time tracker.
 *
 * Times are day numbers plus fractions of a day since 1987-07-28 00:00 UTC;
 * see dktime.h.  This file is the command line: option parsing, --now and
 * --convert.  Summing ranges and the tracker come later.
 */
#include "dktime.h"

#include <errno.h>
#include <getopt.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define PROGRAM "daykeep"
#define VERSION "0.1"

/* exit statuses */
enum { EXIT_OK = 0, EXIT_BADINPUT = 1, EXIT_TROUBLE = 2 };

static int utc;                 /* -u: HH:MM and clock output in UTC */

static void try_help(void)
{
	fprintf(stderr, "Try '%s --help' for more information.\n", PROGRAM);
	exit(EXIT_TROUBLE);
}

static void usage(void)
{
	printf("Usage: %s [OPTION]... --now\n"
	       "  or:  %s [OPTION]... --convert VALUE...\n", PROGRAM, PROGRAM);
	fputs("Work with decimal time: day numbers and fractions of a day counted\n"
	      "from day 0 = 1987-07-28 00:00 UTC.  One step of the 4th decimal\n"
	      "place is 8.64 seconds; 90 minutes is .0625.\n"
	      "\n"
	      "      --now          print the current stamp\n"
	      "  -c, --convert      convert each VALUE: a decimal stamp becomes a\n"
	      "                       local date and time; HH:MM or an ISO date\n"
	      "                       (YYYY-MM-DD[ HH:MM[:SS]][ +HHMM]) becomes a stamp\n"
	      "  -u, --utc          read and print wall-clock times in UTC\n"
	      "      --help         display this help and exit\n"
	      "      --version      output version information and exit\n"
	      "\n"
	      "A stamp may leave out digits.  Missing right digits are zeros\n"
	      "(.5 is .5000).  Missing left digits pick the most recent day, up to\n"
	      "today, whose number ends in the digits given; a leading '+' picks the\n"
	      "first such day from today on.  If today is 14301, then 9 is 14299,\n"
	      "+9 is 14309 and .5 is 14301.5000.  HH:MM means that time today.\n"
	      "\n"
	      "Exit status is 0 if all went well, 1 if some input was invalid, and\n"
	      "2 for usage or I/O errors.\n", stdout);
}

static void version(void)
{
	printf("%s %s\n", PROGRAM, VERSION);
	fputs("Copyright (C) 2026 Tacio Medeiros\n"
	      "License GPLv3+: GNU GPL version 3 or later <https://gnu.org/licenses/gpl.html>.\n"
	      "This is free software: you are free to change and redistribute it.\n"
	      "There is NO WARRANTY, to the extent permitted by law.\n", stdout);
}

/* The current time.  DAYKEEP_NOW overrides the clock for tests: either a
 * full decimal stamp or @UNIX_SECONDS. */
static dk_secs now(void)
{
	const char *env = getenv("DAYKEEP_NOW");
	dk_secs s;
	char *end;
	long long t;

	if (!env || !*env)
		return dk_from_unix(time(NULL));
	if (*env == '@') {
		errno = 0;
		t = strtoll(env + 1, &end, 10);
		if (end != env + 1 && *end == '\0' && errno == 0)
			return dk_from_unix((time_t)t);
	} else if (dk_parse_literal(env, &s) == 0) {
		return s;
	}
	fprintf(stderr, "%s: invalid DAYKEEP_NOW '%s'\n", PROGRAM, env);
	exit(EXIT_TROUBLE);
}

static int is_date(const char *s)
{
	return strlen(s) >= 5 && s[4] == '-';
}

/* convert one VALUE; returns 0, or -1 after reporting bad input */
static int convert(const char *v, dk_secs t_now)
{
	char buf[DK_BUFSZ];
	dk_secs s;

	if (is_date(v)) {
		if (dk_parse_date(v, utc, &s))
			goto bad;
		dk_fmt_stamp(buf, s);
	} else if (strchr(v, ':')) {
		if (dk_parse_stamp(v, t_now, utc, &s))
			goto bad;
		dk_fmt_stamp(buf, s);
	} else {
		if (dk_parse_stamp(v, t_now, utc, &s))
			goto bad;
		dk_fmt_clock(buf, s, utc);
	}
	puts(buf);
	return 0;
bad:
	fprintf(stderr, "%s: invalid value '%s'\n", PROGRAM, v);
	return -1;
}

/* flush stdout and report any write error, as GNU close_stdout does */
static void close_stdout(void)
{
	if (fclose(stdout) != 0) {
		fprintf(stderr, "%s: write error: %s\n", PROGRAM, strerror(errno));
		_exit(EXIT_TROUBLE);
	}
}

enum { OPT_NOW = 256, OPT_HELP, OPT_VERSION };

static const struct option longopts[] = {
	{ "now",     no_argument, NULL, OPT_NOW },
	{ "convert", no_argument, NULL, 'c' },
	{ "utc",     no_argument, NULL, 'u' },
	{ "help",    no_argument, NULL, OPT_HELP },
	{ "version", no_argument, NULL, OPT_VERSION },
	{ NULL, 0, NULL, 0 }
};

int main(int argc, char **argv)
{
	enum { MODE_NONE, MODE_NOW, MODE_CONVERT } mode = MODE_NONE;
	char buf[DK_BUFSZ];
	dk_secs t_now;
	int c, status = EXIT_OK;

	atexit(close_stdout);
	while ((c = getopt_long(argc, argv, "cu", longopts, NULL)) != -1) {
		switch (c) {
		case OPT_NOW:
		case 'c':
			if (mode != MODE_NONE) {
				fprintf(stderr, "%s: --now and --convert are exclusive\n", PROGRAM);
				try_help();
			}
			mode = (c == 'c') ? MODE_CONVERT : MODE_NOW;
			break;
		case 'u':
			utc = 1;
			break;
		case OPT_HELP:
			usage();
			return EXIT_OK;
		case OPT_VERSION:
			version();
			return EXIT_OK;
		default:
			try_help();
		}
	}

	t_now = now();
	switch (mode) {
	case MODE_NOW:
		if (optind < argc) {
			fprintf(stderr, "%s: extra operand '%s'\n", PROGRAM, argv[optind]);
			try_help();
		}
		dk_fmt_stamp(buf, t_now);
		puts(buf);
		break;
	case MODE_CONVERT:
		if (optind == argc) {
			fprintf(stderr, "%s: --convert needs a VALUE\n", PROGRAM);
			try_help();
		}
		for (; optind < argc; optind++)
			if (convert(argv[optind], t_now))
				status = EXIT_BADINPUT;
		break;
	case MODE_NONE:
		if (optind == argc) {
			fprintf(stderr, "%s: missing operand\n", PROGRAM);
			try_help();
		}
		fprintf(stderr, "%s: summing ranges is not implemented yet\n", PROGRAM);
		return EXIT_TROUBLE;
	}
	return status;
}
