"""Parser for doc-gen4 RenderedCode BLOBs.

Doc-gen4 stores declaration type signatures in the ``name_info.type`` column
as a binary BLOB using leansqlite's ToBinary serialization format.

The type is ``RenderedCode = TaggedText RenderedCode.Tag`` where::

    TaggedText:  text(0) String | tag(1) Tag TaggedText | append(2) Array
    Tag:         keyword(0) | string(1) | const(2) Name | sort-none(3)
                 | sort-type(4) | sort-prop(5) | sort-sort(6) | otherExpr(7)
    Name:        anonymous(0) | str(1) Name String | num(2) Name Nat

Encoding primitives (big-endian, leansqlite Classes.lean)::

    Nat    - variable-length 7-bit chunks, high bit = continuation
    String - Nat(utf8_byte_length) + raw UTF-8 bytes
    Array  - Nat(count) + elements
"""

import logging

logger = logging.getLogger(__name__)


class BlobReader:
    """Minimal reader for leansqlite ToBinary format."""

    __slots__ = ("_data", "_cursor")

    def __init__(self, data: bytes) -> None:
        """Initialize the reader at the start of ``data``.

        Args:
            data: Raw bytes to decode.
        """
        self._data = data
        self._cursor = 0

    def read_byte(self) -> int:
        """Read a single byte.

        Returns:
            The byte value.

        Raises:
            ValueError: If the end of the data has been reached.
        """
        if self._cursor >= len(self._data):
            raise ValueError("Unexpected end of BLOB data")
        value = self._data[self._cursor]
        self._cursor += 1
        return value

    def read_nat(self) -> int:
        """Read a variable-length natural number (7-bit chunks, MSB = more).

        Returns:
            The decoded natural number.
        """
        result = 0
        shift = 0
        while True:
            byte = self.read_byte()
            if byte >= 128:
                result |= (byte & 0x7F) << shift
            else:
                result |= byte << shift
                break
            shift += 7
        return result

    def read_string(self) -> str:
        """Read a length-prefixed UTF-8 string.

        Returns:
            The decoded string.

        Raises:
            ValueError: If the string extends past the end of the data.
        """
        byte_length = self.read_nat()
        if self._cursor + byte_length > len(self._data):
            raise ValueError("String extends past end of BLOB data")
        raw = self._data[self._cursor : self._cursor + byte_length]
        self._cursor += byte_length
        return raw.decode("utf-8")

    def read_name(self) -> str:
        """Read a Lean Name and return its dot-separated string form.

        Returns:
            The dotted name, or an empty string for the anonymous name.

        Raises:
            ValueError: If an invalid Name tag is encountered.
        """
        tag = self.read_byte()
        if tag == 0:  # anonymous
            return ""
        if tag == 1:  # str parent s
            parent = self.read_name()
            component = self.read_string()
            return f"{parent}.{component}" if parent else component
        if tag == 2:  # num parent n
            parent = self.read_name()
            number = self.read_nat()
            return f"{parent}.{number}" if parent else str(number)
        raise ValueError(f"Invalid Name tag: {tag}")


class _NameCollector:
    """Walks a TaggedText tree, collecting names from ``const`` tags in order."""

    def __init__(self, blob: bytes) -> None:
        self._reader = BlobReader(blob)
        self.names: list[str] = []
        self._seen: set[str] = set()

    def walk_tagged_text(self) -> None:
        """Walk one TaggedText node and its children."""
        tag = self._reader.read_byte()
        if tag == 0:  # text
            self._reader.read_string()
        elif tag == 1:  # tag
            self._walk_tag()
            self.walk_tagged_text()
        elif tag == 2:  # append
            count = self._reader.read_nat()
            for _ in range(count):
                self.walk_tagged_text()
        else:
            raise ValueError(f"Invalid TaggedText tag: {tag}")

    def _walk_tag(self) -> None:
        tag = self._reader.read_byte()
        if tag <= 1 or (3 <= tag <= 7):
            # keyword(0), string(1), sort-none(3), sort-type(4),
            # sort-prop(5), sort-sort(6), otherExpr(7) — no payload
            return
        if tag == 2:  # const
            name = self._reader.read_name()
            if name and name not in self._seen:
                self.names.append(name)
                self._seen.add(name)
            return
        raise ValueError(f"Invalid RenderedCode.Tag tag: {tag}")


def extract_names_from_rendered_code(blob: bytes) -> list[str]:
    """Extract referenced declaration names from a RenderedCode BLOB.

    Walks the TaggedText tree and collects Lean Names from every
    RenderedCode.Tag.const node (tag byte 2).

    Args:
        blob: Raw bytes of the RenderedCode BLOB from name_info.type.

    Returns:
        De-duplicated list of fully-qualified Lean names referenced in the type,
        or an empty list if the BLOB is malformed.
    """
    collector = _NameCollector(blob)
    try:
        collector.walk_tagged_text()
    except (ValueError, IndexError):
        logger.debug("Failed to parse RenderedCode BLOB (%d bytes)", len(blob))
        return []
    return collector.names
