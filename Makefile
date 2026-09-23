# daykeep (hosted C, decimal time)

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

all: daykeep

daykeep: $(DK_SRC) src/dktime.h src/daykeep.h
	$(CC) $(DK_FLAGS) $(CPPFLAGS) $(CFLAGS) $(LDFLAGS) -o $@ $(DK_SRC)

test: daykeep
	@tests/daykeep-core.sh ./daykeep
	@tests/daykeep-sum.sh ./daykeep
	@tests/daykeep-track.sh ./daykeep

check: test

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

DISTFILES = AUTHORS COPYING MANIFESTO.md NEWS README.md THANKS Makefile \
	assets/gone-with-the-wind.webp \
	$(DK_SRC) src/dktime.h src/daykeep.h \
	tests/daykeep-core.sh tests/daykeep-sum.sh tests/daykeep-track.sh \
	tests/distcheck.sh doc/daykeep.texi doc/version.texi doc/daykeep.info \
	doc/daykeep.h2m doc/daykeep.1 completion/daykeep \
	verify/README.md verify/dkspec.py verify/dkprove.py \
	verify/fuzz.py verify/dk_libfuzzer.c

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

# formal proof of daykeep's decimal-time rules (needs a one-time:
# make verify-setup); memory-capped in its own scope so a runaway solver
# cannot OOM the terminal
VERIFY_MEM ?= 12G
VERIFY_RUN = systemd-run --user --scope --quiet -p MemoryMax=$(VERIFY_MEM) \
	-p MemorySwapMax=0 verify/.venv/bin/python -u
verify:
	$(VERIFY_RUN) verify/dkprove.py

verify-setup:
	uv venv verify/.venv
	uv pip install -q -p verify/.venv angr z3-solver

# differential and robustness fuzzing (needs verify/.venv); seeded, so
# reproducible: FUZZ_ITERS=3000 runs longer, FUZZ_SEED=n tries other cases
fuzz: daykeep
	verify/.venv/bin/python verify/fuzz.py

# the same, with daykeep built with AddressSanitizer and UBSan
SAN_FLAGS = -g -O1 -fsanitize=address,undefined -fno-sanitize-recover=all \
	-fno-omit-frame-pointer
daykeep-asan: $(DK_SRC) src/dktime.h src/daykeep.h
	$(CC) $(DK_FLAGS) $(SAN_FLAGS) -o $@ $(DK_SRC)

fuzz-asan: daykeep-asan
	verify/.venv/bin/python verify/fuzz.py --daykeep ./daykeep-asan

# coverage-guided fuzzing of the time library's parsers (needs clang)
CLANG = clang
FUZZ_RUNS = 2000000
dk-libfuzzer: verify/dk_libfuzzer.c src/dktime.c src/dktime.h
	$(CLANG) -std=c11 -D_DEFAULT_SOURCE -Isrc $(SAN_FLAGS) -fsanitize=fuzzer \
		-o $@ verify/dk_libfuzzer.c src/dktime.c

fuzz-libfuzzer: dk-libfuzzer
	./dk-libfuzzer -runs=$(FUZZ_RUNS) -seed=1

clean:
	rm -f daykeep daykeep-asan dk-libfuzzer *.o

# also removes what the tarball ships but a checkout can regenerate
maintainer-clean: clean
	rm -f doc/daykeep.1 doc/daykeep.info doc/version.texi $(PACKAGE)-*.tar.gz

.PHONY: all test check doc info install install-strip uninstall dist distcheck \
	verify verify-setup fuzz fuzz-asan fuzz-libfuzzer clean maintainer-clean
