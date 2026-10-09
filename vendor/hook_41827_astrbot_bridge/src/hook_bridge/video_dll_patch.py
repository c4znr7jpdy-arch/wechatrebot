from __future__ import annotations

import argparse
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import tempfile


SOURCE_SHA256 = "0F66F21D86D8D273E6DA0FB8D0FBEAE59FA0A501E633504AED52F77B79C7A528"
PATCHED_SHA256 = "AC4D0D20CB1CDF48EE45DEE0789C3DB70528088670690BA3C09299CEC5CEAE0D"
PATCH_OFFSET = 0x247B0C
SOURCE_INSTRUCTION = bytes.fromhex("48 8D 95 30 02 00 00")
PATCHED_INSTRUCTION = bytes.fromhex("48 8D 95 D0 01 00 00")
CHAT_PATCHED_SHA256 = "21FAFB726A287093020B4CAE799C3B1839397B735BE5B8354C927052A46A6C8D"


@dataclass(frozen=True)
class BinaryPatch:
    name: str
    offset: int
    original: bytes
    replacement: bytes


_SNS_DNS_POINTER_PATCHES = tuple(
    BinaryPatch(
        f"select C2C dnsInfo pointer at 0x{offset:X}",
        offset,
        original,
        original[:4] + bytes.fromhex("B0 05 00 00"),
    )
    for offset, original in (
        (0x27FB43, bytes.fromhex("48 8B 84 24 B8 05 00 00")),
        (0x27FDC8, bytes.fromhex("48 8B 84 24 B8 05 00 00")),
        (0x27FF66, bytes.fromhex("48 8B 84 24 B8 05 00 00")),
        (0x280C8D, bytes.fromhex("48 8B 84 24 B8 05 00 00")),
        (0x280CB9, bytes.fromhex("48 8B 84 24 B8 05 00 00")),
        (0x280CFA, bytes.fromhex("48 8B 94 24 B8 05 00 00")),
        (0x280E77, bytes.fromhex("48 8B 94 24 B8 05 00 00")),
        (0x280F6D, bytes.fromhex("48 8B 94 24 B8 05 00 00")),
    )
)


CHAT_VIDEO_PATCHES = (
    BinaryPatch(
        "return the generated video AES key as data.fileId",
        PATCH_OFFSET,
        SOURCE_INSTRUCTION,
        PATCHED_INSTRUCTION,
    ),
    BinaryPatch(
        "return the C2C CDN file id as data.fileUrl",
        0x2479CC,
        bytes.fromhex("48 8D 95 F0 01 00 00"),
        bytes.fromhex("48 8D 95 30 02 00 00"),
    ),
    BinaryPatch(
        "rename the upload route to cdn_c2c_video_upload",
        0x857958,
        b"cdn_sns_video_upload",
        b"cdn_c2c_video_upload",
    ),
    BinaryPatch(
        "require ordinary C2C dnsInfo instead of snsDnsInfo",
        0x27FB2B,
        bytes.fromhex("F6 84 24 A0 05 00 00 04"),
        bytes.fromhex("F6 84 24 A0 05 00 00 02"),
    ),
    *_SNS_DNS_POINTER_PATCHES,
    BinaryPatch(
        "use the C2C CDN primary rule key",
        0x85B918,
        b"3_0_0_0_-1",
        b"1_0_0_0_-1",
    ),
    BinaryPatch(
        "use the C2C chat-video CDN rule key",
        0x85B928,
        b"3_102_20202_1_-1",
        b"1_1_4_1_-1" + (b"\0" * 6),
    ),
    BinaryPatch(
        "set the C2C chat-video rule-key length",
        0x27FF06,
        bytes.fromhex("41 B8 10 00 00 00"),
        bytes.fromhex("41 B8 0A 00 00 00"),
    ),
    BinaryPatch(
        "set C2C video fileType=4",
        0x28033C,
        bytes.fromhex("48 C7 84 24 D0 03 00 00 EA 4E 00 00"),
        bytes.fromhex("48 C7 84 24 D0 03 00 00 04 00 00 00"),
    ),
    BinaryPatch(
        "set C2C appType=1",
        0x2803A0,
        bytes.fromhex("C7 84 24 14 04 00 00 66 00 00 00"),
        bytes.fromhex("C7 84 24 14 04 00 00 01 00 00 00"),
    ),
)


