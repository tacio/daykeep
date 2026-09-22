/* timekeep: track work sessions, or sum "HH:MM - HH:MM" ranges.
 *
 *   timekeep <entries...>   sum the ranges given as arguments, print running
 *                           totals (e.g. `timekeep 9:00 - 10:00 11:00 12:00`)
 *   timekeep                (no args) start the interactive tracker; [s] and
 *                           quitting save the table to ./timekeep-<epoch>.txt
 *
 * Freestanding x86_64 Linux: no libc, raw syscalls.  This file is the readable
 * reference for the hand-built ELF in timekeep.s; the two must behave alike.
 */

typedef unsigned long u64;
typedef long i64;

static inline long sys1(long n, long a)
{ long r; __asm__ volatile("syscall":"=a"(r):"0"(n),"D"(a):"rcx","r11","memory"); return r; }
static inline long sys3(long n, long a, long b, long c)
{ long r; __asm__ volatile("syscall":"=a"(r):"0"(n),"D"(a),"S"(b),"d"(c):"rcx","r11","memory"); return r; }

#define SYS_read 0
#define SYS_write 1
#define SYS_open 2
#define SYS_close 3
#define SYS_ioctl 16
#define SYS_exit 60
#define SYS_time 201
#define TCGETS 0x5401
#define TCSETS 0x5402
#define O_WRONLY 01
#define O_CREAT  0100
#define O_TRUNC  01000
#define ICANON 0000002
#define ECHO   0000010
#define ISIG   0000001

/* --- number formatting -------------------------------------------------- */

/* write v in decimal ending at p (least significant last); returns new start */
static char *putdec(char *p, unsigned v)
{
	do *--p = '0' + v % 10; while (v /= 10);
	return p;
}

/* render "<h>h <m>m" of a minute count into buf; returns length written */
static int fmt_hm(char *buf, unsigned t)
{
	char tmp[24], *end = tmp + sizeof tmp, *p = end;
	int len, i;
	*--p = 'm';
	p = putdec(p, t % 60);
	*--p = ' ';
	*--p = 'h';
	p = putdec(p, t / 60);
	len = end - p;
	for (i = 0; i < len; i++)
		buf[i] = p[i];
	return len;
}

/* --- entry arithmetic --------------------------------------------------- */

/* minutes for end-start, wrapping past midnight; start,end in [0,1440) */
static unsigned entry_dur(unsigned start, unsigned end)
{
	int d = (int)end - (int)start;
	if (d < 0)
		d += 1440;
	return (unsigned)d;
}

/* --- sum mode ----------------------------------------------------------- */

/* Streaming parser mirroring the register machine in timekeep.s.  Every
 * non-digit is a separator; four numbers make an entry (end-start), whose
 * running total prints on completion; a newline abandons a partial entry. */
static const int WEIGHTS = 0x013CFFC4;   /* signed bytes: -60, -1, 60, 1 */

struct sum { int weights, cur, line, indig; unsigned total; };

static void sum_reset(struct sum *st)
{ st->weights = WEIGHTS; st->line = 0; st->cur = 0; st->indig = 0; }

static void sum_emit(struct sum *st)
{
	char buf[24];
	int len, d = st->line % 1440;
	if (d < 0)
		d += 1440;
	st->total += (unsigned)d;
	len = fmt_hm(buf, st->total);
	buf[len] = '\n';
	sys3(SYS_write, 1, (long)buf, len + 1);
}

static void sum_byte(struct sum *st, int c)
{
	if (c >= '0' && c <= '9') {
		st->cur = st->cur * 10 + (c - '0');
		st->indig = 1;
		return;
	}
	if (st->indig) {
		st->line += (signed char)st->weights * st->cur;
		st->weights = (int)((unsigned)st->weights >> 8);
		st->cur = 0;
		st->indig = 0;
		if (st->weights == 0) {          /* four numbers: entry complete */
			sum_emit(st);
			sum_reset(st);
			return;
		}
	}
	if (c == '\n')
		sum_reset(st);
}

static void run_sum(int argc, char **argv)
{
	struct sum st;
	int a;
	sum_reset(&st);
	for (a = 1; a < argc; a++) {
		char *p = argv[a];
		while (*p)
			sum_byte(&st, (unsigned char)*p++);
		sum_byte(&st, ' ');              /* the NUL between args separates */
	}
	sys3(SYS_exit, 0, 0, 0);
}

/* --- timezone ----------------------------------------------------------- */

static unsigned be32(const unsigned char *p)
{ return (unsigned)p[0] << 24 | (unsigned)p[1] << 16 | (unsigned)p[2] << 8 | p[3]; }

