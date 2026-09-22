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
