# timekeep: track work sessions, or sum "HH:MM - HH:MM" ranges.
#   timekeep <entries...>   sum ranges from argv, print running totals
#   timekeep                (no args) interactive tracker
#
# Hand-built x86_64 Linux ELF (flat binary, single .text, no libc).  The 56-byte
# program header overlaps the tail of the 64-byte ELF header (e_phoff = 56); all
# real code and string data follow at offset 112, where e_entry points.  It is
# ET_EXEC loaded at a fixed base, so strings are reached RIP-relative.
#
# Track-mode state lives in a single stack frame based at rbx (see offsets).
# Registers held live across the key loop: rbx = frame base.  Every subroutine
# preserves rbx; render keeps its entry index in r14, so the format helpers it
# calls avoid r12-r15.

.intel_syntax noprefix
.set BASE, 0x400000
.set SYS_read, 0
.set SYS_write, 1
.set SYS_open, 2
.set SYS_close, 3
.set SYS_ioctl, 16
.set SYS_exit, 60
.set SYS_time, 201
.set TCGETS, 0x5401
.set TCSETS, 0x5402
.set WEIGHTS, 0x013CFFC4        # signed bytes: -60, -1, 60, 1
.set NMAX, 250
.set RAWBITS, 0x0B              # ICANON|ECHO|ISIG in c_lflag

# --- track frame layout (offsets from rbx) ---
.set F_TZ,      0x0000          # 4096  timezone file buffer
.set F_TIMES,   0x1000          #  500  u16 minutes-of-day, NMAX entries
.set F_TERMIOS, 0x1200          #   60  saved termios
.set F_WORK,    0x1240          #   60  working termios / probe
.set F_IN,      0x1280          #   16  edit input buffer
.set F_MSG,     0x1290          #   96  status message
.set F_LINE,    0x1300          #  128  render line buffer
.set F_N,       0x1388          #    4  entry count
.set F_RAW,     0x138C          #    4  raw-mode flag
.set F_CLR,     0x1390          #    4  stdout-is-tty flag
.set F_MSGLEN,  0x1394          #    4  message length
.set F_TZLEN,   0x1398          #    8  bytes read from tz file
.set F_KEY,     0x13A0          #    1  single-byte read target
.set FRAME,     0x1400

# ---------------------------------------------------------------- ELF header
hdr:
	.byte	0x7f, 'E', 'L', 'F', 2, 1, 1, 0
	.quad	0                       # e_ident padding
	.word	2, 0x3e                 # e_type ET_EXEC, e_machine x86_64
	.long	1                       # e_version
	.quad	BASE + (_start - hdr)   # e_entry
	.quad	56                      # e_phoff (program header overlaps here)
	.quad	0                       # e_shoff
	.long	0                       # e_flags
	.word	64                      # e_ehsize
	.word	56                      # e_phentsize
# --- program header at offset 56 ---
	.word	1                       # e_phnum = 1  (= p_type low word, PT_LOAD)
	.word	0
	.long	5                       # p_flags R+X
	.quad	0                       # p_offset
	.quad	BASE                    # p_vaddr
	.quad	BASE                    # p_paddr
	.quad	filesz                  # p_filesz
	.quad	filesz                  # p_memsz
	.quad	0x1000                  # p_align
# ---- string data (before the code, so length constants resolve as immediates)
clrstr:		.ascii	"\033[H\033[J"
dashstr:	.ascii	" - "
runstr:		.ascii	" - ...     (running)"
totstr:		.ascii	"total "
editstr:	.ascii	"edit: "
bsstr:		.ascii	"\b \b"
nlstr:		.ascii	"\n"
path:		.asciz	"/etc/localtime"
inv1:		.ascii	"invalid time '"
.set inv1len, . - inv1
inv2:		.ascii	"' (want HH:MM), kept "
.set inv2len, . - inv2
promptstr:	.ascii	"[enter] stamp  [x] remove last  [e] edit last  [q] quit\n"
.set promptlen, . - promptstr