/* TZif v1 lookup: gmt offset in seconds for `now`, or 0 on any malformed data */
static int tz_offset(const unsigned char *tz, long len, i64 now)
{
	u64 timecnt, typecnt, base, i, idx = 0;
	if (len < 44 || tz[0] != 'T' || tz[1] != 'Z' || tz[2] != 'i' || tz[3] != 'f')
		return 0;
	timecnt = be32(tz + 32);
	typecnt = be32(tz + 36);
	if (typecnt == 0)
		return 0;
	/* header 44 + times 4*timecnt + type indices 1*timecnt + ttinfo 6*typecnt */
	base = (u64)44 + (u64)5 * timecnt + (u64)6 * typecnt;
	if (base > (u64)len)
		return 0;
	for (i = 0; i < timecnt; i++) {
		i64 t = (i64)(int)be32(tz + 44 + 4 * i);
		if (t <= now)
			idx = tz[44 + 4 * timecnt + i];
		else
			break;
	}
	if (idx >= typecnt)
		return 0;
	return (int)be32(tz + 44 + 5 * timecnt + 6 * idx);
}

static unsigned minute_of_day(i64 secs)
{
	i64 s = secs % 86400;
	if (s < 0)
		s += 86400;
	return (unsigned)(s / 60);
}

/* --- interactive tracker ------------------------------------------------ */

#define NMAX 250

struct track {
	unsigned char termios[60];   /* saved TCGETS blob */
	int raw;                     /* did we switch the tty to raw mode? */
	int clr;                     /* is stdout a tty (clear the screen)? */
	unsigned char tz[4096];
	long tzlen;
	unsigned short times[NMAX];
	int n;
	char msg[80];
	int msglen;
	char path[32];               /* save file name, NUL-terminated */
	int pathlen;
	int outfd;                   /* render's fd: 1, or the save file */
};

static void out(const char *s, long len) { sys3(SYS_write, 1, (long)s, len); }
static void outs(const char *s) { const char *e = s; while (*e) e++; out(s, e - s); }
static char *puts_(char *p, const char *s) { while (*s) *p++ = *s++; return p; }
static char *put2(char *p, unsigned v) { *p++ = '0' + v / 10 % 10; *p++ = '0' + v % 10; return p; }
static char *puthm(char *p, unsigned m) { p = put2(p, m / 60); *p++ = ':'; return put2(p, m % 60); }
static char *putdur(char *p, unsigned d) { return p + fmt_hm(p, d); }

static void restore(struct track *t)
{ if (t->raw) sys3(SYS_ioctl, 0, TCSETS, (long)t->termios); }

static void save(struct track *t);

static void quit(struct track *t)
{
	if (t->n) {                  /* save on the way out, and say where it went */
		save(t);
		out(t->msg, t->msglen);
		outs("\n");
	}
	restore(t);
	sys3(SYS_exit, 0, 0, 0);
}

static unsigned now_minute(struct track *t)
{
	i64 now = sys1(SYS_time, 0);
	return minute_of_day(now + tz_offset(t->tz, t->tzlen, now));
}

/* render the screen; when t->outfd is a save file, write only the entries and
 * the total, to that fd */
static void render(struct track *t)
{
	char buf[128], *p;
	int i;
	if (t->outfd == 1 && t->clr)
		outs("\033[H\033[J");
	for (i = 0; i < t->n; i += 2) {
		unsigned idx = i / 2 + 1;
		p = buf;
		if (idx < 10) *p++ = ' ';                       /* right-align to width 2 */
		{ char d[8], *q = putdec(d + 8, idx); while (q < d + 8) *p++ = *q++; }
		*p++ = ' '; *p++ = ' ';
		p = puthm(p, t->times[i]);
		if (i + 1 < t->n) {
			p = puts_(p, " - ");
			p = puthm(p, t->times[i + 1]);
			p = puts_(p, "   ");
			p = putdur(p, entry_dur(t->times[i], t->times[i + 1]));
		} else {
			p = puts_(p, " - ...     (running)");
		}
		*p++ = '\n';
		sys3(SYS_write, t->outfd, (long)buf, p - buf);
	}
	{
		unsigned tot = 0;
		for (i = 0; i + 1 < t->n; i += 2)
			tot += entry_dur(t->times[i], t->times[i + 1]);
		p = puts_(buf, "total ");
		p = putdur(p, tot);
		*p++ = '\n';
		sys3(SYS_write, t->outfd, (long)buf, p - buf);
	}
	if (t->outfd != 1)
		return;
	if (t->msglen) {
		out(t->msg, t->msglen);
		outs("\n");
		t->msglen = 0;
	}
	outs("[enter] stamp  [x] remove last  [e] edit last  [s] save  [q] quit\n");
}

/* write the entries and total to t->path (created or truncated), then leave
 * "saved <path>" or "cannot save <path>" as the status message */
