"""Smoke test: gateway reachable, model listed, chat + tool calling + vision work.

Run from the project root:  python scripts/check_connection.py
"""
import base64
import struct
import sys
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rbot.llm import MODEL, get_client  # noqa: E402


def red_png_data_url(size: int = 64) -> str:
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * size for _ in range(size))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return "data:image/png;base64," + base64.b64encode(png).decode()


def check(name, fn):
    t = time.time()
    try:
        detail = fn()
        print(f"[PASS] {name:<14} {time.time() - t:5.1f}s  {detail}")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] {name:<14} {time.time() - t:5.1f}s  {type(e).__name__}: {e}")
        return False


def main() -> int:
    client = get_client()

    def models():
        ids = [m.id for m in client.models.list().data]
        assert MODEL in ids, f"{MODEL} not in gateway model list"
        return f"{MODEL} available ({len(ids)} models on gateway)"

    def chat():
        r = client.chat.completions.create(
            model=MODEL, max_tokens=200,
            messages=[{"role": "user", "content": "Reply with exactly: CONNECTED"}])
        return repr(r.choices[0].message.content.strip())

    def tools():
        r = client.chat.completions.create(
            model=MODEL, max_tokens=400,
            messages=[{"role": "user", "content": "Click at position x=100, y=200."}],
            tools=[{"type": "function", "function": {
                "name": "click", "description": "Click at screen coordinates",
                "parameters": {"type": "object", "required": ["x", "y"],
                               "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}}}}}])
        call = r.choices[0].message.tool_calls[0].function
        return f"{call.name}({call.arguments})"

    def vision():
        # Streamed so the check also covers the SSE path we'll use for live agent output.
        stream = client.chat.completions.create(
            model=MODEL, max_tokens=400, stream=True,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": "What color is this image? Answer in one word."},
                {"type": "image_url", "image_url": {"url": red_png_data_url()}}]}])
        text = "".join(c.choices[0].delta.content or "" for c in stream if c.choices)
        assert "red" in text.lower(), f"unexpected answer: {text!r}"
        return repr(text.strip())

    results = [check(n, f) for n, f in
               [("models", models), ("chat", chat), ("tool calling", tools), ("vision", vision)]]
    print("\nALL CHECKS PASSED" if all(results) else "\nSOME CHECKS FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