.globl _start
_start:
	mov	rdi, [rsp]              # argc
	lea	rsi, [rsp+8]            # argv
	cmp	rdi, 1
	jg	run_sum
	jmp	run_track

# ============================================================= number output
# emit eax in decimal, ascending, into [rdi]; advances rdi. uses eax,ecx,edx,r8
emitdec:
	mov	ecx, 10
	xor	r8d, r8d
0:	xor	edx, edx
	div	ecx
	push	rdx
	inc	r8d
	test	eax, eax
	jnz	0b
1:	pop	rax
	add	al, '0'
	mov	[rdi], al
	inc	rdi
	dec	r8d
	jnz	1b
	ret

# emit two digits of al (0..99) into [rdi]; advances rdi by 2. uses eax,ecx,edx
emit2:
	movzx	eax, al
	xor	edx, edx
	mov	ecx, 10
	div	ecx
	add	al, '0'
	mov	[rdi], al
	add	dl, '0'
	mov	[rdi+1], dl
	add	rdi, 2
	ret

# emit "HH:MM" for eax minute-of-day (0..1439) into [rdi]; advances rdi.
emithm:
	xor	edx, edx
	mov	ecx, 60
	div	ecx                     # eax=hour, edx=min
	push	rdx
	call	emit2                   # hour
	mov	byte ptr [rdi], ':'
	inc	rdi
	pop	rax
	call	emit2                   # min
	ret

# emit "<h>h <m>m" for eax minutes into [rdi]; advances rdi.
emitdur:
	xor	edx, edx
	mov	ecx, 60
	div	ecx                     # eax=h, edx=m
	push	rdx
	call	emitdec
	mov	word ptr [rdi], 0x2068  # 'h',' '
	add	rdi, 2
	pop	rax
	call	emitdec
	mov	byte ptr [rdi], 'm'
	inc	rdi
	ret

# copy rcx bytes from rsi to [rdi]; advances rdi (and rsi). uses al,rsi,rcx.
copyn:
	test	rcx, rcx
	jz	1f
0:	mov	al, [rsi]
	mov	[rdi], al
	inc	rsi
	inc	rdi
	dec	rcx
	jnz	0b
1:	ret

# write(1, rsi, rdx).  uses rax; syscall clobbers rcx,r11.
writestr:
	mov	edi, 1
	mov	eax, SYS_write
	syscall
	ret

# ================================================================= sum mode
# entry: rdi=argc, rsi=argv.  state: r12d total, ebx weights, ebp cur,
# r10d line, r11b indig, r13 argidx, r14 argc, r15 argv.
run_sum:
	sub	rsp, 0x100              # scratch frame for the output line
	mov	r14, rdi
	mov	r15, rsi
	xor	r12d, r12d
	mov	ebx, WEIGHTS
	xor	ebp, ebp
	xor	r10d, r10d
	xor	r11d, r11d
	mov	r13d, 1
.Larg:
	cmp	r13, r14
	jge	.Lsum_done
	mov	r9, [r15 + r13*8]
.Lbyte:
	movzx	eax, byte ptr [r9]
	test	al, al
	jz	.Largend
	inc	r9
	call	feedb
	jmp	.Lbyte
.Largend:
	mov	al, ' '                 # NUL between args is a separator
	call	feedb
	inc	r13
	jmp	.Larg
.Lsum_done:
	xor	edi, edi
	mov	eax, SYS_exit
	syscall

# feed byte al to the parser; preserves r9 and the state regs except as noted.
feedb:
	cmp	al, '0'
	jb	.Lsep
	cmp	al, '9'
	ja	.Lsep
	sub	al, '0'
	movzx	eax, al
	imul	ebp, ebp, 10
	add	ebp, eax
	mov	r11b, 1
	ret
.Lsep:
	mov	cl, al
	test	r11b, r11b
	jz	.Lnl
	movsx	eax, bl                 # finish a number: line += weight*cur
	imul	eax, ebp
	add	r10d, eax
	shr	ebx, 8
	xor	ebp, ebp
	xor	r11d, r11d
	test	ebx, ebx
	jnz	.Lnl
	call	sum_emit                # four numbers: print & reset
	mov	ebx, WEIGHTS
	xor	r10d, r10d
	xor	ebp, ebp
	xor	r11d, r11d
	ret
