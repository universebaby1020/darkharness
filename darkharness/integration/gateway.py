"""Host Bridge launchable gateway extension. B's Store is the sole writer.

Controller stdio actions start/stop/query the seats. Seat AF_UNIX connections
continue to use B's read-only Session, never this controller extension.
"""
from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import Future
import json
import logging
from pathlib import Path
import sys
import threading
import uuid

from darkharness.core import Store, Rejected, process_identity
from darkharness.gateway import Service, Session
from darkharness.ipc import DEFAULT_FRAME_BYTES, DEFAULT_PAGE_BYTES, read_frame, write_frame
from .launch import SeatManager, SerializedOwner
from .mailbox import IntegrationError, Mailbox


class IntegrationService(Service):
    def __init__(self, store, frame_bytes, page_bytes, official_root, python):
        super().__init__(store, frame_bytes, page_bytes)
        self.mutex = threading.RLock()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="dh-seat-loop", daemon=True)
        self.thread.start()
        self.manager = SeatManager(SerializedOwner(store, self.mutex), official_root, python) if store.owner else None
        self.jobs = {}

    def submit(self, coro):
        identifier = str(uuid.uuid4())
        self.jobs[identifier] = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return identifier

    def job(self, identifier):
        future = self.jobs.get(identifier)
        if future is None:
            raise Rejected("JOB_NOT_FOUND")
        if not future.done():
            return {"job_id": identifier, "state": "RUNNING"}
        try:
            return {"job_id": identifier, "state": "RETURNED", "result": future.result()}
        except BaseException as exc:
            code = str(exc) if isinstance(exc, IntegrationError) else type(exc).__name__
            return {"job_id": identifier, "state": "FAILED", "reason": code}

    def close(self):
        # Called outside service.mutex, otherwise the async cleanup cannot commit.
        try:
            if self.manager:
                asyncio.run_coroutine_threadsafe(self.manager.stop(), self.loop).result()
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join()
            super().close()


