"""Offline ABI/PE checks: never load the HOOK or start WeChat."""
from __future__ import annotations

from hashlib import sha256
import faulthandler
import importlib.util
import json
from pathlib import Path
import re
import struct
import sys

from keystone import Ks, KS_ARCH_X86, KS_MODE_64
import pytest
from unicorn import Uc, UC_ARCH_X86, UC_MODE_64, UC_HOOK_CODE
from unicorn import x86_const as reg

from wechatrebot_manager import hook_recall_patch as patch

ROOT = Path(__file__).resolve().parents[1]


def test_reviewed_assembly_matches_shipped_bytes():
    source = (ROOT / "patches/hook-recall-explicit-receipt.asm").read_text()
    blocks = re.findall(r"; BEGIN (\w+) RVA=(0x[0-9A-F]+)\n(.*?)\n; END \1", source, re.S)
    assert len(blocks) == 4
    assembler = Ks(KS_ARCH_X86, KS_MODE_64)
    for _name, address, assembly in blocks:
        rva = int(address, 16)
        encoded, _ = assembler.asm(assembly, rva)
        offset = rva - patch.SECTION_RVA
        assert bytes(encoded) == patch.ADAPTER[offset:offset + len(encoded)]


class Machine:
    """Emulate the real adapter, stubbing only the old DLL's library calls."""

    BASE = 0x180000000
    STACK = 0x40080008
    DATA = 0x50000000

    def __init__(self):
        self.vm = Uc(UC_ARCH_X86, UC_MODE_64)
        # Unicorn intentionally catches a Windows SEH during its first map:
        # https://github.com/unicorn-engine/unicorn/blob/master/docs/FAQ.md
        # Keep pytest's crash tracing enabled during all actual emulation.
        was_enabled = sys.platform == "win32" and faulthandler.is_enabled()
        if was_enabled:
            faulthandler.disable()
        try:
            self.vm.mem_map(self.BASE, 0xB3D000)
        finally:
            if was_enabled:
                faulthandler.enable()
        self.vm.mem_map(0x40000000, 0x100000)
        self.vm.mem_map(self.DATA, 0x10000)
        self.vm.mem_write(self.BASE + patch.SECTION_RVA, patch.ADAPTER)
        self.set("RSP", self.STACK)
        self.set("RBP", self.STACK + 0x108)
        self.stop = self.BASE + 0x300000
        self.qword(self.STACK, self.stop)

    def get(self, name):
        return self.vm.reg_read(getattr(reg, "UC_X86_REG_" + name))

    def set(self, name, value):
        self.vm.reg_write(getattr(reg, "UC_X86_REG_" + name), value)

    def qword(self, address, value=None):
        if value is not None:
            self.vm.mem_write(address, struct.pack("<Q", value & ((1 << 64) - 1)))
        return struct.unpack("<Q", self.vm.mem_read(address, 8))[0]

    def cstring(self, address):
        return bytes(self.vm.mem_read(address, 256)).split(b"\0", 1)[0]

    def returned(self):
        self.set("RIP", self.qword(self.get("RSP")))
        self.set("RSP", self.get("RSP") + 8)

    def run(self, rva, until=None):
        self.vm.emu_start(self.BASE + rva, until or self.stop, count=1000)
        assert self.get("RIP") == (until or self.stop), "adapter failed to return"


@pytest.mark.parametrize("target", [b"wxid_test", b"1234567890123456789@chatroom"])
@pytest.mark.parametrize("message_id", [9007199254740993, (1 << 64) - 1])
@pytest.mark.parametrize("timestamp,type_code", [(1790236882, 5), (0xFFFFFFFF, 6)])
def test_wrapper_preserves_exact_id_and_explicit_receipt(target, message_id, timestamp, type_code):
    _run_wrapper(target, message_id, timestamp, type_code, explicit=True)


@pytest.mark.parametrize("changes", [
    {"object_type": 0}, {"type_code": 0}, {"type_code": 7},
    {"timestamp": 0}, {"timestamp": -1}, {"timestamp": 1 << 32},
    {"target_type": 0}, {"target_type": 5}, {"target": b""},
])
def test_missing_or_invalid_receipt_keeps_legacy_call(changes):
    args = dict(target=b"wxid_test", message_id=1234567890123456789,
                timestamp=1790236882, type_code=6, explicit=False)
    _run_wrapper(**(args | changes))


def test_adapter_works_at_relocated_image_base(monkeypatch):
    monkeypatch.setattr(Machine, "BASE", 0x7FFE20000000)
    _run_wrapper(b"1234567890123456789@chatroom", (1 << 64) - 1, 1790236882, 6, True)
    test_builder_trampolines_override_only_tagged_receipt(True)
    test_builder_trampolines_override_only_tagged_receipt(False)