.Lnl:
	cmp	cl, 10
	jne	0f
	mov	ebx, WEIGHTS            # newline abandons a partial entry
	xor	r10d, r10d
	xor	ebp, ebp
	xor	r11d, r11d
0:	ret

# add this entry to the total and print "<h>h <m>m\n".  total r12d, line r10d.
sum_emit:
	mov	eax, r10d               # d = line mod 1440, non-negative
	cdq
	mov	ecx, 1440
	idiv	ecx
	test	edx, edx
	jns	0f
	add	edx, 1440
0:	add	r12d, edx
	lea	rdi, [rsp+0x40]
	mov	eax, r12d
	call	emitdur
	mov	byte ptr [rdi], 10
	inc	rdi
	lea	rsi, [rsp+0x40]
	mov	edx, edi
	sub	edx, esi
	call	writestr
	ret

# ============================================================== track mode
run_track:
	sub	rsp, FRAME
	mov	rbx, rsp
	mov	dword ptr [rbx+F_N], 0
	mov	dword ptr [rbx+F_RAW], 0
	mov	dword ptr [rbx+F_CLR], 0
	mov	dword ptr [rbx+F_MSGLEN], 0
	mov	qword ptr [rbx+F_TZLEN], 0
	# ioctl(0, TCGETS, termios)
	xor	edi, edi
	mov	esi, TCGETS
	lea	rdx, [rbx+F_TERMIOS]
	mov	eax, SYS_ioctl
	syscall
	test	rax, rax
	jnz	.Lnoraw
	# copy termios -> work, clear raw bits in c_lflag (offset 12), TCSETS
	lea	rsi, [rbx+F_TERMIOS]
	lea	rdi, [rbx+F_WORK]
	mov	ecx, 60
	call	copyn
	and	dword ptr [rbx+F_WORK+12], ~RAWBITS
	xor	edi, edi
	mov	esi, TCSETS
	lea	rdx, [rbx+F_WORK]
	mov	eax, SYS_ioctl
	syscall
	mov	dword ptr [rbx+F_RAW], 1
.Lnoraw:
	# clr = (ioctl(1, TCGETS, probe) == 0)
	mov	edi, 1
	mov	esi, TCGETS
	lea	rdx, [rbx+F_WORK]
	mov	eax, SYS_ioctl
	syscall
	test	rax, rax
	jnz	.Lnoclr
	mov	dword ptr [rbx+F_CLR], 1
.Lnoclr:
	# open("/etc/localtime", O_RDONLY); read up to 4096; close
	lea	rdi, [rip+path]
	xor	esi, esi
	xor	edx, edx
	mov	eax, SYS_open
	syscall
	test	rax, rax
	js	.Lkey
	mov	r12, rax                # fd
	mov	edi, r12d
	lea	rsi, [rbx+F_TZ]
	mov	edx, 4096
	xor	eax, eax                # SYS_read
	syscall
	test	rax, rax
	js	0f
	mov	[rbx+F_TZLEN], rax
0:	mov	edi, r12d
	mov	eax, SYS_close
	syscall

.Lkey:
	call	render
	xor	edi, edi
	lea	rsi, [rbx+F_KEY]
	mov	edx, 1
	xor	eax, eax                # SYS_read
	syscall
	test	rax, rax
	jle	.Lquit
	movzx	eax, byte ptr [rbx+F_KEY]
	cmp	al, 10
	je	.Lstamp
	cmp	al, 13
	je	.Lstamp
	cmp	al, 'x'
	je	.Lremove
	cmp	al, 'e'
	je	.Ledit
	cmp	al, 'q'
	je	.Lquit
	cmp	al, 3
	je	.Lquit
	cmp	al, 4
	je	.Lquit
	jmp	.Lkey
