/* daykeep --track: the interactive tracker.
 *
 * Enter stamps the current time; stamps alternate between the start and the
 * end of an entry.  With -a LOG the session's entries are kept in LOG, one
 * range per line ("START - END", or "START -" while an entry is open), and
 * the file is rewritten after every change, so it is always up to date: a
 * signal or a crash loses nothing.  Only the lines this session owns are
 * rewritten (see log_base); older lines are never touched.  If LOG ends in
 * an open entry, the tracker picks it up as its first stamp.
 */
#include "daykeep.h"

#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <termios.h>
#include <unistd.h>

static int utc;
static enum dk_format format;

static dk_secs *stamps;         /* starts at even indexes, ends at odd ones */
static size_t n, cap;
static char msg[256];           /* status line, shown once */

static struct termios saved_tty;
static int raw;                 /* did we put the terminal in raw mode? */
static int clear;               /* is stdout a terminal (clear the screen)? */

static const char *log_name;    /* -a LOG */
static int log_fd = -1;
static off_t log_base;          /* where this session's lines start */
static int log_nl;              /* LOG lacks a final newline: write one first */
static int log_errno;           /* the last write to LOG failed with this */

static volatile sig_atomic_t caught;
static int read_errno;

static void die(const char *what)
{
	fprintf(stderr, "%s: %s: %s\n", PROGRAM, what, strerror(errno));
	exit(EXIT_TROUBLE);
}

static void push(dk_secs s)
{
	if (n == cap) {
		cap = cap ? cap * 2 : 16;
		if (!(stamps = realloc(stamps, cap * sizeof *stamps)))
			die("tracker");
	}
	stamps[n++] = s;
}

/* --- terminal ------------------------------------------------------------ */

static void restore_tty(void)
{
	if (raw)
		tcsetattr(STDIN_FILENO, TCSADRAIN, &saved_tty);
}

/* keys arrive one at a time, unechoed, and ^C is a key rather than SIGINT */
static void raw_tty(void)
{
	struct termios t;

	if (tcgetattr(STDIN_FILENO, &saved_tty) != 0)
		return;
	t = saved_tty;
	t.c_lflag &= ~(ICANON | ECHO | ISIG);
	t.c_cc[VMIN] = 1;
	t.c_cc[VTIME] = 0;
	if (tcsetattr(STDIN_FILENO, TCSADRAIN, &t) == 0) {
		raw = 1;
		atexit(restore_tty);
	}
}

static void on_signal(int sig)
{
	caught = sig;
}

/* no SA_RESTART: a signal interrupts the blocking read, and the key loop
 * then leaves by the normal path */
static void catch_signals(void)
{
	static const int sigs[] = { SIGHUP, SIGINT, SIGTERM };
	struct sigaction sa;
	size_t i;

	memset(&sa, 0, sizeof sa);
	sa.sa_handler = on_signal;
	sigemptyset(&sa.sa_mask);
	for (i = 0; i < sizeof sigs / sizeof *sigs; i++)
		sigaction(sigs[i], &sa, NULL);
}

/* the next input byte, or -1 at end of input, on a read error or once a
 * signal was caught */
static int key(void)
{
	unsigned char c;
	ssize_t r;

	do {
		if (caught)
			return -1;
		r = read(STDIN_FILENO, &c, 1);
	} while (r < 0 && errno == EINTR);
	if (r < 0)
		read_errno = errno;
	return r == 1 ? c : -1;
}

/* --- the log --------------------------------------------------------------- */

static int blank(int c)
{
	return c == ' ' || c == '\t' || c == '\r' || c == '\n' || c == '\f' || c == '\v';
}

/* LINE is an open entry, "START -"; set *START */
static int open_entry(char *line, dk_secs *start)
{
	char *end = line + strlen(line);

	while (end > line && blank(end[-1]))
		end--;
	if (end == line || end[-1] != '-')
		return 0;
	end--;
	while (end > line && blank(end[-1]))
		end--;
	*end = '\0';
	line += strspn(line, " \t");
	return dk_parse_stamp(line, clock_now(), utc, start) == 0;
}

