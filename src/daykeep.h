/* daykeep: what the command line (daykeep.c) and the tracker (track.c)
 * share. */
#ifndef DAYKEEP_H
#define DAYKEEP_H

#include "dktime.h"

#define PROGRAM "daykeep"

/* exit statuses */
enum { EXIT_OK = 0, EXIT_BADINPUT = 1, EXIT_TROUBLE = 2 };

/* how durations are printed (--format) */
enum dk_format { FMT_DECIMAL, FMT_HM, FMT_MINUTES };

/* the current time, or DAYKEEP_NOW if set */
dk_secs clock_now(void);

/* format duration D as FMT into BUF (DK_BUFSZ bytes) */
void fmt_dur(char *buf, dk_secs d, enum dk_format fmt);

/* run the interactive tracker (--track); LOG is the -a file or NULL.
 * Returns an exit status, or re-raises a caught signal. */
int track(const char *log, int utc, enum dk_format fmt);

#endif
