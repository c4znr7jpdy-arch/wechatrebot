from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from html import unescape
import re
from typing import Any
import xml.etree.ElementTree as ET


@dataclass(frozen=True, slots=True)
class ImageCdnCandidate:
    file_id: str
    aes_key: str
    image_type: int


@dataclass(frozen=True, slots=True)
class QuotedImage:
    text: str
    candidates: tuple[ImageCdnCandidate, ...]


@dataclass(frozen=True, slots=True)
class QuotedMessage:
    """The useful portion of a WeChat type=57 quoted message."""

    quote_type: int
    user_text: str
    content: str
    sender_id: str = ""
    sender_name: str = ""
    message_id: str = ""
    candidates: tuple[ImageCdnCandidate, ...] = ()


def _normalized_name(value: Any) -> str:
    return re.sub(r"[_-]", "", str(value).rsplit("}", 1)[-1]).lower()


def _scalar(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("String", ""))
    return "" if value is None else str(value)


def _lookup(data: Mapping[str, Any], *names: str) -> str:
    wanted = {_normalized_name(name) for name in names}
    for key, value in data.items():
        if _normalized_name(key) in wanted:
            text = _scalar(value).strip()
            if text:
                return text
    return ""


def _int_lookup(data: Mapping[str, Any], *names: str, default: int = 0) -> int:
    raw = _lookup(data, *names)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _candidate(
    data: Mapping[str, Any],
    file_names: tuple[str, ...],
    key_names: tuple[str, ...],
    fallback_key: str,
    image_type: int,
) -> ImageCdnCandidate | None:
    file_id = _lookup(data, *file_names)
    aes_key = _lookup(data, *key_names) or fallback_key
    if not file_id or not aes_key:
        return None
    return ImageCdnCandidate(file_id, aes_key, image_type)


def callback_image_candidates(
    data: Mapping[str, Any],
) -> tuple[ImageCdnCandidate, ...]:
    nested = data.get("data")
    source: dict[str, Any] = dict(nested) if isinstance(nested, Mapping) else {}
    source.update(data)
    fallback_key = _lookup(
        source,
        "aeskey",
        "cdn_aeskey",
        "cdndatakey",
        "cdnbigimgkey",
        "cdnmidimgkey",
        "cdnthumbkey",
        "cdnthumbaeskey",
    )
    candidates: list[ImageCdnCandidate] = []
    for file_names, key_names, image_type in (
        (
            ("cdndataurl",),
            ("cdndatakey",),
            _int_lookup(source, "filetype", default=1),
        ),
        (
            ("cdnmidimgurl", "cdnmidurl"),
            ("cdnmidimgkey", "cdnmidkey"),
            2,
        ),
        (
            ("cdnbigimgurl", "cdnbigurl"),
            ("cdnbigimgkey", "cdnbigkey"),
            1,
        ),
        (
            ("cdnthumbimgurl", "cdnthumburl"),
            ("cdnthumbimgkey", "cdnthumbkey", "cdnthumbaeskey"),
            _int_lookup(source, "thumbfiletype", default=3),
        ),
    ):
        item = _candidate(source, file_names, key_names, fallback_key, image_type)
        if item is not None and item not in candidates:
            candidates.append(item)
    return tuple(candidates)


def _iter_strings(value: Any) -> tuple[str, ...]:
    values: list[str] = []
    if isinstance(value, Mapping):
        for item in value.values():
            values.extend(_iter_strings(item))
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            values.extend(_iter_strings(item))
    elif isinstance(value, str):
        text = value.strip()
        if text:
            values.append(text)
    return tuple(values)


def _candidate_sources_from_text(value: str) -> tuple[dict[str, str], ...]:
    decoded = _decode_layers(value)
    if not decoded:
        return ()
    root = _parse_xml_layers(decoded)
    if root is not None:
        return (_xml_values(root),)
    values = _regex_values(decoded)
    return (values,) if values else ()


def callback_image_candidates_from_payload(
    data: Mapping[str, Any],
) -> tuple[ImageCdnCandidate, ...]:
    candidates: list[ImageCdnCandidate] = []

    def add_sources(source: Mapping[str, Any]) -> None:
        for candidate in callback_image_candidates(source):
            if candidate not in candidates:
                candidates.append(candidate)

    add_sources(data)
    for text in _iter_strings(data):
        if "<" not in text and "&lt;" not in text and "&#" not in text:
            continue
        for source in _candidate_sources_from_text(text):
            add_sources(source)
    return tuple(candidates)


