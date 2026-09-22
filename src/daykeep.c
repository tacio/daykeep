/* daykeep: a decimal-time tracker.
 *
 * Times are day numbers plus fractions of a day since 1987-07-28 00:00 UTC;
 * see dktime.h.  This file is the command line: summing ranges, --now and
 * --convert.  The tracker comes later.
 */
#include "dktime.h"

#include <errno.h>
#include <getopt.h>
#include <stdarg.h>
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
static int summarize;           /* -s: print only the final total */
static enum { FMT_DECIMAL, FMT_HM, FMT_MINUTES } format = FMT_DECIMAL;
static int status = EXIT_OK;    /* worst exit status so far */

static void set_status(int st)
{
	if (st > status)
		status = st;
}

static void try_help(void)
{
	fprintf(stderr, "Try '%s --help' for more information.\n", PROGRAM);
	exit(EXIT_TROUBLE);
}

static void usage(void)
{
	printf("Usage: %s [OPTION]... [RANGE]...\n"
	       "  or:  %s [OPTION]... --now\n"
	       "  or:  %s [OPTION]... --convert VALUE...\n", PROGRAM, PROGRAM, PROGRAM);
	fputs("Work with decimal time: day numbers and fractions of a day counted\n"
	      "from day 0 = 1987-07-28 00:00 UTC.  One step of the 4th decimal\n"
	      "place is 8.64 seconds; 90 minutes is .0625.\n"
	      "\n"
	      "Sum time ranges, written START - END, and print the running total\n"
	      "after each one.  Ranges come from the operands, else from each FILE,\n"
	      "else from standard input.  A line may hold several ranges; one that\n"
	      "ends in '-' is still open and runs until now.\n"
	      "\n"
	      "  -f, --file=FILE    read ranges from FILE; - is standard input\n"
	      "  -s, --summarize    print only the final total\n"
	      "      --format=FMT   print totals as FMT: decimal (.0625, the default),\n"
	      "                       hm (1h 30m) or minutes (90)\n"
	      "      --hm           same as --format=hm\n"
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
	      "An END that leaves out digits is completed from START instead: it is\n"
	      "the first matching time not before START, so .9 - .1 lasts .2000 and\n"
	      "23:00 - 01:00 lasts two hours.\n"
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

/* --- summing ------------------------------------------------------------ */

static dk_secs total;

/* where the text being summed came from, for diagnostics */
struct src {
	const char *name;       /* file name, or NULL for the operands */
	long line;
};

static void bad_input(const struct src *src, const char *fmt, ...)
{
	va_list ap;

	fprintf(stderr, "%s: ", PROGRAM);
	if (src->name)
		fprintf(stderr, "%s:%ld: ", src->name, src->line);
	va_start(ap, fmt);
	vfprintf(stderr, fmt, ap);
	va_end(ap);
	fputc('\n', stderr);
	set_status(EXIT_BADINPUT);
}

static void print_dur(dk_secs d)
{
	char buf[DK_BUFSZ];

	switch (format) {
	case FMT_DECIMAL: dk_fmt_dur(buf, d); break;
	case FMT_HM:      dk_fmt_hm(buf, d); break;
	case FMT_MINUTES: dk_fmt_minutes(buf, d); break;
	}
	puts(buf);
}

static void add_range(dk_secs start, dk_secs end)
{
	total += end - start;
	if (!summarize)
		print_dur(total);
}

#define BLANKS " \t\r\n\f\v"

/* Sum the ranges on one line.  Tokens are '-' and runs of anything else
 * between blanks and dashes, so "10:00-11:00" works.  An error drops the
 * rest of the line. */
static void sum_line(char *p, const struct src *src, dk_secs t_now)
{
	enum { WANT_START, WANT_DASH, WANT_END } state = WANT_START;
	char start_text[DK_BUFSZ];
	dk_secs start = 0, end;
	char *tok, saved;
	size_t n;

	for (;;) {
		p += strspn(p, BLANKS);
		if (*p == '\0')
			break;
		n = (*p == '-') ? 1 : strcspn(p, BLANKS "-");
		tok = p;
		p += n;
		saved = *p;
		*p = '\0';

		switch (state) {
		case WANT_START:
			if (*tok == '-') {
				bad_input(src, "'-' with no START before it");
				return;
			}
			if (dk_parse_stamp(tok, t_now, utc, &start)) {
				bad_input(src, "invalid stamp '%s'", tok);
				return;
			}
			snprintf(start_text, sizeof start_text, "%s", tok);
			state = WANT_DASH;
			break;
		case WANT_DASH:
			if (*tok != '-') {
				bad_input(src, "missing '-' after '%s'", start_text);
				return;
			}
			state = WANT_END;
			break;
		case WANT_END:
			if (*tok == '-') {
				bad_input(src, "missing END after '%s -'", start_text);
				return;
			}
			switch (dk_parse_end(tok, start, utc, &end)) {
			case 0:
				add_range(start, end);
				state = WANT_START;
				break;
			case -2:
				bad_input(src, "'%s - %s' ends before it starts", start_text, tok);
				return;
			default:
				bad_input(src, "invalid stamp '%s'", tok);
				return;
			}
			break;
		}
		*p = saved;
	}

	if (state == WANT_DASH) {
		bad_input(src, "missing '-' after '%s'", start_text);
	} else if (state == WANT_END) {         /* open range: runs until now */
		if (t_now < start)
			bad_input(src, "open range '%s -' starts in the future", start_text);
		else
			add_range(start, t_now);
	}
}

/* the operands form one text, joined by spaces, so a range may span several
 * of them; newlines inside them still separate lines */
static void sum_operands(char **v, int n, dk_secs t_now)
{
	struct src src = { NULL, 0 };
	size_t len = 1;
	char *text, *p, *nl;
	int i;

	for (i = 0; i < n; i++)
		len += strlen(v[i]) + 1;
	if (!(text = malloc(len))) {
		fprintf(stderr, "%s: %s\n", PROGRAM, strerror(errno));
		exit(EXIT_TROUBLE);
	}
	for (p = text, i = 0; i < n; i++) {
		p = stpcpy(p, v[i]);
		*p++ = ' ';
	}
	*p = '\0';
	for (p = text; p; p = nl) {
		if ((nl = strchr(p, '\n')))
			*nl++ = '\0';
		sum_line(p, &src, t_now);
	}
	free(text);
}

static const char **files;      /* -f FILE, in order */
static int nfiles;

static void sum_file(const char *name, dk_secs t_now)
{
	struct src src = { name, 0 };
	FILE *f = strcmp(name, "-") == 0 ? stdin : fopen(name, "r");
	char *line = NULL;
	size_t cap = 0;

	if (!f) {
		fprintf(stderr, "%s: %s: %s\n", PROGRAM, name, strerror(errno));
		set_status(EXIT_TROUBLE);
		return;
	}
	while (getline(&line, &cap, f) != -1) {
		src.line++;
		sum_line(line, &src, t_now);
	}
	if (ferror(f)) {
		fprintf(stderr, "%s: %s: read error: %s\n", PROGRAM, name, strerror(errno));
		set_status(EXIT_TROUBLE);
	}
	free(line);
	if (f == stdin)
		clearerr(stdin);
	else
		fclose(f);
}

/* flush stdout and report any write error, as GNU close_stdout does */
static void close_stdout(void)
{
	if (fclose(stdout) != 0) {
		fprintf(stderr, "%s: write error: %s\n", PROGRAM, strerror(errno));
		_exit(EXIT_TROUBLE);
	}
}

enum { OPT_NOW = 256, OPT_FORMAT, OPT_HM, OPT_HELP, OPT_VERSION };

static const struct option longopts[] = {
	{ "file",      required_argument, NULL, 'f' },
	{ "summarize", no_argument,       NULL, 's' },
	{ "format",    required_argument, NULL, OPT_FORMAT },
	{ "hm",        no_argument,       NULL, OPT_HM },
	{ "now",       no_argument,       NULL, OPT_NOW },
	{ "convert",   no_argument,       NULL, 'c' },
	{ "utc",       no_argument,       NULL, 'u' },
	{ "help",      no_argument,       NULL, OPT_HELP },
	{ "version",   no_argument,       NULL, OPT_VERSION },
	{ NULL, 0, NULL, 0 }
};

int main(int argc, char **argv)
{
	enum { MODE_NONE, MODE_NOW, MODE_CONVERT } mode = MODE_NONE;
	char buf[DK_BUFSZ];
	dk_secs t_now;
	int c, i;

	atexit(close_stdout);
	if (!(files = malloc(argc * sizeof *files))) {
		fprintf(stderr, "%s: %s\n", PROGRAM, strerror(errno));
		return EXIT_TROUBLE;
	}
	while ((c = getopt_long(argc, argv, "cf:su", longopts, NULL)) != -1) {
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
		case 'f':
			files[nfiles++] = optarg;
			break;
		case 's':
			summarize = 1;
			break;
		case OPT_HM:
			format = FMT_HM;
			break;
		case OPT_FORMAT:
			if (strcmp(optarg, "decimal") == 0)
				format = FMT_DECIMAL;
			else if (strcmp(optarg, "hm") == 0)
				format = FMT_HM;
			else if (strcmp(optarg, "minutes") == 0)
				format = FMT_MINUTES;
			else {
				fprintf(stderr, "%s: invalid argument '%s' for '--format'\n"
				        "Valid arguments are: 'decimal', 'hm', 'minutes'\n",
				        PROGRAM, optarg);
				try_help();
			}
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

	if (mode != MODE_NONE && nfiles > 0) {
		fprintf(stderr, "%s: -f works only when summing ranges\n", PROGRAM);
		try_help();
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
				set_status(EXIT_BADINPUT);
		break;
	case MODE_NONE:
		if (optind < argc)
			sum_operands(argv + optind, argc - optind, t_now);
		for (i = 0; i < nfiles; i++)
			sum_file(files[i], t_now);
		if (optind == argc && nfiles == 0)
			sum_file("-", t_now);
		if (summarize)
			print_dur(total);
		break;
	}
	return status;
}