def _run_wrapper(target, message_id, timestamp, type_code, explicit,
                 object_type=1, target_type=3):
    m = Machine()
    json_object = m.get("RBP") - 0xB0
    time_value, target_value, string = m.DATA + 0x100, m.DATA + 0x120, m.DATA + 0x140
    id_pointer, output = m.DATA + 0x300, m.DATA + 0x400
    m.vm.mem_write(json_object, bytes([object_type]))
    m.vm.mem_write(time_value, bytes([type_code]))
    m.qword(time_value + 8, timestamp)
    m.vm.mem_write(target_value, bytes([target_type]))
    m.qword(target_value + 8, string)
    m.qword(string + 0x10, len(target))
    m.qword(string + 0x18, max(15, len(target)))
    if len(target) <= 15:
        m.vm.mem_write(string, target + b"\0")
    else:
        m.qword(string, m.DATA + 0x200)
        m.vm.mem_write(m.DATA + 0x200, target + b"\0")
    m.qword(id_pointer, message_id)
    m.set("RCX", output)
    m.set("RDX", id_pointer)
    saved = {"RBX": 0x1122, "RSI": 0x3344, "RDI": 0x5566, "RBP": m.get("RBP")}
    for name, value in saved.items():
        m.set(name, value)
    calls = []

    def external(vm, address, _size, _data):
        rva = address - m.BASE
        if rva not in {0x139A50, 0x205030}:
            return
        assert m.get("RSP") % 16 == 8
        # A callee may use every byte of its 32-byte incoming home area.
        vm.mem_write(m.get("RSP") + 8, b"\xAA" * 32)
        if rva == 0x139A50:
            assert m.get("RCX") == json_object
            key = m.get("RDX")
            assert m.qword(key + 0x10) == 10
            assert m.qword(key + 0x18) == 15
            name = m.cstring(key)
            assert name in {b"createTime", b"toUserName"}
            m.set("RAX", time_value if name == b"createTime" else target_value)
            # The old accessor consumes the key's MSVC string object.
            vm.mem_write(key, bytes(32))
        else:
            assert m.get("RCX") == output
            assert m.get("RDX") == id_pointer
            assert m.qword(id_pointer) == message_id
            if explicit:
                assert m.get("R9") == patch.RECEIPT_TAG
                context = m.get("R8")
                assert struct.unpack("<I", vm.mem_read(context, 4))[0] == timestamp
                assert m.cstring(m.qword(context + 8)) == target
            else:
                assert m.get("R8") == m.get("R9") == 0
            calls.append(rva)
            m.set("RAX", output)
        for name in ("RCX", "RDX", "R8", "R9", "R10", "R11"):
            m.set(name, 0xDEADBEEF)
        m.returned()

    m.vm.hook_add(UC_HOOK_CODE, external)
    m.run(patch.SECTION_RVA)
    assert calls == [0x205030]
    assert m.get("RAX") == output
    assert m.get("RSP") == m.STACK + 8
    for name, value in saved.items():
        assert m.get(name) == value


@pytest.mark.parametrize("explicit", [True, False])
def test_builder_trampolines_override_only_tagged_receipt(explicit):
    m = Machine()
    # Builder frame after its real prologue: RBP = entry RSP - 0x188,
    # RSP = entry RSP - 0x288. Its home area begins at RBP + 0x190.
    frame = m.STACK - 0x188
    m.set("RBP", frame)
    m.set("RSP", m.STACK - 0x288)
    m.set("RDX", m.DATA + 0x400)
    context, target = m.DATA + 0x100, m.DATA + 0x200
    m.qword(context, 1790236882)
    m.qword(context + 8, target)
    m.vm.mem_write(target, b"room@chatroom\0")
    m.set("R8", context)
    m.set("R9", patch.RECEIPT_TAG if explicit else 0xDEADBEEF)
    m.run(patch.SECTION_RVA + 0x100, m.BASE + 0x205077)
    assert m.qword(frame + 0x190) == (context if explicit else 0)
    assert m.get("RBX") == m.DATA + 0x400
    assert m.qword(m.get("RSP") + 0x70) == m.DATA + 0x400
    # Poison former argument registers as the cache/string code does.
    m.set("R8", 0)
    m.set("R9", 0)
    m.set("RAX", 123)
    m.qword(frame - 0x58, 31)
    m.run(patch.SECTION_RVA + 0x130, m.BASE + 0x2053C3)
    assert struct.unpack("<I", m.vm.mem_read(m.get("RSP") + 0x30, 4))[0] == (
        1790236882 if explicit else 123
    )
    assert m.get("RDX") == 31
    m.set("RCX", 0xABCD)
    m.set("RDX", m.DATA + 0x600)
    before_rsp = m.get("RSP")
    m.run(patch.SECTION_RVA + 0x150, m.BASE + (0x1386C0 if explicit else 0x264450))
    assert m.get("RSP") == before_rsp
    assert m.get("RCX") == (m.DATA + 0x600 if explicit else 0xABCD)
    assert m.get("RDX") == (target if explicit else m.DATA + 0x600)


