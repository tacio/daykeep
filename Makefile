# timekeep (hand-built ELF, sum mode) and timekeep-c (freestanding C reference)
CFLAGS = -Os -static -nostdlib -fno-stack-protector -fno-asynchronous-unwind-tables \
         -fno-unwind-tables -fno-ident -fno-pie -no-pie -ffunction-sections \
         -fdata-sections -fcf-protection=none -mno-red-zone -Wall -Wextra
LDFLAGS = -Wl,--gc-sections -Wl,-z,norelro -Wl,--build-id=none -Wl,-z,noseparate-code

all: timekeep timekeep-c

# the .s file contains the whole ELF (headers included); just flatten it
timekeep: timekeep.s
	$(AS) -o $@.o $<
	objcopy -O binary --only-section=.text $@.o $@
	rm -f $@.o
	chmod +x $@

timekeep-c: timekeep.c
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ $<
	strip -s -R .comment -R '.note*' -R '.eh_frame*' $@

size: all
	@wc -c timekeep timekeep-c

test: all
	@./test.sh ./timekeep && ./test.sh ./timekeep-c

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
	rm -f timekeep timekeep-c *.o

.PHONY: all size test verify verify-setup fuzz clean