def _decode_layers(value: str, max_depth: int = 5) -> str:
    decoded = value.strip()
    for _ in range(max_depth):
        next_value = unescape(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    return decoded


def _parse_xml_layers(value: str, max_depth: int = 5) -> ET.Element | None:
    candidate = value.strip()
    for _ in range(max_depth + 1):
        if not candidate:
            return None
        try:
            return ET.fromstring(candidate)
        except ET.ParseError:
            next_value = unescape(candidate)
            if next_value == candidate:
                return None
            candidate = next_value
    return None


def _local_name(value: Any) -> str:
    return _normalized_name(value)


def _find_first(root: ET.Element, name: str) -> ET.Element | None:
    wanted = _local_name(name)
    return next((node for node in root.iter() if _local_name(node.tag) == wanted), None)


def _xml_values(root: ET.Element) -> dict[str, str]:
    values: dict[str, str] = {}
    for node in root.iter():
        text = (node.text or "").strip()
        if text:
            values[_local_name(node.tag)] = text
        for key, value in node.attrib.items():
            if value:
                values[_local_name(key)] = value
    return values


def _regex_values(xml: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for match in re.finditer(
        r"([A-Za-z_][\w-]*)\s*=\s*(['\"])(.*?)\2", xml, re.DOTALL
    ):
        values[_normalized_name(match.group(1))] = unescape(match.group(3)).strip()
    for match in re.finditer(
        r"<([A-Za-z_][\w:-]*)\b[^>]*>(.*?)</\1>", xml, re.DOTALL
    ):
        values[_normalized_name(match.group(1))] = unescape(match.group(2)).strip()
    return values


def _node_text(root: ET.Element, name: str) -> str:
    node = _find_first(root, name)
    return (node.text or "").strip() if node is not None else ""


def _quoted_plain_text(value: str) -> str:
    """Normalize text quotes without leaking the nested XML envelope."""
    decoded = _decode_layers(value)
    if not decoded:
        return ""
    root = _parse_xml_layers(decoded)
    if root is None:
        return decoded.strip()
    for name in ("text", "title", "content"):
        text = _node_text(root, name)
        if text:
            return text
    return "".join(part.strip() for part in root.itertext()).strip()


def parse_quoted_message(xml: str) -> QuotedMessage | None:
    """Parse a WeChat type=57 appmsg and its refermsg payload.

    The outer title is the user's current prompt; refermsg.content is the
    message being quoted.  We deliberately keep these separate so the quote
    enters AstrBot through a Reply component instead of polluting the current
    prompt or conversation history.
    """
    root = _parse_xml_layers(xml)
    if root is None:
        return None
    appmsg = _find_first(root, "appmsg")
    if appmsg is None:
        return None
    refermsg = _find_first(appmsg, "refermsg")
    if refermsg is None:
        return None
    quote_type = _node_text(refermsg, "type")
    try:
        quote_type_int = int(quote_type)
    except (TypeError, ValueError):
        return None
    content_node = _find_first(refermsg, "content")
    content = (content_node.text or "").strip() if content_node is not None else ""
    user_text = _node_text(appmsg, "title")
    sender_name = _node_text(refermsg, "displayname")
    sender_id = _node_text(refermsg, "chatusr") or _node_text(refermsg, "fromusr")
    message_id = _node_text(refermsg, "svrid")
    candidates: tuple[ImageCdnCandidate, ...] = ()
    if quote_type_int == 3:
        decoded = _decode_layers(content)
        inner_root = _parse_xml_layers(decoded)
        if inner_root is not None:
            candidates = callback_image_candidates(_xml_values(inner_root))
        elif decoded:
            candidates = callback_image_candidates(_regex_values(decoded))
    elif quote_type_int == 1:
        content = _quoted_plain_text(content)
    else:
        content = _decode_layers(content)
    return QuotedMessage(
        quote_type=quote_type_int,
        user_text=user_text,
        content=content,
        sender_id=sender_id,
        sender_name=sender_name,
        message_id=message_id,
        candidates=candidates,
    )


def parse_quoted_image(xml: str) -> QuotedImage | None:
    quoted = parse_quoted_message(xml)
    if quoted is None or quoted.quote_type != 3:
        return None
    return QuotedImage(text=quoted.user_text, candidates=quoted.candidates)
