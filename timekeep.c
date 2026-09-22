/* timekeep: sum "HH:MM - HH:MM" ranges given as arguments.
 *
 *   timekeep <entries...>   print the running total after each entry
 *                           (e.g. `timekeep 9:00 - 10:00 11:00 12:00`)
 *   timekeep                (no args) nothing to sum; exit 0
 *
 * Freestanding x86_64 Linux: no libc, raw syscalls.  This file is the readable
 * reference for the hand-built ELF in timekeep.s; the two must behave alike.
 */

static inline long sys3(long n, long a, long b, long c)
{ long r; __asm__ volatile("syscall":"=a"(r):"0"(n),"D"(a),"S"(b),"d"(c):"rcx","r11","memory"); return r; }

#define SYS_write 1
#define SYS_exit 60

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

__attribute__((used)) static void run_sum(long argc, char **argv)
{
	struct sum st;
	long a;
	sum_reset(&st);
	for (a = 1; a < argc; a++) {
		char *p = argv[a];
		while (*p)
			sum_byte(&st, (unsigned char)*p++);
		sum_byte(&st, ' ');              /* the NUL between args separates */
	}
	sys3(SYS_exit, 0, 0, 0);
}

/* raw entry: the kernel puts argc at [rsp], argv at rsp+8, no return address */
__attribute__((naked, noreturn)) void _start(void)
{
	__asm__ volatile(
		"mov (%rsp), %rdi\n"        /* argc */
		"lea 8(%rsp), %rsi\n"       /* argv */
		"and $-16, %rsp\n"          /* 16-byte align for the call */
		"call run_sum\n");
}