.Lstamp:
	mov	ecx, [rbx+F_N]
	cmp	ecx, NMAX
	jge	.Lkey
	call	now_minute              # -> eax
	mov	ecx, [rbx+F_N]
	lea	rdx, [rbx+F_TIMES]
	mov	[rdx+rcx*2], ax
	inc	dword ptr [rbx+F_N]
	jmp	.Lkey
.Lremove:
	mov	ecx, [rbx+F_N]
	test	ecx, ecx
	jz	.Lkey
	dec	dword ptr [rbx+F_N]
	jmp	.Lkey
.Ledit:
	mov	ecx, [rbx+F_N]
	test	ecx, ecx
	jz	.Lkey
	call	edit_last
	jmp	.Lkey
.Lquit:
track_quit:                             # public label for the terminal-restore proof
	cmp	dword ptr [rbx+F_RAW], 0
	je	0f
	xor	edi, edi
	mov	esi, TCSETS
	lea	rdx, [rbx+F_TERMIOS]
	mov	eax, SYS_ioctl
	syscall
0:	xor	edi, edi
	mov	eax, SYS_exit
	syscall

# now_minute -> eax = local minute-of-day.  keeps `now` in r13.
now_minute:
	xor	edi, edi
	mov	eax, SYS_time
	syscall
	mov	r13, rax
	lea	rdi, [rbx+F_TZ]
	mov	rsi, [rbx+F_TZLEN]
	mov	rdx, r13
	call	tz_offset               # -> eax signed offset seconds
	movsxd	rax, eax
	add	rax, r13
	mov	rcx, 86400              # s = local mod 86400, non-negative
	cqo
	idiv	rcx
	mov	rax, rdx
	test	rax, rax
	jns	0f
	add	rax, 86400
0:	xor	edx, edx
	mov	ecx, 60
	div	ecx                     # eax = minute
	ret

# tz_offset(rdi=tz, rsi=len, rdx=now) -> eax signed gmt offset, 0 if malformed.
# preserves rbx and r12-r15 (uses rax,rcx,rdx,rsi,rdi,r8,r9,r10,r11).
tz_offset:
	cmp	rsi, 44
	jb	.Ltz0
	cmp	dword ptr [rdi], 0x66695A54   # "TZif"
	jne	.Ltz0
	mov	r8d, [rdi+32]
	bswap	r8d                     # timecnt
	mov	r9d, [rdi+36]
	bswap	r9d                     # typecnt
	test	r9d, r9d
	jz	.Ltz0
	mov	rax, r8                 # base = 44 + 5*timecnt + 6*typecnt
	imul	rax, rax, 5
	mov	rcx, r9
	imul	rcx, rcx, 6
	add	rax, rcx
	add	rax, 44
	cmp	rax, rsi
	ja	.Ltz0
	lea	rsi, [rdi + r8*4 + 44]  # first transition time
	lea	r11, [rdi + r8*4 + 44]  # type-index array (starts at 44 + 4*timecnt)
	mov	rcx, r8                 # loop counter = timecnt
	xor	r10d, r10d              # idx = 0
.Ltzscan:
	test	rcx, rcx
	jz	.Ltzdone
	mov	eax, [rsi]
	bswap	eax
	movsxd	rax, eax
	cmp	rax, rdx                # t <= now ?
	jg	.Ltzdone
	movzx	r10d, byte ptr [r11]
	add	rsi, 4
	inc	r11
	dec	rcx
	jmp	.Ltzscan
.Ltzdone:
	cmp	r10, r9                 # idx >= typecnt -> malformed
	jae	.Ltz0
	lea	rax, [rdi + r8*4 + 44]  # ttinfo base = 44 + 5*timecnt
	add	rax, r8
	lea	rcx, [r10 + r10*2]      # idx*3
	lea	rax, [rax + rcx*2]      # + idx*6
	mov	eax, [rax]
	bswap	eax
	ret
.Ltz0:
	xor	eax, eax
	ret

# render the current screen.  rbx=frame; uses r14 as entry index.
render:
	cmp	dword ptr [rbx+F_CLR], 0
	je	0f
	lea	rsi, [rip+clrstr]
	mov	edx, 6
	call	writestr
