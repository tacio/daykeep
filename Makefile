# timekeep (hand-built ELF, sum mode), timekeep-c (freestanding C reference)
# and daykeep (hosted C, decimal time)

# GNU install directories; override on the command line
prefix      = /usr/local
exec_prefix = $(prefix)
bindir      = $(exec_prefix)/bin
datarootdir = $(prefix)/share
mandir      = $(datarootdir)/man
INSTALL         = install
INSTALL_PROGRAM = $(INSTALL)
INSTALL_DATA    = $(INSTALL) -m 644

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

install: daykeep
	$(INSTALL) -d $(DESTDIR)$(bindir)
	$(INSTALL_PROGRAM) daykeep $(DESTDIR)$(bindir)/daykeep

install-strip:
	$(MAKE) INSTALL_PROGRAM='$(INSTALL_PROGRAM) -s' install

uninstall:
	rm -f $(DESTDIR)$(bindir)/daykeep

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

.PHONY: all size test check install install-strip uninstall verify verify-setup fuzz clean