def _pe_fixture():
    """Only the pinned layout and instruction sites; no third-party binary."""
    data = bytearray(patch.SOURCE_SIZE)
    struct.pack_into("<I", data, 0x3C, 0x128)
    data[0x128:0x12C] = b"PE\0\0"
    struct.pack_into("<HH", data, 0x12C, 0x8664, 9)
    struct.pack_into("<H", data, 0x140, 0x20B)
    struct.pack_into("<I", data, 0x144, 0x837800)
    struct.pack_into("<I", data, 0x178, patch.SECTION_RVA)
    struct.pack_into("<II", data, 0x1C8, 0xACC000, 0x54BE8)
    struct.pack_into("<I", data, 0x2B0, 0x54BE8)
    for offset, expected, _replacement in patch.INSTRUCTIONS:
        data[offset:offset + len(expected)] = expected
    return bytes(data)


def test_pe_section_unwind_directory_and_branch_sites():
    original = _pe_fixture()
    output = patch._apply_layout(original)
    assert len(output) == len(original) + 512
    assert struct.unpack_from("<H", output, 0x12E)[0] == 10
    assert struct.unpack_from("<I", output, 0x178)[0] == 0xB3D000
    assert struct.unpack_from("<II", output, 0x1C8) == (0xACC000, 0x54C00)
    assert struct.unpack_from("<I", output, 0x2B0)[0] == 0x54C00
    section = struct.unpack_from("<8sIIIIIIHHI", output, 0x398)
    assert section[:5] == (b".wrbrcl\0", len(patch.ADAPTER), patch.SECTION_RVA, 512, len(original))
    assert section[-1] == 0x60000020  # Executable/readable, never writable.
    assert struct.unpack_from("<III", output, 0xAD97E8) == (
        patch.SECTION_RVA, patch.SECTION_RVA + patch.WRAPPER_SIZE, patch.UNWIND_RVA,
    )
    assert struct.unpack_from("<III", output, 0xAD97F4) == (
        patch.SECTION_RVA + 0x100, patch.SECTION_RVA + 0x14C, patch.TRAMPOLINE_UNWIND_RVA,
    )
    # Chain to the original builder's fully established nonvolatile frame.
    assert patch.ADAPTER[0x180:0x190] == bytes.fromhex("2100000030502000e95d2000a8dfa000")
    assert output[patch.SECTION_RAW:patch.SECTION_RAW + len(patch.ADAPTER)] == patch.ADAPTER
    for offset, _expected, replacement in patch.INSTRUCTIONS:
        assert output[offset:offset + len(replacement)] == replacement


def test_patch_rejects_unknown_input_or_unreviewed_output(monkeypatch):
    original = _pe_fixture()
    with pytest.raises(ValueError, match="pinned"):
        patch.patch_hook_recall(original)
    monkeypatch.setattr(patch, "SOURCE_SHA256", sha256(original).hexdigest().upper())
    with pytest.raises(ValueError, match="output SHA-256"):
        patch.patch_hook_recall(original)
    corrupt = bytearray(original)
    corrupt[patch.INSTRUCTIONS[0][0]] ^= 1
    with pytest.raises(ValueError, match="instruction mismatch"):
        patch._apply_layout(bytes(corrupt))


def test_staging_transforms_copy_and_preserves_external_source(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("recall_staging", ROOT / "scripts/build_staging.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    original = _pe_fixture()
    expected = patch._apply_layout(original)
    monkeypatch.setattr(patch, "SOURCE_SHA256", sha256(original).hexdigest().upper())
    monkeypatch.setattr(patch, "PATCHED_SHA256", sha256(expected).hexdigest().upper())
    source, target = tmp_path / "vendor.dll", tmp_path / "stage/hook.dll"
    source.write_bytes(original)
    entry = dict(source="vendor.dll", transform=patch.TRANSFORM,
                 input_sha256=patch.SOURCE_SHA256, sha256=patch.PATCHED_SHA256)
    builder._copy_source_entry(source, target, entry)
    assert source.read_bytes() == original
    assert target.read_bytes() == expected
    for invalid in ({"transform": "unknown"}, {"input_sha256": "0" * 64}, {"sha256": "0" * 64}):
        with pytest.raises(RuntimeError):
            builder._copy_source_entry(source, tmp_path / "invalid.dll", entry | invalid)
        assert not (tmp_path / "invalid.dll").exists()


def test_release_manifests_pin_both_sides_of_transform():
    source = json.loads((ROOT / "release/source-manifest.json").read_text())
    hook = json.loads((ROOT / "release/hook-binaries.json").read_text())
    entries = [entry for entry in source["entries"] if entry.get("transform")]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["transform"] == patch.TRANSFORM
    assert entry["input_sha256"] == patch.SOURCE_SHA256
    assert entry["sha256"] == patch.PATCHED_SHA256
    shipped = next(item for item in hook["files"] if item["path"] == entry["source"])
    assert shipped["sha256"] == patch.PATCHED_SHA256
    assert shipped["size"] == patch.SOURCE_SIZE + patch.SECTION_SIZE
