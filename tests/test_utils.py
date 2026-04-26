"""Tests for utils.normalize_doi()."""

from __future__ import annotations

import pytest

from dataset_citation_finder.utils import normalize_doi


class TestNormalizeDoi:

    @pytest.mark.parametrize("inp,expected", [
        ("10.6073/pasta/ABC123", "10.6073/pasta/abc123"),
        ("10.1038/s41559-023-01234", "10.1038/s41559-023-01234"),
        ("https://doi.org/10.6073/PASTA/foo", "10.6073/pasta/foo"),
        ("http://dx.doi.org/10.1234/5678", "10.1234/5678"),
        ("  10.1234/abc  ", "10.1234/abc"),
        ("doi:10.1234/xyz", "10.1234/xyz"),
        ("DOI:10.1234/XYZ", "10.1234/xyz"),
        ("HTTPS://DOI.ORG/10.1234/xyz", "10.1234/xyz"),
    ])
    def test_valid_inputs_normalize(self, inp, expected):
        assert normalize_doi(inp) == expected

    @pytest.mark.parametrize("inp", [
        None,
        "",
        "   ",
        "not-a-doi",
        "10.x/short-prefix",       # prefix must be digits
        "10.12/",                   # empty suffix
        "11.1234/abc",              # must start with 10.
        "https://example.com/foo",  # not a DOI
    ])
    def test_invalid_inputs_return_none(self, inp):
        assert normalize_doi(inp) is None

    def test_percent_encoding_decoded(self):
        # %2F is a URL-encoded forward slash — sometimes appears in citation
        # strings copied from URLs.
        assert normalize_doi("10.1234%2Fabc") == "10.1234/abc"

    def test_idempotent(self):
        normalized = "10.6073/pasta/abc"
        assert normalize_doi(normalized) == normalized
        assert normalize_doi(normalize_doi(normalized)) == normalized
