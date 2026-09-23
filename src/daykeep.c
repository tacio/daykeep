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

/* daykeep: a decimal-time tracker.
 *
 * Times are day numbers plus fractions of a day since day 0, 2000-01-01
 * 00:00 UTC unless --epoch, DAYKEEP_EPOCH or the config file says otherwise;
 * see dktime.h.  This file is the command line: summing ranges, --now and
 * --convert.  The tracker is in track.c.
 */
#include "daykeep.h"

#include <errno.h>
#include <getopt.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define VERSION "0.1"

static int utc;                 /* -u: HH:MM and clock output in UTC */
static int summarize;           /* -s: print only the final total */
static enum dk_format format = FMT_DECIMAL;
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
	       "  or:  %s [OPTION]... --convert VALUE...\n"
	       "  or:  %s [OPTION]... --track\n", PROGRAM, PROGRAM, PROGRAM, PROGRAM);
	fputs("Work with decimal time: day numbers and fractions of a day counted\n"
	      "from day 0, which is 2000-01-01 00:00 UTC unless --epoch says\n"
	      "otherwise.  A day is 86400 seconds unless --day-length says\n"
	      "otherwise.  Examples below use the default length: one displayed\n"
	      "step is 8.64 seconds and 90 minutes is .0625.\n"
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
	      "  -t, --track        track time interactively: Enter stamps the time,\n"
	      "                       starting and ending entries in turn\n"
	      "  -a, --append=LOG   with --track, keep the entries in LOG, one range\n"
	      "                       per line; an open entry at its end is resumed\n"
	      "  -u, --utc          read and print wall-clock times in UTC\n"
	      "      --epoch=DATE   count days from DATE, an ISO date and time as\n"
	      "                       --convert reads it, but in UTC unless it has\n"
	      "                       an offset\n"
	      "      --day-length=SECONDS\n"
	      "                    seconds per day: 1..1000000000 (default 86400)\n"
	      "      --help         display this help and exit\n"
	      "      --version      output version information and exit\n"
	      "\n"
	      "A stamp may leave out digits.  Missing right digits are zeros\n"
	      "(.5 is .5000).  Missing left digits pick the most recent day, up to\n"
	      "today, whose number ends in the digits given; a leading '+' picks the\n"
	      "first such day from today on.  If today is 9761, then 9 is 9759,\n"
	      "+9 is 9769 and .5 is 9761.5000.  HH:MM means that time today.\n"
	      "\n"
	      "An END that leaves out digits is completed from START instead: it is\n"
	      "the first matching time not before START, so .9 - .1 lasts .2000 and\n"
	      "23:00 - 01:00 lasts two hours.\n"
	      "\n"
	      "In the tracker, Enter stamps the time, x removes the last stamp, e\n"
	      "edits it, s saves the log and q quits.  An edited stamp takes any\n"
	      "form above and is completed like an END, from the stamp before it.\n"
	      "The log is rewritten after every change, so it is always current,\n"
	      "and -f LOG reads it back.\n"
	      "\n"
	      "Day 0 comes from --epoch, else from the DAYKEEP_EPOCH environment\n"
	      "variable, else from an 'epoch = DATE' line in\n"
	      "$XDG_CONFIG_HOME/daykeep/config (~/.config/daykeep/config).\n"
	      "Day length comes from --day-length, else DAYKEEP_DAY_LENGTH, else\n"
	      "'day_length = SECONDS' in that file.  Settings resolve independently.\n"
	      "Decimal input rounds to whole seconds; output has four decimals.\n"
	      "Read logs with the epoch and day length used to write them.\n"
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
dk_secs clock_now(void)
{
	const char *env = getenv("DAYKEEP_NOW");
	dk_secs s;
	char *end;
	long long t;

	if (!env || !*env) {
		if (dk_sub((dk_secs)time(NULL), dk_epoch, &s) == 0)
			return s;
		fprintf(stderr, "%s: current time is out of range\n", PROGRAM);
		exit(EXIT_TROUBLE);
	}
	if (*env == '@') {
		errno = 0;
		t = strtoll(env + 1, &end, 10);
		if (end != env + 1 && *end == '\0' && errno == 0
		    && dk_sub(t, dk_epoch, &s) == 0)
			return s;
	} else if (dk_parse_literal(env, &s) == 0) {
		return s;
	}
	fprintf(stderr, "%s: invalid DAYKEEP_NOW '%s'\n", PROGRAM, env);
	exit(EXIT_TROUBLE);
}

/* --- configuration ------------------------------------------------------ */

#define BLANKS " \t\r\n\f\v"

static char *trim(char *s)
{
	char *e;

	s += strspn(s, BLANKS);
	for (e = s + strlen(s); e > s && strchr(BLANKS, e[-1]); e--)
		;
	*e = '\0';
	return s;
}

static void config_error(const char *name, long line, const char *what, const char *v)
{
	fprintf(stderr, "%s: %s:%ld: %s '%s'\n", PROGRAM, name, line, what, v);
	exit(EXIT_TROUBLE);
}

/* $XDG_CONFIG_HOME/daykeep/config, or ~/.config/daykeep/config; NULL if
 * neither variable is usable.  The result is malloc'd. */
static char *config_path(void)
{
	const char *dir = getenv("XDG_CONFIG_HOME"), *sub = "daykeep/config";
	char *path;

	if (!dir || dir[0] != '/') {
		dir = getenv("HOME");
		sub = ".config/daykeep/config";
		if (!dir || !*dir)
			return NULL;
	}
	if (!(path = malloc(strlen(dir) + strlen(sub) + 2))) {
		fprintf(stderr, "%s: %s\n", PROGRAM, strerror(errno));
		exit(EXIT_TROUBLE);
	}
	sprintf(path, "%s/%s", dir, sub);
	return path;
}

/* Read "key = value" lines; '#' starts a comment line.  A missing file is
 * fine; anything wrong in one that exists is an error. */
static void read_config(int need_epoch, int need_length)
{
	char *name = config_path(), *line = NULL, *key, *val, *eq;
	size_t cap = 0;
	long n = 0;
	char *values[2] = { NULL, NULL };
	long lines[2] = { 0, 0 };
	int needs[] = { need_epoch, need_length };
	int (*setters[])(const char *) = { dk_set_epoch, dk_set_day_length };
	const char *errors[] = { "invalid epoch", "invalid day length" };
	int setting, i;
	FILE *f;

	if (!name)
		return;
	if (!(f = fopen(name, "r"))) {
		if (errno == ENOENT) {
			free(name);
			return;
		}
		fprintf(stderr, "%s: %s: %s\n", PROGRAM, name, strerror(errno));
		exit(EXIT_TROUBLE);
	}
	while (getline(&line, &cap, f) != -1) {
		n++;
		key = trim(line);
		if (*key == '\0' || *key == '#')
			continue;
		if (!(eq = strchr(key, '=')))
			config_error(name, n, "invalid line", key);
		*eq = '\0';
		key = trim(key);
		val = trim(eq + 1);
		if (strcmp(key, "epoch") == 0) {
			setting = 0;
		} else if (strcmp(key, "day_length") == 0) {
			setting = 1;
		} else {
			config_error(name, n, "unknown setting", key);
			continue;
		}
		if (needs[setting]) {
			free(values[setting]);
			if (!(values[setting] = strdup(val))) {
				fprintf(stderr, "%s: %s\n", PROGRAM, strerror(errno));
				exit(EXIT_TROUBLE);
			}
			lines[setting] = n;
		}
	}
	if (ferror(f)) {
		fprintf(stderr, "%s: %s: read error: %s\n", PROGRAM, name, strerror(errno));
		exit(EXIT_TROUBLE);
	}
	free(line);
	fclose(f);
	for (i = 0; i < 2; i++) {
		if (values[i] && setters[i](values[i]))
			config_error(name, lines[i], errors[i], values[i]);
		free(values[i]);
	}
	free(name);
}

/* Resolve each setting independently; overridden values need not be valid. */
static void configure(const char *epoch, const char *length)
{
	const char *opts[] = { epoch, length };
	const char *names[] = { "--epoch", "--day-length" };
	const char *envnames[] = { "DAYKEEP_EPOCH", "DAYKEEP_DAY_LENGTH" };
	int (*setters[])(const char *) = { dk_set_epoch, dk_set_day_length };
	int need[2], i;

	for (i = 0; i < 2; i++) {
		const char *env = getenv(envnames[i]);
		const char *value = opts[i] ? opts[i] : env && *env ? env : NULL;
		need[i] = value == NULL;
		if (value && setters[i](value)) {
			if (opts[i]) {
				fprintf(stderr, "%s: invalid argument '%s' for '%s'\n",
				        PROGRAM, value, names[i]);
				try_help();
			}
			fprintf(stderr, "%s: invalid %s '%s'\n", PROGRAM, envnames[i], value);
			exit(EXIT_TROUBLE);
		}
	}
	if (need[0] || need[1])
		read_config(need[0], need[1]);
}

/* --- converting --------------------------------------------------------- */

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

void fmt_dur(char *buf, dk_secs d, enum dk_format fmt)
{
	switch (fmt) {
	case FMT_DECIMAL: dk_fmt_dur(buf, d); break;
	case FMT_HM:      dk_fmt_hm(buf, d); break;
	case FMT_MINUTES: dk_fmt_minutes(buf, d); break;
	}
}

static void print_dur(dk_secs d)
{
	char buf[DK_BUFSZ];

	fmt_dur(buf, d, format);
	puts(buf);
}

static void add_range(dk_secs start, dk_secs end, const struct src *src)
{
	dk_secs duration;
	if (dk_sub(end, start, &duration) || dk_add(total, duration, &total)) {
		bad_input(src, "duration total is out of range");
		return;
	}
	if (!summarize)
		print_dur(total);
}

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
				add_range(start, end, src);
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
			add_range(start, t_now, src);
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
	int earlier = ferror(stdout);   /* a failed fflush already dropped its data */

	if (fclose(stdout) != 0) {
		fprintf(stderr, "%s: write error: %s\n", PROGRAM, strerror(errno));
		_exit(EXIT_TROUBLE);
	}
	if (earlier) {
		fprintf(stderr, "%s: write error\n", PROGRAM);
		_exit(EXIT_TROUBLE);
	}
}

