"""Deterministic, build-time receipt adapter for the pinned 4.1.8.27 HOOK.

The old HTTP revoke handler reads only newMsgId and obtains createTime and
toUserName from its private cache. Those fields can be absent for outgoing image
receipts. This adapter forwards explicit receipt fields to the existing request
builder; its transport, protobuf encoding and response handling stay unchanged.

Only staging copies are transformed. No process is opened and no DLL is loaded.
See patches/hook-recall-explicit-receipt.asm for the reviewed instruction source.
"""
from __future__ import annotations

from hashlib import sha256
import struct


TRANSFORM = "hook-recall-explicit-receipt-v1"
SOURCE_SHA256 = "21FAFB726A287093020B4CAE799C3B1839397B735BE5B8354C927052A46A6C8D"
PATCHED_SHA256 = "C3F64AF5E3BF30135FF5B193BE7F9BE6E7F03174BDD4B66E5C921E3A0D14B977"
SOURCE_SIZE = 0xAF0200
SECTION_RVA = 0xB3C000
SECTION_RAW = SOURCE_SIZE
SECTION_SIZE = 0x200
WRAPPER_SIZE = 0xF2
UNWIND_RVA = SECTION_RVA + 0x170
TRAMPOLINE_UNWIND_RVA = SECTION_RVA + 0x180
RECEIPT_TAG = 0x57524243

# Windows x64, RIP-relative calls only (ASLR independent). Assembly is checked
# by executing these bytes in an emulator in test_hook_recall_binary_patch.py.
ADAPTER = bytes.fromhex(
    "5356574883ec704889cf4889d6488d9d50ffffff803b010f85bc000000"
    "48b86372656174655469488944244048c74424486d65000048c74424500a000000"
    "48c74424580f0000004889d9488d542440e8fcd95fff0fb60883e90583f901777a"
    "488b40084885c07e71baffffffff4839d077678944242048b8746f557365724e61"
    "488944244048c74424486d65000048c74424500a00000048c74424580f000000"
    "4889d9488d542440e8a3d95fff8038037527488b40084883781000741c"
    "488378180f7603488b0048894424284c8d44242041b943425257eb064531c04531c9"
    "4889f94889f2e8468f6cff4883c4705f5e5bc3"
    "cccccccccccccccccccccccccccc"
    "4889d348895424704531db4181f94342525775034d89c34c899d90010000e9548f6cff"
    "cccccccccccccccccccccccccc"
    "4c8b9d900100004d85db7403418b0389442430488b55a8e977926cff"
    "cccccccc"
    "4c8b9d900100004d85db740c4889d1498b5308e958c55fffe9e38272ff"
    "cccccc"
    # UNWIND_INFO: alloc 0x70, preserve RDI/RSI/RBX, seven-byte prologue.
    "0107040007d2037002600130"
    "00000000"
    # Body trampolines share the existing builder frame (chained unwind).
    "2100000030502000e95d2000a8dfa000"
)


def _branch(opcode: int, source: int, target: int, size: int = 5) -> bytes:
    return bytes([opcode]) + struct.pack("<i", target - source - 5) + b"\x90" * (size - 5)


# (file offset, expected bytes, replacement). The pinned PE's .text RVA is
# its file offset + 0xC00. Entry/timestamp trampolines replay displaced bytes.
INSTRUCTIONS = (
    (0x2A056F, bytes.fromhex("e8bc3ef6ff"), _branch(0xE8, 0x2A116F, SECTION_RVA)),
    (0x20446F, bytes.fromhex("488bda4889542470"), _branch(0xE9, 0x20506F, SECTION_RVA + 0x100, 8)),
    (0x2047BB, bytes.fromhex("89442430488b55a8"), _branch(0xE9, 0x2053BB, SECTION_RVA + 0x130, 8)),
    (0x2048C5, bytes.fromhex("e886ef0500"), _branch(0xE8, 0x2054C5, SECTION_RVA + 0x150)),
)


def _replace(data: bytearray, offset: int, before: bytes, after: bytes) -> None:
    if len(before) != len(after) or data[offset:offset + len(before)] != before:
        raise ValueError(f"HOOK recall patch instruction mismatch at 0x{offset:X}")
    data[offset:offset + len(before)] = after


def _apply_layout(original: bytes) -> bytes:
    """Apply the checked PE layout; called only after the full SHA-256 gate."""
    if len(original) != SOURCE_SIZE:
        raise ValueError("HOOK recall patch source size mismatch")
    data = bytearray(original)
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        raise ValueError("HOOK recall patch source is not a PE image")
    optional = pe + 24
    _replace(data, pe + 4, struct.pack("<H", 0x8664), struct.pack("<H", 0x8664))
    _replace(data, pe + 6, struct.pack("<H", 9), struct.pack("<H", 10))
    _replace(data, optional, struct.pack("<H", 0x20B), struct.pack("<H", 0x20B))
    _replace(data, optional + 56, struct.pack("<I", SECTION_RVA), struct.pack("<I", SECTION_RVA + 0x1000))
    code_size = struct.unpack_from("<I", data, optional + 4)[0]
    struct.pack_into("<I", data, optional + 4, code_size + SECTION_SIZE)
    # Unsigned user-mode DLLs do not require the optional PE checksum.
    struct.pack_into("<I", data, optional + 64, 0)
    section = struct.pack("<8sIIIIIIHHI", b".wrbrcl\0", len(ADAPTER), SECTION_RVA,
                          SECTION_SIZE, SECTION_RAW, 0, 0, 0, 0, 0x60000020)
    _replace(data, 0x398, bytes(40), section)
    # Existing .pdata has 24 bytes of zero padding for the wrapper and body
    # trampolines. The target tail-call is a leaf with its return on the stack.
    exception = optional + 112 + 3 * 8
    _replace(data, exception, struct.pack("<II", 0xACC000, 0x54BE8),
             struct.pack("<II", 0xACC000, 0x54C00))
    _replace(data, 0x2B0, struct.pack("<I", 0x54BE8), struct.pack("<I", 0x54C00))
    _replace(data, 0xAD97E8, bytes(24), struct.pack(
        "<IIIIII", SECTION_RVA, SECTION_RVA + WRAPPER_SIZE, UNWIND_RVA,
        SECTION_RVA + 0x100, SECTION_RVA + 0x14C, TRAMPOLINE_UNWIND_RVA,
    ))
    for offset, before, after in INSTRUCTIONS:
        _replace(data, offset, before, after)
    if len(ADAPTER) > SECTION_SIZE:
        raise ValueError("HOOK recall adapter exceeds its PE section")
    data.extend(ADAPTER.ljust(SECTION_SIZE, b"\0"))
    return bytes(data)


def patch_hook_recall(original: bytes) -> bytes:
    """Return the exact approved transform, rejecting unknown or edited DLLs."""
    if sha256(original).hexdigest().upper() != SOURCE_SHA256:
        raise ValueError("HOOK recall patch requires the pinned C2C video DLL")
    result = _apply_layout(original)
    if sha256(result).hexdigest().upper() != PATCHED_SHA256:
        raise ValueError("HOOK recall patch output SHA-256 mismatch")
    return result
