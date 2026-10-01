"""Foreground Linux gateway. Its inherited stdio is the controller channel.

Seat sessions use a separate AF_UNIX endpoint and an ephemeral credential issued
only over controller stdio. This is a channel boundary, NOT same-UID isolation.
"""
import argparse
import base64
from collections import OrderedDict
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import sys
import threading

from .core import Store, Rejected, is_alive, process_identity
from .ipc import DEFAULT_FRAME_BYTES, DEFAULT_PAGE_BYTES, canonical, frame, read_frame, write_frame


def operation_projection(row):
    result = dict(row)
    payload = result.pop("payload").encode()
    result.update(payload_artifact_id=hashlib.sha256(payload).hexdigest(), payload_bytes=len(payload))
    return result


def path_probe(path, argv):
    path = Path(path)
    if not path.is_absolute():
        raise Rejected("ABSOLUTE_PATH_REQUIRED")
    # O_NONBLOCK prevents FIFO hangs; no symlink final component is accepted.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > 8 * 1024 * 1024:
            raise Rejected("PROBE_NOT_SMALL_REGULAR_FILE")
        digest = hashlib.sha256()
        count = 0
        while chunk := os.read(fd, 128 * 1024):
            count += len(chunk)
            if count > 8 * 1024 * 1024:
                raise Rejected("PROBE_TOO_LARGE")
            digest.update(chunk)
        after = os.fstat(fd)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise Rejected("PROBE_CHANGED")
        return {"argv": argv, "argv_hex": [s.encode("utf-8").hex() for s in argv],
                "cwd_realpath": str(Path.cwd().resolve()), "sentinel_realpath": str(path.resolve()),
                "sha256": digest.hexdigest(), "bytes": count}
    finally:
        os.close(fd)