0:	xor	r14d, r14d
.Lrow:
	mov	ecx, [rbx+F_N]
	cmp	r14d, ecx
	jge	.Ltotal
	lea	rdi, [rbx+F_LINE]
	mov	eax, r14d               # idx = i/2 + 1
	shr	eax, 1
	inc	eax
	cmp	eax, 10                 # right-align to width 2
	jae	1f
	mov	byte ptr [rdi], ' '
	inc	rdi
1:	call	emitdec
	mov	word ptr [rdi], 0x2020  # two spaces
	add	rdi, 2
	lea	rdx, [rbx+F_TIMES]
	movzx	eax, word ptr [rdx + r14*2]
	call	emithm
	mov	ecx, [rbx+F_N]
	lea	eax, [r14+1]
	cmp	eax, ecx
	jge	.Lrun
	lea	rsi, [rip+dashstr]      # " - "
	mov	rcx, 3
	call	copyn
	lea	rdx, [rbx+F_TIMES]
	movzx	eax, word ptr [rdx + r14*2 + 2]
	call	emithm
	mov	byte ptr [rdi], ' '     # three spaces
	mov	word ptr [rdi+1], 0x2020
	add	rdi, 3
	lea	rdx, [rbx+F_TIMES]
	movzx	eax, word ptr [rdx + r14*2 + 2]
	movzx	ecx, word ptr [rdx + r14*2]
	sub	eax, ecx                # entry_dur, wrap past midnight
	jns	2f
	add	eax, 1440
2:	call	emitdur
	jmp	.Lemit
.Lrun:
	lea	rsi, [rip+runstr]       # " - ...     (running)"
	mov	rcx, 20
	call	copyn
.Lemit:
	mov	byte ptr [rdi], 10
	inc	rdi
	lea	rsi, [rbx+F_LINE]
	mov	rdx, rdi
	sub	rdx, rsi
	call	writestr
	add	r14d, 2
	jmp	.Lrow
.Ltotal:
	lea	rdi, [rbx+F_LINE]
	lea	rsi, [rip+totstr]       # "total "
	mov	rcx, 6
	call	copyn
	xor	r9d, r9d                # tot
	xor	r10d, r10d              # j
.Ltl:
	mov	ecx, [rbx+F_N]
	lea	eax, [r10+1]
	cmp	eax, ecx
	jge	.Lte
	lea	rdx, [rbx+F_TIMES]
	movzx	eax, word ptr [rdx + r10*2 + 2]
	movzx	ecx, word ptr [rdx + r10*2]
	sub	eax, ecx
	jns	0f
	add	eax, 1440
0:	add	r9d, eax
	add	r10d, 2
	jmp	.Ltl
.Lte:
	mov	eax, r9d
	call	emitdur
	mov	byte ptr [rdi], 10
	inc	rdi
	lea	rsi, [rbx+F_LINE]
	mov	rdx, rdi
	sub	rdx, rsi
	call	writestr
	# optional message
	mov	ecx, [rbx+F_MSGLEN]
	test	ecx, ecx
	jz	.Lprompt
	lea	rsi, [rbx+F_MSG]
	mov	edx, ecx
	call	writestr
	lea	rsi, [rip+nlstr]
	mov	edx, 1
	call	writestr
	mov	dword ptr [rbx+F_MSGLEN], 0
.Lprompt:
	lea	rsi, [rip+promptstr]
	mov	edx, promptlen
	call	writestr
	ret

# edit the last stamp.  rbx=frame; uses r14 = len, r15 = prev value.
edit_last:
	mov	ecx, [rbx+F_N]
	dec	ecx
	lea	rdx, [rbx+F_TIMES]
	movzx	r15d, word ptr [rdx + rcx*2]   # prev
	lea	rdi, [rbx+F_IN]                # pre-fill with HH:MM(prev)
	mov	eax, r15d
	call	emithm
	lea	rcx, [rbx+F_IN]
	mov	r14, rdi
	sub	r14, rcx                       # len
	lea	rsi, [rip+editstr]             # "edit: "
	mov	edx, 6
	call	writestr
	lea	rsi, [rbx+F_IN]
	mov	rdx, r14
	call	writestr