class VideoDllPatchError(RuntimeError):
    pass


def _digest(payload: bytes | bytearray) -> str:
    return sha256(payload).hexdigest().upper()


def _apply_exact_patches(
    payload: bytearray, patches: tuple[BinaryPatch, ...]
) -> None:
    for patch in patches:
        if len(patch.original) != len(patch.replacement):
            raise VideoDllPatchError(
                f"patch changes binary size: {patch.name}"
            )
        end = patch.offset + len(patch.original)
        if payload[patch.offset:end] != patch.original:
            raise VideoDllPatchError(
                f"supported HOOK DLL does not contain the expected bytes: "
                f"{patch.name}"
            )
        payload[patch.offset:end] = patch.replacement


def _write_atomic(payload: bytes | bytearray, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, destination)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def patch_video_upload_file_key(
    source: Path,
    destination: Path,
    *,
    expected_source_sha256: str = SOURCE_SHA256,
    expected_patched_sha256: str | None = PATCHED_SHA256,
) -> Path:
    source = source.resolve()
    destination = destination.resolve()
    if source == destination:
        raise VideoDllPatchError("source and destination must be different files")

    payload = bytearray(source.read_bytes())
    actual_source_sha256 = _digest(payload)
    if actual_source_sha256 != expected_source_sha256.upper():
        raise VideoDllPatchError(
            "unsupported HOOK DLL SHA256; refusing to patch an unknown binary"
        )

    end = PATCH_OFFSET + len(SOURCE_INSTRUCTION)
    if payload[PATCH_OFFSET:end] != SOURCE_INSTRUCTION:
        raise VideoDllPatchError(
            "supported HOOK DLL does not contain the expected instruction"
        )
    payload[PATCH_OFFSET:end] = PATCHED_INSTRUCTION

    actual_patched_sha256 = _digest(payload)
    if (
        expected_patched_sha256 is not None
        and actual_patched_sha256 != expected_patched_sha256.upper()
    ):
        raise VideoDllPatchError("patched HOOK DLL SHA256 verification failed")

    _write_atomic(payload, destination)

    return destination


def patch_chat_video_upload(
    source: Path,
    destination: Path,
    *,
    expected_source_sha256: str = SOURCE_SHA256,
    expected_patched_sha256: str | None = CHAT_PATCHED_SHA256,
) -> Path:
    """Create the 4.1.8.27 C2C chat-video upload variant.

    The vendor route uploads video bytes to the SNS CDN.  This exact-version
    patch selects the ordinary C2C CDN credentials and media identifiers while
    keeping the existing network, response-decoding, and video-forward code.
    """

    source = source.resolve()
    destination = destination.resolve()
    if source == destination:
        raise VideoDllPatchError("source and destination must be different files")

    payload = bytearray(source.read_bytes())
    actual_source_sha256 = _digest(payload)
    if actual_source_sha256 != expected_source_sha256.upper():
        raise VideoDllPatchError(
            "unsupported HOOK DLL SHA256; refusing to patch an unknown binary"
        )

    _apply_exact_patches(payload, CHAT_VIDEO_PATCHES)

    actual_patched_sha256 = _digest(payload)
    if (
        expected_patched_sha256
        and actual_patched_sha256 != expected_patched_sha256.upper()
    ):
        raise VideoDllPatchError("C2C chat-video DLL SHA256 verification failed")

    _write_atomic(payload, destination)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build an exact-version 4.1.8.27 video upload DLL variant."
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument(
        "--sns-file-key-only",
        action="store_true",
        help="build the historical SNS fileKey diagnostic variant",
    )
    args = parser.parse_args()
    if args.sns_file_key_only:
        patched = patch_video_upload_file_key(args.source, args.destination)
        digest = PATCHED_SHA256
    else:
        patched = patch_chat_video_upload(args.source, args.destination)
        digest = CHAT_PATCHED_SHA256
    print(f"patched DLL written: {patched}")
    print(f"SHA256: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
