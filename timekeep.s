# timekeep: sum "HH:MM - HH:MM" ranges given as arguments.
#   timekeep <entries...>   print the running total after each entry
#   timekeep                (no args) nothing to sum; exit 0
#
# Hand-built x86_64 Linux ELF (flat binary, single .text, no libc).  The 56-byte
# program header overlaps the tail of the 64-byte ELF header (e_phoff = 56); the
# code follows at offset 112, where e_entry points.  It is ET_EXEC loaded at a
# fixed base.

.intel_syntax noprefix
.set BASE, 0x400000
.set SYS_write, 1
.set SYS_exit, 60
.set WEIGHTS, 0x013CFFC4        # signed bytes: -60, -1, 60, 1

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

# ================================================================= sum mode
# entry: argc at [rsp], argv at rsp+8.  state: r12d total, ebx weights, ebp cur,
# r10d line, r11b indig, r13 argidx, r14 argc, r15 argv.  With no arguments
# the loop has nothing to read and exits 0.
.globl _start
_start:
	mov	rdi, [rsp]              # argc
	lea	rsi, [rsp+8]            # argv
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
sum_arg:                                # public cut-point label for the loop proof
.Larg:
	cmp	r13, r14
	jge	.Lsum_done
	mov	r9, [r15 + r13*8]
sum_byte:                               # public cut-point label for the loop proof
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

# write(1, rsi, rdx).  uses rax; syscall clobbers rcx,r11.
writestr:
	mov	edi, 1
	mov	eax, SYS_write
	syscall
	ret

filesz = . - hdr