class Session:
    def __init__(self, service, binding=None):
        self.service = service
        self.binding = binding  # None is inherited controller pipe, never payload-derived.
        self.hello = False
        self.cache = OrderedDict()
        self.stop = False

    def response(self, request, status="SUCCEEDED", reason="OK", data=None):
        return {"request_id": request.get("request_id"), "operation_id": request.get("operation_id"),
                "execution_status": status, "public_reason_code": reason, "evidence_refs": [],
                "next_observable_action": "core.status", "data": data}

    def handle(self, request):
        # Revoke is a live authority check, not a cached transport fact. Apply
        # before any cached reply (including hello) can disclose old results.
        if self.binding is not None and self.service.bindings.get(self.binding["credential"]) is not self.binding:
            return self.response(request, "FAILED", "BINDING_REVOKED")
        if "_frame_error" in request:
            return self.response({}, "FAILED", request["_frame_error"])
        required = {"protocol_version", "request_id", "operation_id", "environment_id", "action", "payload", "expected_revision"}
        if not required <= request.keys() or request["protocol_version"] != "1" or not isinstance(request["payload"], dict) or not isinstance(request["request_id"], str) or not request["request_id"]:
            return self.response(request, "FAILED", "INVALID_ENVELOPE")
        key = request["request_id"]
        fingerprint = hashlib.sha256(canonical(request)).hexdigest()
        if key in self.cache:
            old, answer = self.cache[key]
            return answer if old == fingerprint else self.response(request, "FAILED", "REQUEST_ID_CONFLICT")
        try:
            answer = self.dispatch(request)
        except Rejected as exc:
            answer = self.response(request, "FAILED", str(exc))
        except (KeyError, TypeError, ValueError):
            answer = self.response(request, "FAILED", "INVALID_PAYLOAD")
        except OSError:
            answer = self.response(request, "FAILED", "IO_ERROR")
        try:
            frame(answer, self.service.frame_bytes)
        except ValueError:
            # Long projections become durable artifacts, not truncated stdout.
            raw = canonical(answer.get("data"))
            identifier = hashlib.sha256(raw).hexdigest()
            try:
                with self.service.store.transaction() as db:
                    db.execute("INSERT OR IGNORE INTO artifacts VALUES(?,?)", (identifier, raw))
                answer["data"] = {"artifact_id": identifier, "bytes": len(raw), "encoding": "json-utf8"}
                answer["evidence_refs"] = ["artifact:" + identifier]
                answer["next_observable_action"] = "artifact.read"
                frame(answer, self.service.frame_bytes)
            except (Rejected, ValueError):
                answer = self.response({}, "FAILED", "RESPONSE_TOO_LARGE")
        self.cache[key] = (fingerprint, answer)
        if len(self.cache) > 256:
            self.cache.popitem(last=False)
        return answer

    def dispatch(self, req):
        s = self.service
        action, p = req["action"], req["payload"]
        if action == "hello":
            if req["environment_id"] not in (None, s.environment_id):
                raise Rejected("ENVIRONMENT_MISMATCH")
            self.hello = True
            return self.response(req, data={"protocol_version": "1", "backend": s.store.status(),
                                          "channel": "seat" if self.binding is not None else "controller",
                                          "frame_bytes": s.frame_bytes, "page_bytes": s.page_bytes})
        if not self.hello:
            raise Rejected("HELLO_REQUIRED")
        if req["environment_id"] != s.environment_id:
            raise Rejected("ENVIRONMENT_MISMATCH")
        if self.binding is not None:
            if action not in {"core.status", "artifact.read", "operation.status"}:
                raise Rejected("SEAT_ACTION_DENIED")
            if not s.store.owner:
                raise Rejected("NOT_OWNER")
            if s.bindings.get(self.binding["credential"]) is not self.binding:
                raise Rejected("BINDING_REVOKED")
        if action in {"core.status", "environment.inspect"}:
            data = s.store.status() if action == "core.status" else s.store.status()["environment"]
            if self.binding is not None:
                data = s.store.identity()
            return self.response(req, data=data)
        if not s.store.owner:
            raise Rejected("ATTACHED_READ_ONLY")
        if action == "shutdown":
            self.stop = True
            return self.response(req, data={"shutdown": "requested"})
        if action == "seat.bind":
            if not isinstance(p["seat_id"], str) or not p["seat_id"]:
                raise Rejected("INVALID_BINDING")
            binding = {"credential": secrets.token_urlsafe(32), "seat_id": p["seat_id"],
                       "operations": list(p.get("operations", [])), "artifacts": list(p.get("artifacts", []))}
            s.bindings[binding["credential"]] = binding
            return self.response(req, data={"credential": binding["credential"], "endpoint": s.endpoint})
        if action == "seat.revoke":
            for k, v in list(s.bindings.items()):
                if v["seat_id"] == p["seat_id"]:
                    del s.bindings[k]
            return self.response(req)
        if action in {"grant.record", "grant.revoke", "settings.save"}:
            # These records describe scope, not an OS authority escalation.
            if action == "grant.record" and (not p.get("source") or not isinstance(p.get("scope"), dict) or not p.get("end_condition")):
                raise Rejected("INVALID_GRANT")
            kind = "grant" if action.startswith("grant.") else "settings"
            body = dict(p)
            if action == "grant.revoke":
                body["revoked"] = True
            return self.response(req, data=s.store.control(kind, p["id"], body, req["expected_revision"]))
        if action == "operation.intent":
            return self.response(req, data=operation_projection(s.store.intent(req["operation_id"], p["attempt"], p["intent"], p["epoch"])))
        if action == "operation.transition":
            return self.response(req, data=operation_projection(s.store.transition(req["operation_id"], p["attempt"], p["state"], p["epoch"], req["expected_revision"])))
        if action == "operation.status":
            if self.binding is not None and req["operation_id"] not in self.binding["operations"]:
                raise Rejected("SEAT_BINDING_DENIED")
            row = s.store.db.execute("SELECT * FROM operations WHERE id=?", (req["operation_id"],)).fetchone()
            if row is None:
                raise Rejected("OPERATION_NOT_FOUND")
            return self.response(req, data=operation_projection(row))
        if action == "artifact.record":
            raw = base64.b64decode(p["base64"], validate=True)
            identifier = hashlib.sha256(raw).hexdigest()
            with s.store.transaction(p["epoch"]) as db:
                # Admission and artifact evidence share the operation transaction boundary.
                row = db.execute("SELECT attempt FROM operations WHERE id=?", (req["operation_id"],)).fetchone()
                if not row or row[0] != p["attempt"]:
                    raise Rejected("OWNER_ATTEMPT_FENCE")
                db.execute("INSERT OR IGNORE INTO artifacts VALUES(?,?)", (identifier, raw))
                db.execute("INSERT INTO events(operation,kind,body) VALUES(?,'ARTIFACT',?)", (req["operation_id"], canonical({"sha256": identifier}).decode()))
            return self.response(req, data={"artifact_id": identifier, "bytes": len(raw)})
        if action == "artifact.read":
            identifier = p["artifact_id"]
            if self.binding is not None and identifier not in self.binding["artifacts"]:
                raise Rejected("SEAT_BINDING_DENIED")
            offset = p.get("cursor", 0)
            if type(offset) is not int or offset < 0:
                raise Rejected("INVALID_CURSOR")
            row = s.store.db.execute("SELECT length(body),substr(body,?,?) FROM artifacts WHERE id=?", (offset + 1, s.page_bytes, identifier)).fetchone()
            if row is None:
                raise Rejected("ARTIFACT_NOT_FOUND")
            if offset > row[0]:
                raise Rejected("INVALID_CURSOR")
            next_cursor = offset + len(row[1])
            return self.response(req, data={"artifact_id": identifier, "bytes": row[0], "cursor": offset,
                "base64": base64.b64encode(row[1]).decode(), "next_cursor": next_cursor if next_cursor < row[0] else None})
        if action == "workspace.enter":
            path = Path(p["linux_cwd"])
            if not path.is_absolute():
                raise Rejected("ABSOLUTE_PATH_REQUIRED")
            os.chdir(path)
            return self.response(req, data={"cwd_realpath": str(Path.cwd().resolve()),
                                          "received_path": str(path), "received_path_hex": str(path).encode().hex()})
        if action == "path.probe":
            data = path_probe(p["sentinel"], sys.argv)
            data.update(received_path=p["sentinel"], received_path_hex=p["sentinel"].encode().hex())
            return self.response(req, data=data)
        if action == "run.bind":
            # No task launch or automatic replay in W1. The live gateway is the
            # recovery subject; optional runtime identity is observed, not invented.
            runtime = p.get("runtime_instance")
            if runtime is not None and not is_alive(runtime):
                raise Rejected("RUNTIME_NOT_ALIVE")
            body = {"run_id": p["id"], "runtime_instance": runtime,
                    "recovery_instance": s.store.instance, "owner_epoch": s.store.epoch}
            return self.response(req, data=s.store.control("run", p["id"], body, req["expected_revision"]))
        if action == "run.status":
            row = s.store.db.execute("SELECT body,revision FROM controls WHERE kind='run' AND id=?", (p["id"],)).fetchone()
            if not row:
                raise Rejected("RUN_NOT_FOUND")
            body = json.loads(row[0])
            body.update(revision=row[1], recovery_alive=is_alive(body["recovery_instance"]),
                        current_binding=body["owner_epoch"] == s.store.epoch,
                        runtime_alive=is_alive(body["runtime_instance"]) if body["runtime_instance"] else None)
            return self.response(req, data=body)
        raise Rejected("UNKNOWN_ACTION")