class IntegrationSession(Session):
    def dispatch(self, req):
        try:
            return self._dispatch_integration(req)
        except IntegrationError as exc:
            raise Rejected(str(exc)) from None

    def _dispatch_integration(self, req):
        action, payload = req["action"], req["payload"]
        if not action.startswith("integration."):
            response = super().dispatch(req)
            if action == "grant.revoke" and self.service.manager:
                self.service.submit(self.service.manager.stop())
            return response
        if self.binding is not None:
            raise Rejected("SEAT_ACTION_DENIED")
        if not self.hello or req["environment_id"] != self.service.environment_id:
            raise Rejected("HELLO_ENVIRONMENT_REQUIRED")
        if not self.service.store.owner:
            raise Rejected("NOT_OWNER")
        service, manager = self.service, self.service.manager
        if action == "integration.start":
            try:
                config = json.loads(Path(payload["config_path"]).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise Rejected("RUN_CONFIG_READ_FAILED") from None
            job = service.submit(manager.start(config))
            return self.response(req, data={"job_id": job, "state": "QUEUED", "recovery_instance": process_identity()})
        if action == "integration.stop":
            return self.response(req, data={"job_id": service.submit(manager.stop())})
        if action == "integration.job":
            return self.response(req, data=service.job(payload["job_id"]))
        if action == "integration.status":
            with manager.owner.transaction(manager.owner.epoch) as db:
                works = [dict(row) for row in db.execute("SELECT id,seat,room,attempt,state,delivery,thread FROM c_work")]
                unknown = [dict(row) for row in db.execute("SELECT id,operation,state FROM c_outbox WHERE state!='ACKED'")]
            return self.response(req, data={"agent_count": len(manager.agents), "works": works, "unknown_outbox": unknown})
        if action == "integration.events":
            after = payload.get("after", 0)
            count = payload.get("count", 100)
            if type(after) is not int or after < 0 or type(count) is not int or count < 1:
                raise Rejected("INVALID_CURSOR")
            with manager.owner.transaction(manager.owner.epoch) as db:
                rows = db.execute("SELECT * FROM c_event WHERE seq>? ORDER BY seq LIMIT ?", (after, count)).fetchall()
                page = []
                for row in rows:
                    raw = row["body"].encode()
                    identifier = Mailbox.artifact(db, raw)
                    page.append({"seq": row["seq"], "operation": row["operation"], "kind": row["kind"], "at": row["at"], "artifact_id": identifier, "bytes": len(raw)})
            return self.response(req, data={"events": page, "next_cursor": page[-1]["seq"] if page else after})
        if action == "integration.artifact.read":
            import base64
            identifier, offset = payload["artifact_id"], payload.get("cursor", 0)
            if type(offset) is not int or offset < 0:
                raise Rejected("INVALID_CURSOR")
            with manager.owner.transaction(manager.owner.epoch) as db:
                row = db.execute("SELECT length(body),substr(body,?,?) FROM c_artifact WHERE hash=?", (offset + 1, service.page_bytes, identifier)).fetchone()
            if row is None:
                raise Rejected("ARTIFACT_NOT_FOUND")
            if offset > row[0]:
                raise Rejected("INVALID_CURSOR")
            end = offset + len(row[1])
            return self.response(req, data={"artifact_id": identifier, "bytes": row[0], "cursor": offset, "base64": base64.b64encode(row[1]).decode(), "next_cursor": end if end < row[0] else None})
        if action == "integration.cancel":
            adapter = next((a for a in manager.adapters if a.alias == payload["seat"]), None)
            if adapter is None:
                raise Rejected("SEAT_NOT_RUNNING")
            manager.mailbox.control(req["request_id"], req["operation_id"], payload["attempt"], "cancel", {"seat": payload["seat"]})
            return self.response(req, data={"job_id": service.submit(adapter.cancel_owned(req["operation_id"], payload["attempt"]))})
        if action == "integration.reconcile":
            receipt = payload.get("receipt")
            if not isinstance(receipt, dict) or not receipt.get("evidence_refs"):
                raise Rejected("RECONCILIATION_EVIDENCE_REQUIRED")
            row = manager.mailbox.update(req["operation_id"], payload["attempt"], delivery="RECONCILED", result=receipt)
            return self.response(req, data={k: row[k] for k in ("id", "attempt", "state", "delivery")})
        if action == "integration.run_end":
            # Controller-only durable end condition; live Grant check sees this.
            run = payload["run_id"]
            state = payload["state"]
            if state not in {"STOPPED", "COMPLETED", "REVOKED"}:
                raise Rejected("INVALID_RUN_END")
            with manager.owner.transaction(manager.owner.epoch) as db:
                old = db.execute("SELECT body FROM controls WHERE kind='run' AND id=?", (run,)).fetchone()
            body = json.loads(old[0]) if old else {}
            body.update(state=state, run_id=run)
            result = service.store.control("run", run, body, req["expected_revision"])
            result["stop_job_id"] = service.submit(manager.stop())
            return self.response(req, data=result)
        raise Rejected("UNKNOWN_ACTION")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--official-root", required=True)
    parser.add_argument("--official-python", default=sys.executable)
    parser.add_argument("--frame-bytes", type=int, default=DEFAULT_FRAME_BYTES)
    parser.add_argument("--page-bytes", type=int, default=DEFAULT_PAGE_BYTES)
    args = parser.parse_args()
    if args.page_bytes <= 0 or args.frame_bytes < 4096 or args.page_bytes * 2 + 2048 > args.frame_bytes:
        parser.error("inconsistent frame/page limits")
    # SDK raw exception loggers can embed credentials. Structured local evidence
    # uses the integration guard instead. Do not enable SDK debug on a real run.
    logging.disable(logging.CRITICAL)
    store = service = None
    try:
        store = Store(args.state_root)
        service = IntegrationService(store, args.frame_bytes, args.page_bytes, args.official_root, args.official_python)
        session = IntegrationSession(service)
        print("integration gateway ready", file=sys.stderr)
        while not session.stop:
            request = read_frame(sys.stdin.buffer, args.frame_bytes)
            if request is None:
                break
            with service.mutex:
                answer = session.handle(request)
            write_frame(sys.stdout.buffer, answer, args.frame_bytes)
    except (Rejected, IntegrationError, OSError):
        print("integration bootstrap failed", file=sys.stderr)
        return 2
    finally:
        if service:
            service.close()
        if store:
            store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
