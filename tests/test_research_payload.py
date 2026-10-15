"""Lossless input transport and bounded malformed-input rejection, no publication."""
import base64
import json
import random
import zlib

import pytest

from decision_kernel.domain.records import RecordError, digest, encoded
from decision_kernel.interfaces import research_payload as codec


def envelope(raw, **changes):
    value = {codec.TRANSPORT_KEY: codec.TRANSPORT_SCHEMA, "size": len(raw),
             "sha256": digest(raw), "data": base64.b64encode(zlib.compress(raw, wbits=31)).decode()}
    return encoded({**value, **changes}).decode()


def test_large_chinese_text_and_every_question_roundtrip_without_truncation():
    value = {"body": "合成研究段落：利润、资本与现金；这是测试而非公司结论。\n" * 4000,
             "questions": [{"id": str(i), "text": f"第{i}项原问题"} for i in range(32)],
             "literal": "$(touch should-not-run) ${GITHUB_TOKEN}\n```python\npass\n```"}
    raw = encoded(value)
    assert codec.MAX_PAYLOAD_BYTES < len(raw) < codec.MAX_DECODED_BYTES
    with pytest.raises(RecordError, match="DISPATCH_PAYLOAD_TOO_LARGE"):
        codec.decode_payload(raw.decode())
    packed = codec.pack_payload(raw)
    assert len(packed.encode()) <= codec.MAX_PAYLOAD_BYTES
    assert codec.decode_payload(packed) == value
    assert digest(encoded(codec.decode_payload(packed))) == digest(raw)


def test_plain_json_keeps_old_policy_and_identity():
    value = {"limit": 4}
    assert codec.decode_payload('{ "limit" : 4 }') == value
    assert codec.decode_payload(codec.pack_payload(b'{ "limit" : 4 }')) == value


@pytest.mark.parametrize("raw", [b'[]', b'null', b'{"x":1,"x":2}', b'{"x":NaN}',
                                  b'{"x":1e999}', b'{"x":"\\ud800"}', b'not-json', b'\xff'])
def test_invalid_json_rejected_in_plain_and_pack(raw):
    with pytest.raises(RecordError):
        codec.pack_payload(raw)
    with pytest.raises((RecordError, UnicodeError)):
        codec.decode_payload(raw.decode())


@pytest.mark.parametrize("size", [True, 0, -1, 1.5, "9", None, codec.MAX_DECODED_BYTES + 1])
def test_declared_size_checked_before_decompression(size, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("malformed size reached decompressor")
    monkeypatch.setattr(codec.zlib, "decompressobj", forbidden)
    with pytest.raises(RecordError, match="INVALID_DECODED_SIZE"):
        codec.decode_payload(envelope(encoded({}), size=size))


@pytest.mark.parametrize("change", [{"sha256": "f" * 64}, {"size": 1}, {"size": 999},
                                     {"data": "not base64!"}, {"data": "甲"}, {"data": None},
                                     {"sha256": "bad"}, {"sha256": False}, {"unexpected": "ignored?"},
                                     {codec.TRANSPORT_KEY: "unknown-version"}])
def test_corrupt_envelope_or_digest_never_returns_data(change):
    with pytest.raises(RecordError):
        codec.decode_payload(envelope(encoded({"a": 1}), **change))


@pytest.mark.parametrize("mode", ["truncated", "trailing", "second-member", "wrong-crc", "zlib-not-gzip"])
def test_only_one_complete_valid_gzip_member_is_accepted(mode):
    raw = encoded({"a": 1})
    packed = zlib.compress(raw, wbits=31)
    bad = {"truncated": packed[:-1], "trailing": packed + b'x',
           "second-member": packed + zlib.compress(b'{}', wbits=31),
           "wrong-crc": packed[:-8] + bytes([packed[-8] ^ 1]) + packed[-7:],
           "zlib-not-gzip": zlib.compress(raw)}[mode]
    with pytest.raises(RecordError):
        codec.decode_payload(envelope(raw, data=base64.b64encode(bad).decode()))


def test_expansion_cap_passed_to_native_decoder_without_unlimited_flush(monkeypatch):
    original = zlib.decompressobj
    observed = []
    class Spy:
        def __init__(self, **kwargs):
            self.stream = original(**kwargs)
        def decompress(self, data, *, max_length):
            observed.append(max_length)
            return self.stream.decompress(data, max_length=max_length)
        def __getattr__(self, name):
            assert name != "flush"
            return getattr(self.stream, name)
    monkeypatch.setattr(codec.zlib, "decompressobj", Spy)
    raw = b' ' * (codec.MAX_DECODED_BYTES * 2)
    with pytest.raises(RecordError, match="SIZE_OR_STREAM"):
        codec.decode_payload(envelope(raw, size=32))
    assert observed == [33]


def test_decoded_limit_inclusive_and_one_byte_over_rejected():
    overhead = len(encoded({"body": ""}))
    exact = encoded({"body": "a" * (codec.MAX_DECODED_BYTES - overhead)})
    assert len(exact) == codec.MAX_DECODED_BYTES
    assert encoded(codec.decode_payload(codec.pack_payload(exact))) == exact
    with pytest.raises(RecordError, match="DECODED_PAYLOAD_TOO_LARGE"):
        codec.pack_payload(exact + b' ')


@pytest.mark.parametrize("raw", [b'{ "a" : 1 }', encoded({codec.TRANSPORT_KEY: "nested"}),
                                  b'{"a":1,"a":2}\n', b'{"a":1e999}\n', b'[]\n'])
def test_decoded_noncanonical_nested_duplicate_or_nonobject_rejected(raw):
    with pytest.raises(RecordError):
        codec.decode_payload(envelope(raw))


def test_outer_wire_limit_not_bypassed_by_compression_or_whitespace():
    with pytest.raises(RecordError, match="DISPATCH_PAYLOAD_TOO_LARGE"):
        codec.decode_payload(' ' * codec.MAX_PAYLOAD_BYTES + '{}')
    uncompressible = encoded({"body": random.Random(7).randbytes(100000).hex()})
    with pytest.raises(RecordError, match="COMPRESSED_PAYLOAD_TOO_LARGE"):
        codec.pack_payload(uncompressible)
    with pytest.raises(RecordError, match="NESTED_PAYLOAD"):
        codec.pack_payload(encoded({codec.TRANSPORT_KEY: "nested"}))


def test_offline_cli_roundtrip_and_bounded_error_output(tmp_path, capsys):
    file = tmp_path / "approved.json"
    raw = encoded({"body": "合成正文" * 20000})
    file.write_bytes(raw)
    assert codec.main(["--input", str(file)]) == 0
    output = capsys.readouterr()
    assert not output.err and encoded(codec.decode_payload(output.out)) == raw
    file.write_bytes(b'{"secret-test-string":NaN}')
    assert codec.main(["--input", str(file)]) == 2
    output = capsys.readouterr()
    assert not output.out and "secret-test-string" not in output.err
    assert json.loads(output.err)["error"] == "INVALID_COMMAND_JSON"


def test_offline_missing_file_does_not_reveal_path(tmp_path, capsys):
    assert codec.main(["--input", str(tmp_path / "private-path.json")]) == 2
    output = capsys.readouterr()
    assert not output.out and "private-path" not in output.err
