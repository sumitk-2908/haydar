import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from haydar import __version__
from haydar.updater import get_latest_version, get_release_url, is_newer


def _response(payload: bytes) -> MagicMock:
    response = MagicMock()
    response.read.return_value = payload
    context = MagicMock()
    context.__enter__.return_value = response
    return context


def test_is_newer_true():
    assert is_newer("0.3.0", "0.2.0") is True


def test_is_newer_false_same():
    assert is_newer("0.2.0", "0.2.0") is False


def test_is_newer_false_older():
    assert is_newer("0.1.0", "0.2.0") is False


def test_is_newer_handles_prerelease():
    assert is_newer("0.3.0a1", "0.2.0") is True
    assert is_newer("0.3.0a1", "0.3.0") is False


def test_is_newer_malformed_tag_returns_false():
    assert is_newer("nightly-build", "0.2.0") is False
    assert is_newer("latest", "0.2.0") is False


@patch("urllib.request.urlopen")
def test_get_latest_version_request_contract(mock_urlopen):
    mock_urlopen.return_value = _response(b'{"tag_name": "v0.3.0"}')

    assert get_latest_version(timeout=2.5) == "0.3.0"

    request = mock_urlopen.call_args.args[0]
    assert request.get_header("User-agent") == f"haydar/{__version__}"
    assert mock_urlopen.call_args.kwargs["timeout"] == 2.5
    assert mock_urlopen.return_value.__enter__.return_value.read.call_args.args == (
        1024 * 1024 + 1,
    )


@patch("urllib.request.urlopen")
def test_get_latest_version_network_error_returns_none(mock_urlopen):
    mock_urlopen.side_effect = urllib.error.URLError("no network")
    assert get_latest_version() is None


@patch("urllib.request.urlopen")
def test_get_latest_version_timeout_returns_none(mock_urlopen):
    mock_urlopen.side_effect = TimeoutError()
    assert get_latest_version() is None


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b"{}",
        b'{"tag_name": 3}',
        b'{"tag_name": "nightly"}',
        b'\xff',
    ],
)
@patch("urllib.request.urlopen")
def test_get_latest_version_malformed_responses_return_none(mock_urlopen, payload):
    mock_urlopen.return_value = _response(payload)
    assert get_latest_version() is None


@patch("urllib.request.urlopen")
def test_get_latest_version_accepts_unprefixed_tag(mock_urlopen):
    mock_urlopen.return_value = _response(json.dumps({"tag_name": "0.3.0"}).encode())
    assert get_latest_version() == "0.3.0"


def test_get_release_url_format_and_validation():
    assert get_release_url("0.3.0").endswith("/v0.3.0")
    assert get_release_url("v0.3.0").endswith("/v0.3.0")
    with pytest.raises(ValueError):
        get_release_url("../../bad#fragment")