.Led_loop:
	xor	edi, edi
	lea	rsi, [rbx+F_KEY]
	mov	edx, 1
	xor	eax, eax
	syscall
	test	rax, rax
	jle	.Led_ret
	movzx	eax, byte ptr [rbx+F_KEY]
	cmp	al, 10
	je	.Led_break
	cmp	al, 13
	je	.Led_break
	cmp	al, 0x7f
	je	.Led_bs
	cmp	al, 8
	je	.Led_bs
	cmp	r14, 15
	jae	.Led_loop
	lea	rcx, [rbx+F_IN]
	mov	[rcx+r14], al
	inc	r14
	lea	rsi, [rbx+F_KEY]
	mov	edx, 1
	call	writestr
	jmp	.Led_loop
.Led_bs:
	test	r14, r14
	jz	.Led_loop
	dec	r14
	lea	rsi, [rip+bsstr]
	mov	edx, 3
	call	writestr
	jmp	.Led_loop
.Led_break:
	lea	rsi, [rip+nlstr]
	mov	edx, 1
	call	writestr
	lea	rdi, [rbx+F_IN]
	mov	rsi, r14
	call	parse_hm                       # -> eax minute or -1
	test	eax, eax
	js	.Led_bad
	mov	ecx, [rbx+F_N]
	dec	ecx
	lea	rdx, [rbx+F_TIMES]
	mov	[rdx+rcx*2], ax
	ret
.Led_bad:
	lea	rdi, [rbx+F_MSG]                # "invalid time '" + IN + "' ... " + HH:MM
	lea	rsi, [rip+inv1]
	mov	rcx, inv1len
	call	copyn
	lea	rsi, [rbx+F_IN]
	mov	rcx, r14
	call	copyn
	lea	rsi, [rip+inv2]
	mov	rcx, inv2len
	call	copyn
	mov	eax, r15d
	call	emithm
	lea	rcx, [rbx+F_MSG]
	sub	rdi, rcx
	mov	[rbx+F_MSGLEN], edi
	ret
.Led_ret:
	ret

# parse_hm(rdi=ptr, rsi=len) -> eax minute-of-day, or -1.
parse_hm:
	cmp	rsi, 4
	jb	.Lph_bad
	cmp	rsi, 5
	ja	.Lph_bad
	mov	r8d, 2                  # hd = (s[1]==':') ? 1 : 2
	cmp	byte ptr [rdi+1], ':'
	jne	0f
	mov	r8d, 1
0:	lea	rax, [r8+3]
	cmp	rax, rsi
	jne	.Lph_bad
	cmp	byte ptr [rdi+r8], ':'
	jne	.Lph_bad
	xor	r9d, r9d                # hh
	xor	ecx, ecx                # i
.Lph_h:
	cmp	ecx, r8d
	jge	.Lph_hd
	movzx	eax, byte ptr [rdi+rcx]
	sub	eax, '0'
	cmp	eax, 9
	ja	.Lph_bad
	imul	r9d, r9d, 10
	add	r9d, eax
	inc	ecx
	jmp	.Lph_h
.Lph_hd:
	cmp	r9d, 23
	jg	.Lph_bad
	movzx	eax, byte ptr [rdi+r8+1]
	sub	eax, '0'
	cmp	eax, 5
	ja	.Lph_bad
	mov	r10d, eax
	movzx	eax, byte ptr [rdi+r8+2]
	sub	eax, '0'
	cmp	eax, 9
	ja	.Lph_bad
	lea	r10d, [r10 + r10*4]     # m = tens*10 + units
	add	r10d, r10d
	add	r10d, eax
	imul	eax, r9d, 60           # hh*60 + m
	add	eax, r10d
	ret
.Lph_bad:
	mov	eax, -1
	ret

filesz = . - hdr