enum { OPT_NOW = 256, OPT_FORMAT, OPT_HM, OPT_EPOCH, OPT_DAY_LENGTH, OPT_HELP, OPT_VERSION };

static const struct option longopts[] = {
	{ "file",      required_argument, NULL, 'f' },
	{ "summarize", no_argument,       NULL, 's' },
	{ "format",    required_argument, NULL, OPT_FORMAT },
	{ "hm",        no_argument,       NULL, OPT_HM },
	{ "now",       no_argument,       NULL, OPT_NOW },
	{ "convert",   no_argument,       NULL, 'c' },
	{ "track",     no_argument,       NULL, 't' },
	{ "append",    required_argument, NULL, 'a' },
	{ "utc",       no_argument,       NULL, 'u' },
	{ "epoch",     required_argument, NULL, OPT_EPOCH },
	{ "day-length", required_argument, NULL, OPT_DAY_LENGTH },
	{ "help",      no_argument,       NULL, OPT_HELP },
	{ "version",   no_argument,       NULL, OPT_VERSION },
	{ NULL, 0, NULL, 0 }
};

int main(int argc, char **argv)
{
	enum { MODE_NONE, MODE_NOW, MODE_CONVERT, MODE_TRACK } mode = MODE_NONE;
	const char *log = NULL, *epoch = NULL, *length = NULL;
	char buf[DK_BUFSZ];
	dk_secs t_now;
	int c, i;

	atexit(close_stdout);
	if (!(files = malloc(argc * sizeof *files))) {
		fprintf(stderr, "%s: %s\n", PROGRAM, strerror(errno));
		return EXIT_TROUBLE;
	}
	while ((c = getopt_long(argc, argv, "a:cf:stu", longopts, NULL)) != -1) {
		switch (c) {
		case OPT_NOW:
		case 'c':
		case 't':
			if (mode != MODE_NONE) {
				fprintf(stderr, "%s: --now, --convert and --track are exclusive\n",
				        PROGRAM);
				try_help();
			}
			mode = c == 'c' ? MODE_CONVERT : c == 't' ? MODE_TRACK : MODE_NOW;
			break;
		case 'a':
			log = optarg;
			break;
		case 'u':
			utc = 1;
			break;
		case OPT_EPOCH:
			epoch = optarg;
			break;
		case OPT_DAY_LENGTH:
			length = optarg;
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
	if (log && mode != MODE_TRACK) {
		fprintf(stderr, "%s: -a works only with --track\n", PROGRAM);
		try_help();
	}
	configure(epoch, length);
	t_now = clock_now();
	switch (mode) {
	case MODE_TRACK:
	case MODE_NOW:
		if (optind < argc) {
			fprintf(stderr, "%s: extra operand '%s'\n", PROGRAM, argv[optind]);
			try_help();
		}
		if (mode == MODE_TRACK)
			return track(log, utc, format);
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
