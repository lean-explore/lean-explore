"""Tests for the doc-gen4 RenderedCode BLOB parser."""

import pytest

from lean_explore.extract.rendered_code import (
    BlobReader,
    extract_names_from_rendered_code,
)
from tests.extract.docgen_fixtures import (
    append_node,
    const_node,
    const_tag,
    encode_name,
    encode_nat,
    encode_string,
    tag_node,
    text_node,
)


class TestBlobReader:
    """Tests for the leansqlite primitive decoders."""

    @pytest.mark.parametrize("value", [0, 127, 128, 300, 100000])
    def test_nat_roundtrip(self, value):
        """Test that Nat encoding/decoding matches leansqlite format."""
        assert BlobReader(encode_nat(value)).read_nat() == value

    @pytest.mark.parametrize("name", ["Mathlib.Data.Nat.Basic", "Nat", ""])
    def test_name_roundtrip(self, name):
        """Test that Name encoding produces correct dot-separated output."""
        assert BlobReader(encode_name(name)).read_name() == name

    def test_numeric_name_components(self):
        """Test Name.num components, both at the root and under a parent."""
        root_number = b"\x02\x00" + encode_nat(3)
        assert BlobReader(root_number).read_name() == "3"

        nested = b"\x02" + encode_name("Foo._private") + encode_nat(42)
        assert BlobReader(nested).read_name() == "Foo._private.42"

    def test_string_decodes_utf8(self):
        """Test that string lengths count UTF-8 bytes, not characters."""
        assert BlobReader(encode_string("α → β")).read_string() == "α → β"

    def test_invalid_name_tag_raises(self):
        """Test that an unknown Name tag is rejected."""
        with pytest.raises(ValueError, match="Invalid Name tag"):
            BlobReader(b"\x09").read_name()

    def test_string_past_end_raises(self):
        """Test that a string longer than the remaining data is rejected."""
        with pytest.raises(ValueError, match="past end"):
            BlobReader(encode_nat(10) + b"abc").read_string()

    def test_read_past_end_raises(self):
        """Test that reading from exhausted data is rejected."""
        with pytest.raises(ValueError, match="Unexpected end"):
            BlobReader(b"").read_byte()


class TestExtractNamesFromRenderedCode:
    """Tests for dependency-name extraction from type BLOBs."""

    def test_extract_single_const(self):
        """Test extracting a single const name from a type BLOB."""
        assert extract_names_from_rendered_code(const_node("Nat")) == ["Nat"]

    def test_extract_multiple_consts(self):
        """Test extracting multiple const names in order of appearance."""
        blob = append_node([const_node("Nat"), text_node(" → "), const_node("Bool")])
        assert extract_names_from_rendered_code(blob) == ["Nat", "Bool"]

    def test_extract_dotted_name(self):
        """Test extracting a dotted name like Nat.add."""
        assert extract_names_from_rendered_code(const_node("Nat.add")) == ["Nat.add"]

    def test_deduplicates_names(self):
        """Test that duplicate const references are deduplicated."""
        blob = append_node([const_node("Nat"), text_node(" → "), const_node("Nat")])
        assert extract_names_from_rendered_code(blob) == ["Nat"]

    def test_skips_non_const_tags(self):
        """Test that keyword, string, sort, otherExpr tags are skipped."""
        blob = append_node(
            [
                tag_node(b"\x00", text_node("def")),  # keyword
                tag_node(b"\x01", text_node('"s"')),  # string
                text_node(" "),
                const_node("Nat"),
                tag_node(b"\x07", text_node("x")),  # otherExpr
            ]
        )
        assert extract_names_from_rendered_code(blob) == ["Nat"]

    @pytest.mark.parametrize("sort_byte", [3, 4, 5, 6])
    def test_sort_tags_handled(self, sort_byte):
        """Test that sort tag variants (3-6) are handled without error."""
        blob = tag_node(bytes([sort_byte]), text_node("Type"))
        assert extract_names_from_rendered_code(blob) == []

    def test_text_only_returns_empty(self):
        """Test that a BLOB with only text returns no names."""
        assert extract_names_from_rendered_code(text_node("hello world")) == []

    def test_nested_tagged_text(self):
        """Test parsing nested tag nodes."""
        blob = tag_node(b"\x07", const_node("List"))  # otherExpr wrapping
        assert extract_names_from_rendered_code(blob) == ["List"]

    def test_anonymous_const_name_is_ignored(self):
        """Test that a const tag carrying the anonymous name adds nothing."""
        blob = tag_node(const_tag(""), text_node("_"))
        assert extract_names_from_rendered_code(blob) == []

    @pytest.mark.parametrize(
        "blob",
        [
            b"",  # empty
            b"\xff",  # invalid TaggedText tag
            tag_node(b"\x08", text_node("x")),  # invalid RenderedCode.Tag
            append_node([const_node("Nat")])[:-2],  # truncated
        ],
    )
    def test_malformed_blob_returns_empty(self, blob):
        """Test that malformed BLOBs yield no names instead of raising."""
        assert extract_names_from_rendered_code(blob) == []

    def test_malformed_blob_discards_partial_names(self):
        """Test that names read before a parse error are not returned."""
        blob = append_node([const_node("Nat"), b"\xff"])
        assert extract_names_from_rendered_code(blob) == []
