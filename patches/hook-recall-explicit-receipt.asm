; Reviewed Windows x64 adapter for the SHA-256 pinned WeChat 4.1.8.27 DLL.
; Each block is independently assembled by Keystone at the stated RVA.
; No absolute image addresses: these bytes work at any ASLR image base.
; Tests assemble these blocks and emulate the production bytes.
; wrapper: RBP is the HTTP handler frame; RCX is return string, RDX is uint64 ID.
; Its JSON object is at RBP-0xB0. Accessor keys are writable MSVC SSO strings.
; capture: R8 points to {uint32 create_time, padding, const char* target}; R9D is tag.
; The builder stores that pointer in its unused incoming home slot (entry RSP+8).
; timestamp/target replace cache values only when the tagged context is present.
; Target constructor tail-call retains the original return address and stack ABI.

; BEGIN wrapper RVA=0xB3C000
push rbx
push rsi
push rdi
sub rsp, 0x70
mov rdi, rcx
mov rsi, rdx
lea rbx, [rbp-0xb0]
cmp byte ptr [rbx], 1
jne legacy
mov rax, 0x6954657461657263
mov qword ptr [rsp+0x40], rax
mov qword ptr [rsp+0x48], 0x656d
mov qword ptr [rsp+0x50], 10
mov qword ptr [rsp+0x58], 15
mov rcx, rbx
lea rdx, [rsp+0x40]
call 0x139a50
movzx ecx, byte ptr [rax]
sub ecx, 5
cmp ecx, 1
ja legacy
mov rax, qword ptr [rax+8]
test rax, rax
jle legacy
mov edx, 0xffffffff
cmp rax, rdx
ja legacy
mov dword ptr [rsp+0x20], eax
mov rax, 0x614e726573556f74
mov qword ptr [rsp+0x40], rax
mov qword ptr [rsp+0x48], 0x656d
mov qword ptr [rsp+0x50], 10
mov qword ptr [rsp+0x58], 15
mov rcx, rbx
lea rdx, [rsp+0x40]
call 0x139a50
cmp byte ptr [rax], 3
jne legacy
mov rax, qword ptr [rax+8]
cmp qword ptr [rax+0x10], 0
je legacy
cmp qword ptr [rax+0x18], 15
jbe inline_target
mov rax, qword ptr [rax]
inline_target:
mov qword ptr [rsp+0x28], rax
lea r8, [rsp+0x20]
mov r9d, 1465008707
jmp invoke
legacy:
xor r8d, r8d
xor r9d, r9d
invoke:
mov rcx, rdi
mov rdx, rsi
call 0x205030
add rsp, 0x70
pop rdi
pop rsi
pop rbx
ret
; END wrapper

; BEGIN capture RVA=0xB3C100
mov rbx, rdx
mov qword ptr [rsp+0x70], rdx
xor r11d, r11d
cmp r9d, 1465008707
jne store
mov r11, r8
store:
mov qword ptr [rbp+0x190], r11
jmp 0x205077
; END capture

; BEGIN timestamp RVA=0xB3C130
mov r11, qword ptr [rbp+0x190]
test r11, r11
jz store
mov eax, dword ptr [r11]
store:
mov dword ptr [rsp+0x30], eax
mov rdx, qword ptr [rbp-0x58]
jmp 0x2053c3
; END timestamp

; BEGIN target RVA=0xB3C150
mov r11, qword ptr [rbp+0x190]
test r11, r11
jz legacy
mov rcx, rdx
mov rdx, qword ptr [r11+8]
jmp 0x1386c0
legacy:
jmp 0x264450
; END target

; UNWIND_INFO at RVA 0xB3C170: 01 07 04 00 07 D2 03 70 02 60 01 30
; Wrapper prologue: push RBX/RSI/RDI, sub RSP,0x70.
; Chained UNWIND_INFO at RVA 0xB3C180: 21 00 00 00 30 50 20 00 E9 5D 20 00 A8 DF A0 00
; Capture/timestamp chain to the builder frame; the target tail-call is a leaf.