class Service:
    def __init__(self, store, frame_bytes, page_bytes):
        self.store, self.frame_bytes, self.page_bytes = store, frame_bytes, page_bytes
        self.environment_id = store.status()["environment"]["environment_id"]
        self.bindings = {}
        self.endpoint = None
        self.listener = None
        self.closed = threading.Event()
        self.clients = set()
        self.mutex = threading.Lock()  # One canonical SQLite writer across channels.
        if store.owner:
            store.db.execute("CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY, body BLOB NOT NULL)")
            # Abstract Unix socket: avoids stale pathname deletion and path limits.
            self.endpoint = "\0darkharness-" + hashlib.sha256(canonical(store.identity())).hexdigest()[:32]
            self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.listener.bind(self.endpoint)
            self.listener.listen()
            self.listener.settimeout(.2)
            threading.Thread(target=self.accept, daemon=True).start()

    def accept(self):
        while not self.closed.is_set():
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.clients.add(client)
            threading.Thread(target=self.seat, args=(client,), daemon=True).start()

    def seat(self, client):
        # Seat actions are read-only projections through an allowlist. Processing
        # shares the gateway mutex with controller; no second canonical writer.
        try:
            with client, client.makefile("rwb", buffering=0) as stream:
                auth = read_frame(stream, self.frame_bytes)
                with self.mutex:
                    binding = self.bindings.get(auth.get("credential")) if auth else None
                if binding is None:
                    write_frame(stream, {"execution_status": "FAILED", "public_reason_code": "SEAT_AUTH_FAILED"})
                    return
                session = Session(self, binding)
                while not self.closed.is_set():
                    req = read_frame(stream, self.frame_bytes)
                    if req is None:
                        break
                    with self.mutex:
                        # Controller and seat never use the writer connection concurrently.
                        answer = session.handle(req)
                    write_frame(stream, answer, self.frame_bytes)
        except (OSError, ValueError):
            pass
        finally:
            self.clients.discard(client)

    def close(self):
        self.closed.set()
        if self.listener:
            self.listener.close()
        for client in list(self.clients):
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--frame-bytes", type=int, default=DEFAULT_FRAME_BYTES)
    parser.add_argument("--page-bytes", type=int, default=DEFAULT_PAGE_BYTES)
    parser.add_argument("--probe-arg", action="append", default=[])
    args = parser.parse_args()
    if args.page_bytes <= 0 or args.frame_bytes < 4096 or args.page_bytes * 2 + 2048 > args.frame_bytes:
        parser.error("inconsistent frame/page limits")
    store = None
    service = None
    try:
        store = Store(args.state_root)
        service = Service(store, args.frame_bytes, args.page_bytes)
        session = Session(service)
        print("gateway ready; owner=" + str(store.owner), file=sys.stderr)
        while not session.stop:
            req = read_frame(sys.stdin.buffer, args.frame_bytes)
            if req is None:
                break
            with service.mutex:
                answer = session.handle(req)
            write_frame(sys.stdout.buffer, answer, args.frame_bytes)
    except (Rejected, OSError) as exc:
        print(type(exc).__name__ + ": bootstrap failed", file=sys.stderr)
        return 2
    finally:
        if service:
            service.close()
        if store:
            store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