static void log_open(void)
{
	char tail[256], *nl;
	struct stat st;
	off_t off;
	ssize_t len;
	size_t start;
	dk_secs s;

	if ((log_fd = open(log_name, O_RDWR | O_CREAT | O_CLOEXEC, 0666)) < 0)
		die(log_name);
	if (flock(log_fd, LOCK_EX | LOCK_NB) != 0) {
		if (errno != EWOULDBLOCK)
			die(log_name);
		fprintf(stderr, "%s: %s: in use by another tracker\n", PROGRAM, log_name);
		exit(EXIT_TROUBLE);
	}
	if (fstat(log_fd, &st) != 0)
		die(log_name);
	log_base = st.st_size;
	if (log_base == 0)
		return;

	/* read the last line; if it is an open entry, it becomes ours */
	off = log_base > (off_t)sizeof tail - 1 ? log_base - (off_t)(sizeof tail - 1) : 0;
	if ((len = pread(log_fd, tail, (size_t)(log_base - off), off)) < 0)
		die(log_name);
	if (len != log_base - off) {
		errno = EIO;
		die(log_name);
	}
	tail[len] = '\0';
	log_nl = tail[len - 1] != '\n';
	while (len > 0 && tail[len - 1] == '\n')
		tail[--len] = '\0';
	if ((nl = strrchr(tail, '\n')))
		start = (size_t)(nl + 1 - tail);
	else if (off == 0)
		start = 0;
	else
		return;                         /* a longer line: not an entry */
	if (open_entry(tail + start, &s)) {
		push(s);
		log_base = off + (off_t)start;
		log_nl = 0;
		snprintf(msg, sizeof msg, "resumed the open entry in %s", log_name);
	}
}

static int write_all(int fd, const char *p, size_t len, off_t off)
{
	ssize_t w;

	while (len > 0) {
		if ((w = pwrite(fd, p, len, off)) < 0) {
			if (errno == EINTR)
				continue;
			return -1;
		}
		p += w;
		len -= (size_t)w;
		off += w;
	}
	return 0;
}

/* rewrite this session's lines of the log */
static void log_write(void)
{
	char a[DK_BUFSZ], *buf = NULL;
	size_t len = 0, i;
	FILE *m;

	if (log_fd < 0)
		return;
	if (!(m = open_memstream(&buf, &len)))
		die("tracker");
	if (log_nl && n > 0)
		fputc('\n', m);
	for (i = 0; i < n; i++) {
		dk_fmt_stamp(a, stamps[i]);
		fprintf(m, i % 2 ? " - %s\n" : "%s", a);
	}
	if (n % 2)
		fputs(" -\n", m);
	if (fclose(m) != 0)
		die("tracker");
	if (write_all(log_fd, buf, len, log_base) == 0
	    && ftruncate(log_fd, log_base + (off_t)len) == 0) {
		log_errno = 0;
	} else {
		log_errno = errno;
		snprintf(msg, sizeof msg, "cannot write %s: %s", log_name, strerror(errno));
	}
	free(buf);
}

/* --- screen and keys ------------------------------------------------------- */

static void render(void)
{
	char a[DK_BUFSZ], b[DK_BUFSZ], d[DK_BUFSZ];
	dk_secs total = 0;
	size_t i;

	if (clear)
		fputs("\033[H\033[J", stdout);
	for (i = 0; i < n; i += 2) {
		dk_fmt_stamp(a, stamps[i]);
		if (i + 1 < n) {
			dk_fmt_stamp(b, stamps[i + 1]);
			fmt_dur(d, stamps[i + 1] - stamps[i], format);
			total += stamps[i + 1] - stamps[i];
			printf("%2zu  %s - %s   %s\n", i / 2 + 1, a, b, d);
		} else {
			printf("%2zu  %s - ...          (running)\n", i / 2 + 1, a);
		}
	}
	fmt_dur(d, total, format);
	printf("total %s\n", d);
	if (*msg) {
		printf("%s\n", msg);
		*msg = '\0';
	}
	fputs("[enter] stamp  [x] remove last  [e] edit last  [s] save  [q] quit\n", stdout);
	fflush(stdout);
}

