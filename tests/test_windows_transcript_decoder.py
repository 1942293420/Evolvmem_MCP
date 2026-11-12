"""No live services or private transcripts; exercise bounded decoder output."""
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest

try:
    import zstandard
except ImportError:
    zstandard = None

_spec = importlib.util.spec_from_file_location(
    "transcript_decoder", Path(__file__).parents[1] / "scripts/windows/decode-transcript.py"
)
decoder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(decoder)


@unittest.skipUnless(zstandard, "install zstandard==0.25.0 for compressed fixtures")
class DecoderTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.source = Path(self.root.name) / "fixture.jsonl.zstd"

    def test_concatenated_frames_preserve_utf8_bytes(self):
        parts = [b'{"type":"session_meta"}\n', '{"text":"中文"}\n'.encode()]
        self.source.write_bytes(b"".join(zstandard.ZstdCompressor().compress(p) for p in parts))
        output = io.BytesIO()
        decoder.decode(self.source, output)
        self.assertEqual(output.getvalue(), b"".join(parts))

    def test_truncated_frame_is_not_accepted(self):
        self.source.write_bytes(zstandard.ZstdCompressor().compress(b"record\n")[:-2])
        with self.assertRaises(ValueError):
            decoder.decode(self.source, io.BytesIO())

    def test_normal_session_frame_larger_than_128_kib(self):
        content = b'{"type":"event_msg"}\n' * 30000
        self.source.write_bytes(zstandard.ZstdCompressor().compress(content))
        output = io.BytesIO()
        decoder.decode(self.source, output)
        self.assertEqual(output.getvalue(), content)

    def test_decoded_byte_cap_rejects_expansion(self):
        self.source.write_bytes(zstandard.ZstdCompressor().compress(b"x" * 5000))
        with self.assertRaises(ValueError):
            decoder.decode(self.source, io.BytesIO(), limit=1024)

    def test_header_mode_does_not_emit_record_body(self):
        self.source.write_bytes(zstandard.ZstdCompressor().compress(b"header\nprivate body\n"))
        output = io.BytesIO()
        decoder.decode(self.source, output, header=True)
        self.assertEqual(output.getvalue(), b"header\n")


if __name__ == "__main__":
    unittest.main()
