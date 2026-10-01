"""Versioned, bounded, self-resynchronising stdio frames (not log lines)."""
import base64
import hashlib
import json

MAGIC = b"DH1:"
DEFAULT_FRAME_BYTES = 1024 * 1024
DEFAULT_PAGE_BYTES = 48 * 1024


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def frame(value, limit=DEFAULT_FRAME_BYTES):
    body = base64.b64encode(canonical(value))
    if len(body) > limit:
        raise ValueError("FRAME_TOO_LARGE")
    return MAGIC + f"{len(body):08x}:".encode() + hashlib.sha256(body).hexdigest()[:16].encode() + b":" + body + b"\n"


def read_frame(stream, limit=DEFAULT_FRAME_BYTES):
    """Yield a value or a protocol error; EOF alone returns None.

    Base64 contains neither ':' nor newline. Taking the last magic resyncs after
    a truncated frame joined to the next frame. Oversize input is drained in
    bounded chunks, never buffered without a limit. No raw input in errors.
    """
    while True:
        line = stream.readline(limit + 128)
        if not line:
            return None
        if not line.endswith(b"\n") and len(line) >= limit + 128:
            while line and not line.endswith(b"\n"):
                line = stream.readline(limit + 128)
            return {"_frame_error": "FRAME_TOO_LARGE"}
        pos = line.rfind(MAGIC)
        try:
            if pos < 0:
                raise ValueError()
            parts = line[pos:].rstrip(b"\r\n").split(b":", 3)
            _, size, digest, body = parts
            if len(size) != 8 or len(digest) != 16 or int(size, 16) != len(body) or len(body) > limit:
                raise ValueError()
            if hashlib.sha256(body).hexdigest()[:16].encode() != digest:
                raise ValueError()
            value = json.loads(base64.b64decode(body, validate=True).decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeError):
            return {"_frame_error": "MALFORMED_FRAME"}


def write_frame(stream, value, limit=DEFAULT_FRAME_BYTES):
    stream.write(frame(value, limit))
    stream.flush()


def envelope(action, request_id, operation_id=None, environment_id=None, payload=None, expected_revision=None):
    return dict(protocol_version="1", request_id=request_id, operation_id=operation_id,
                environment_id=environment_id, action=action, payload=payload or {},
                expected_revision=expected_revision)