static void rubout(size_t *len, size_t to)
{
	for (; *len > to; --*len)
		fputs("\b \b", stdout);
}

/* Edit the last stamp on a one-line editor, prefilled with its value.  The
 * first stamp is completed from today, like a stamp operand; every later one
 * from the stamp before it, like a range END. */
static void edit_last(void)
{
	char line[DK_BUFSZ], kept[DK_BUFSZ];
	size_t len = (size_t)dk_fmt_stamp(line, stamps[n - 1]);
	dk_secs s;
	int c, r;

	printf("edit: %s", line);
	for (;;) {
		fflush(stdout);
		c = key();
		if (c == -1 || c == 3 || c == 27) {     /* ^C, Esc: cancel */
			putchar('\n');
			if (c != -1)
				snprintf(msg, sizeof msg, "edit cancelled");
			return;
		}
		if (c == '\n' || c == '\r')
			break;
		if (c == 0x7f || c == '\b')
			rubout(&len, len ? len - 1 : 0);
		else if (c == 0x15)                     /* ^U */
			rubout(&len, 0);
		else if (c >= ' ' && c < 0x7f && len < 31) {
			line[len++] = (char)c;
			putchar(c);
		}
	}
	putchar('\n');
	line[len] = '\0';

	if (n == 1)
		r = dk_parse_stamp(line, clock_now(), utc, &s);
	else
		r = dk_parse_end(line, stamps[n - 2], utc, &s);
	if (r == 0) {
		stamps[n - 1] = s;
		log_write();
		return;
	}
	dk_fmt_stamp(kept, stamps[n - 1]);
	if (r == -2)
		snprintf(msg, sizeof msg, "'%s' is before the stamp above it, kept %s", line, kept);
	else
		snprintf(msg, sizeof msg, "invalid stamp '%s', kept %s", line, kept);
}

static void save(void)
{
	if (log_fd < 0) {
		snprintf(msg, sizeof msg, "no log to save to (start with -a LOG)");
		return;
	}
	log_write();
	if (!log_errno)
		snprintf(msg, sizeof msg, "saved %s", log_name);
}

int track(const char *log, int use_utc, enum dk_format fmt)
{
	int c, status = EXIT_OK;

	utc = use_utc;
	format = fmt;
	log_name = log;
	if (log)
		log_open();
	raw_tty();
	clear = isatty(STDOUT_FILENO);
	catch_signals();

	for (;;) {
		render();
		switch (c = key()) {
		case '\n': case '\r':
			push(clock_now());
			log_write();
			break;
		case 'x':
			if (n) {
				n--;
				log_write();
			}
			break;
		case 'e':
			if (n)
				edit_last();
			break;
		case 's':
			save();
			break;
		}
		if (c == -1 || c == 'q' || c == 3 || c == 4)    /* ^C, ^D */
			break;
	}

	if (read_errno) {
		fprintf(stderr, "%s: read error: %s\n", PROGRAM, strerror(read_errno));
		status = EXIT_TROUBLE;
	}
	if (log_errno) {
		fprintf(stderr, "%s: %s: %s\n", PROGRAM, log_name, strerror(log_errno));
		status = EXIT_TROUBLE;
	}
	if (log_fd >= 0 && close(log_fd) != 0) {
		fprintf(stderr, "%s: %s: %s\n", PROGRAM, log_name, strerror(errno));
		status = EXIT_TROUBLE;
	}
	if (caught) {           /* the log is already current: die by the signal */
		restore_tty();
		fflush(stdout);
		signal(caught, SIG_DFL);
		raise(caught);
	}
	free(stamps);
	return status;
}
