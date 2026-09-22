# timekeep (hand-built ELF, sum mode), timekeep-c (freestanding C reference)
# and daykeep (hosted C, decimal time)

# Copyright (C) 2026 Tacio Medeiros
#
# Copying and distribution of this file, with or without modification,
# are permitted in any medium without royalty provided the copyright
# notice and this notice are preserved.  This file is offered as-is,
# without any warranty.

PACKAGE = daykeep
VERSION := $(shell sed -n 's/^[#]define VERSION "\(.*\)"$$/\1/p' src/daykeep.c)
distdir = $(PACKAGE)-$(VERSION)

# GNU install directories; override on the command line
prefix      = /usr/local
exec_prefix = $(prefix)
bindir      = $(exec_prefix)/bin
datarootdir = $(prefix)/share
mandir      = $(datarootdir)/man
man1dir     = $(mandir)/man1
infodir     = $(datarootdir)/info
bashcompdir = $(datarootdir)/bash-completion/completions
INSTALL         = install
INSTALL_PROGRAM = $(INSTALL)
INSTALL_DATA    = $(INSTALL) -m 644
INSTALL_INFO    = install-info

# maintainer tools: only needed in a git checkout, since the tarball
# ships the man page and the Info manual
HELP2MAN = help2man
MAKEINFO = makeinfo

# daykeep: CFLAGS is the user's; warnings and feature macros stay on regardless
CFLAGS   = -g -O2
DK_FLAGS = -std=c11 -D_DEFAULT_SOURCE -Wall -Wextra
DK_SRC   = src/daykeep.c src/dktime.c src/track.c

TK_CFLAGS = -Os -static -nostdlib -fno-stack-protector -fno-asynchronous-unwind-tables \
         -fno-unwind-tables -fno-ident -fno-pie -no-pie -ffunction-sections \
         -fdata-sections -fcf-protection=none -mno-red-zone -Wall -Wextra
TK_LDFLAGS = -Wl,--gc-sections -Wl,-z,norelro -Wl,--build-id=none -Wl,-z,noseparate-code

all: timekeep timekeep-c daykeep

# the .s file contains the whole ELF (headers included); just flatten it
timekeep: timekeep.s
	$(AS) -o $@.o $<
	objcopy -O binary --only-section=.text $@.o $@
	rm -f $@.o
	chmod +x $@

timekeep-c: timekeep.c
	$(CC) $(TK_CFLAGS) $(TK_LDFLAGS) -o $@ $<
	strip -s -R .comment -R '.note*' -R '.eh_frame*' $@

daykeep: $(DK_SRC) src/dktime.h src/daykeep.h
	$(CC) $(DK_FLAGS) $(CPPFLAGS) $(CFLAGS) $(LDFLAGS) -o $@ $(DK_SRC)

size: all
	@wc -c timekeep timekeep-c

test: timekeep timekeep-c
	@./test.sh ./timekeep && ./test.sh ./timekeep-c

check: test daykeep
	@tests/daykeep-core.sh ./daykeep
	@tests/daykeep-sum.sh ./daykeep ./timekeep
	@tests/daykeep-track.sh ./daykeep

# daykeep docs: the man page is made from --help, the manual from Texinfo
doc: doc/daykeep.1 doc/daykeep.info
info: doc/daykeep.info

# built from the sources rather than the binary, so an unpacked tarball
# (whose man page is as new as its sources) never needs help2man
doc/daykeep.1: src/daykeep.c doc/daykeep.h2m | daykeep
	$(HELP2MAN) --include=doc/daykeep.h2m --output=$@.tmp ./daykeep
	mv $@.tmp $@

doc/version.texi: src/daykeep.c doc/daykeep.texi
	{ echo '@set VERSION $(VERSION)'; \
	  echo "@set UPDATED $$(LC_ALL=C date -u -r doc/daykeep.texi '+%-d %B %Y')"; } > $@

doc/daykeep.info: doc/daykeep.texi doc/version.texi
	$(MAKEINFO) -I doc -o $@ doc/daykeep.texi

install: daykeep doc
	$(INSTALL) -d $(DESTDIR)$(bindir) $(DESTDIR)$(man1dir) $(DESTDIR)$(infodir) \
		$(DESTDIR)$(bashcompdir)
	$(INSTALL_PROGRAM) daykeep $(DESTDIR)$(bindir)/daykeep
	$(INSTALL_DATA) doc/daykeep.1 $(DESTDIR)$(man1dir)/daykeep.1
	$(INSTALL_DATA) doc/daykeep.info $(DESTDIR)$(infodir)/daykeep.info
	$(INSTALL_DATA) completion/daykeep $(DESTDIR)$(bashcompdir)/daykeep
	-if ($(INSTALL_INFO) --version) >/dev/null 2>&1; then \
		$(INSTALL_INFO) --info-dir=$(DESTDIR)$(infodir) $(DESTDIR)$(infodir)/daykeep.info; \
	fi

install-strip:
	$(MAKE) INSTALL_PROGRAM='$(INSTALL_PROGRAM) -s' install

# leaves the Info 'dir' file, which other manuals share
uninstall:
	-if ($(INSTALL_INFO) --version) >/dev/null 2>&1; then \
		$(INSTALL_INFO) --info-dir=$(DESTDIR)$(infodir) --delete \
			$(DESTDIR)$(infodir)/daykeep.info; \
	fi
	rm -f $(DESTDIR)$(bindir)/daykeep $(DESTDIR)$(man1dir)/daykeep.1 \
		$(DESTDIR)$(infodir)/daykeep.info $(DESTDIR)$(bashcompdir)/daykeep

DISTFILES = AUTHORS COPYING NEWS README.md THANKS Makefile \
	timekeep.s timekeep.c test.sh $(DK_SRC) src/dktime.h src/daykeep.h \
	tests/daykeep-core.sh tests/daykeep-sum.sh tests/daykeep-track.sh \
	tests/distcheck.sh doc/daykeep.texi doc/version.texi doc/daykeep.info \
	doc/daykeep.h2m doc/daykeep.1 completion/daykeep \
	verify/README.md verify/spec.py verify/prove.py

dist: $(DISTFILES)
	rm -rf $(distdir)
	mkdir $(distdir)
	cp -p --parents $(DISTFILES) $(distdir)
	tar --sort=name --owner=0 --group=0 --numeric-owner -czf $(distdir).tar.gz $(distdir)
	rm -rf $(distdir)
	@echo "$(distdir).tar.gz is ready"

# build, check, install and uninstall the tarball, without the doc tools
distcheck: dist
	tests/distcheck.sh $(distdir).tar.gz

# formal proof over the shipped bytes (needs a one-time: make verify-setup)
# memory-capped in its own scope so a runaway exploration can't OOM the terminal
VERIFY_MEM ?= 12G
verify: timekeep
	systemd-run --user --scope --quiet -p MemoryMax=$(VERIFY_MEM) -p MemorySwapMax=0 \
		verify/.venv/bin/python -u verify/prove.py

verify-setup:
	uv venv verify/.venv
	uv pip install -q -p verify/.venv angr z3-solver

fuzz: all
	verify/.venv/bin/python verify/fuzz.py

clean:
	rm -f timekeep timekeep-c daykeep *.o

# also removes what the tarball ships but a checkout can regenerate
maintainer-clean: clean
	rm -f doc/daykeep.1 doc/daykeep.info doc/version.texi $(PACKAGE)-*.tar.gz

.PHONY: all size test check doc info install install-strip uninstall dist distcheck \
	verify verify-setup fuzz clean maintainer-clean
