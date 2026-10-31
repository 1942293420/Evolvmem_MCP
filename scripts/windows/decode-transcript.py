"""Bounded Zstandard JSONL decoder. Binary stdout only; no plaintext disk copy.

Install zstandard==0.25.0 in the interpreter selected by decoder_python.
One frame is decoded incrementally and concatenated frames are supported.
"""
import sys

MAX_BYTES = 128 * 1024 * 1024


def decode(path, output, *, header=False, limit=MAX_BYTES):
    import zstandard

    total = 0
    frame = None
    seen = False
    with open(path, "rb") as source:
        while compressed := source.read(1024):
            while compressed:
                if frame is None or frame.eof:
                    frame = zstandard.ZstdDecompressor(max_window_size=MAX_BYTES).decompressobj()
                decoded = frame.decompress(compressed)
                seen = True
                if header and b"\n" in decoded:
                    decoded = decoded[: decoded.index(b"\n") + 1]
                    total += len(decoded)
                    if total > min(limit, 1024 * 1024):
                        raise ValueError("header_limit")
                    output.write(decoded)
                    return
                total += len(decoded)
                if total > (min(limit, 1024 * 1024) if header else limit):
                    raise ValueError("decoded_limit")
                output.write(decoded)
                compressed = frame.unused_data if frame.eof else b""
    if not seen or not frame.eof:
        raise ValueError("incomplete_frame")


if __name__ == "__main__":
    try:
        decode(sys.argv[1], sys.stdout.buffer, header=len(sys.argv) > 2 and sys.argv[2] == "header")
    except Exception:
        # Do not persist source text or credential-bearing path details.
        sys.stderr.write("compressed_transcript_decode_failed\n")
        sys.exit(1)