static void save(struct track *t)
{
	char *p;
	int i, fd = sys3(SYS_open, (long)t->path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
	if (fd < 0) {
		p = puts_(t->msg, "cannot save ");
	} else {
		t->outfd = fd;
		render(t);
		sys1(SYS_close, fd);
		t->outfd = 1;
		p = puts_(t->msg, "saved ");
	}
	for (i = 0; i < t->pathlen; i++)
		*p++ = t->path[i];
	t->msglen = p - t->msg;
}

/* validate "HH:MM" (1-2 digit hour); minute-of-day or -1 */
static int parse_hm(const char *s, int len)
{
	int hd, hh = 0, m, i;
	if (len < 4 || len > 5)
		return -1;
	hd = (s[1] == ':') ? 1 : 2;
	if (len != hd + 3 || s[hd] != ':')
		return -1;
	for (i = 0; i < hd; i++) {
		if (s[i] < '0' || s[i] > '9') return -1;
		hh = hh * 10 + s[i] - '0';
	}
	if (hh > 23) return -1;
	if (s[hd + 1] < '0' || s[hd + 1] > '5') return -1;
	if (s[hd + 2] < '0' || s[hd + 2] > '9') return -1;
	m = (s[hd + 1] - '0') * 10 + (s[hd + 2] - '0');
	return hh * 60 + m;
}

static void edit_last(struct track *t)
{
	char in[16];
	int len, prev = t->times[t->n - 1], val;
	len = puthm(in, (unsigned)prev) - in;
	out("edit: ", 6);
	out(in, len);
	for (;;) {
		char c;
		if (sys3(SYS_read, 0, (long)&c, 1) <= 0)
			return;
		if (c == '\n' || c == '\r')
			break;
		if (c == 0x7f || c == 8) {
			if (len) { len--; out("\b \b", 3); }
			continue;
		}
		if (len < 15) { in[len++] = c; out(&c, 1); }
	}
	outs("\n");
	val = parse_hm(in, len);
	if (val >= 0) {
		t->times[t->n - 1] = (unsigned short)val;
	} else {
		char *p = puts_(t->msg, "invalid time '");
		int i;
		for (i = 0; i < len; i++) *p++ = in[i];
		p = puts_(p, "' (want HH:MM), kept ");
		p = puthm(p, (unsigned)prev);
		t->msglen = p - t->msg;
	}
}

static void run_track(void)
{
	static struct track t;   /* zero-initialised */
	unsigned char probe[60];
	int fd;

	if (sys3(SYS_ioctl, 0, TCGETS, (long)t.termios) == 0) {
		unsigned char work[60];
		int i;
		for (i = 0; i < 60; i++) work[i] = t.termios[i];
		work[12] &= ~(unsigned char)(ICANON | ECHO | ISIG);  /* c_lflag low byte */
		sys3(SYS_ioctl, 0, TCSETS, (long)work);
		t.raw = 1;
	}
	t.clr = (sys3(SYS_ioctl, 1, TCGETS, (long)probe) == 0);
	t.outfd = 1;

	{       /* save file name: "timekeep-" + decimal epoch + ".txt" */
		char d[12], *q = putdec(d + 12, (unsigned)sys1(SYS_time, 0));
		char *p = puts_(t.path, "timekeep-");
		while (q < d + 12) *p++ = *q++;
		p = puts_(p, ".txt");
		*p = 0;
		t.pathlen = p - t.path;
	}

	fd = sys3(SYS_open, (long)"/etc/localtime", 0 /*O_RDONLY*/, 0);
	if (fd >= 0) {
		t.tzlen = sys3(SYS_read, fd, (long)t.tz, sizeof t.tz);
		if (t.tzlen < 0) t.tzlen = 0;
		sys1(SYS_close, fd);
	}

	for (;;) {
		char key;
		render(&t);
		if (sys3(SYS_read, 0, (long)&key, 1) <= 0)
			quit(&t);
		switch (key) {
		case '\n': case '\r':
			if (t.n < NMAX)
				t.times[t.n++] = (unsigned short)now_minute(&t);
			break;
		case 'x':
			if (t.n) t.n--;
			break;
		case 'e':
			if (t.n) edit_last(&t);
			break;
		case 's':
			if (t.n) {
				save(&t);
			} else {
				char *p = puts_(t.msg, "nothing to save");
				t.msglen = p - t.msg;
			}
			break;
		case 'q': case 3: case 4:
			quit(&t);
		}
	}
}

__attribute__((used)) static void go(long argc, char **argv)
{
	if (argc > 1)
		run_sum((int)argc, argv);   /* never returns */
	run_track();                        /* never returns */
}

/* raw entry: the kernel puts argc at [rsp], argv at rsp+8, no return address */
__attribute__((naked, noreturn)) void _start(void)
{
	__asm__ volatile(
		"mov (%rsp), %rdi\n"        /* argc */
		"lea 8(%rsp), %rsi\n"       /* argv */
		"and $-16, %rsp\n"          /* 16-byte align for the call */
		"call go\n");
}
