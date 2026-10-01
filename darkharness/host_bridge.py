"""Windows launcher with explicit observed Linux binding and framed consumer."""
import argparse
from collections import deque
from pathlib import PurePosixPath
import queue
import subprocess
import threading
import uuid

from .ipc import envelope, read_frame, write_frame


def launcher_argv(distribution, user, linux_cwd, linux_python, state_root, probe_args=()):
    if not distribution or not user or distribution.startswith("-") or user.startswith("-"):
        raise ValueError("explicit distribution/user required")
    for path in (linux_cwd, linux_python, state_root):
        if not PurePosixPath(path).is_absolute() or "\0" in path:
            raise ValueError("absolute Linux paths required")
    argv = ["wsl.exe", "--distribution", distribution, "--user", user, "--cd", linux_cwd,
            "--exec", linux_python, "-B", "-m", "darkharness.gateway", "--state-root", state_root]
    for item in probe_args:
        argv += ["--probe-arg", item]
    # WSL's Windows argument parser does not preserve Python list2cmdline's
    # escaped double quote in --cd/--exec argv (real integration regression).
    # Do not send a value that can accidentally leave --exec parsing. User
    # workspace/sentinel paths with double quotes use framed payload after hello.
    if any('"' in item for item in argv):
        raise ValueError("DOUBLE_QUOTE_REQUIRES_FRAMED_PAYLOAD")
    return argv


def windows_to_linux_path(distribution, user, windows_path):
    # Ask the selected distro, do not guess drive/mount mapping. No shell.
    result = subprocess.run(["wsl.exe", "--distribution", distribution, "--user", user,
                             "--cd", "/", "--exec", "/usr/bin/wslpath", "-a", "-u", windows_path],
                            capture_output=True, check=True)
    return result.stdout.decode("utf-8").strip()


class Bridge:
    def __init__(self, argv, windows_cwd=None, diagnostic_sink=None, frame_bytes=1024 * 1024):
        self.frame_bytes = frame_bytes
        self.process = subprocess.Popen(argv, cwd=windows_cwd, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.environment_id = None
        self.responses = queue.Queue()
        self.diagnostics = deque(maxlen=64)
        self.diagnostic_bytes = 0
        self.diagnostic_retained_bytes = 0
        self.sink = diagnostic_sink
        self.mutex = threading.Lock()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.err_reader = threading.Thread(target=self._stderr, daemon=True)
        self.reader.start()
        self.err_reader.start()

    def _read(self):
        try:
            while (answer := read_frame(self.process.stdout, self.frame_bytes)) is not None:
                self.responses.put(answer)
        finally:
            self.responses.put(None)

    def _stderr(self):
        while chunk := self.process.stderr.read(4096):
            self.diagnostic_bytes += len(chunk)
            self.diagnostics.append(chunk)
            self.diagnostic_retained_bytes = sum(map(len, self.diagnostics))
            if self.sink is not None:
                self.sink(chunk)

    def request(self, action, payload=None, operation_id=None, expected_revision=None, request_id=None, timeout=None):
        with self.mutex:
            rid = request_id or uuid.uuid4().hex
            req = envelope(action, rid, operation_id, self.environment_id, payload, expected_revision)
            write_frame(self.process.stdin, req, self.frame_bytes)
            result = self.responses.get(timeout=timeout)
            if result is None:
                raise EOFError("gateway closed")
            if result.get("request_id") != rid:
                raise ValueError("response correlation mismatch")
            if action == "hello" and result["execution_status"] == "SUCCEEDED":
                self.environment_id = result["data"]["backend"]["environment"]["environment_id"]
            return result

    def close(self):
        try:
            if self.process.poll() is None:
                try:
                    self.request("shutdown", timeout=5)
                except (OSError, EOFError, ValueError, queue.Empty):
                    pass
        finally:
            try:
                self.process.stdin.close()
            except OSError:
                pass
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # Only our directly owned launcher, never arbitrary PID searches.
                self.process.terminate()
                self.process.wait(timeout=5)
            self.reader.join(timeout=2)
            self.err_reader.join(timeout=2)
            self.process.stdout.close()
            self.process.stderr.close()
        return self.process.returncode


def main():
    parser = argparse.ArgumentParser(description="Explicit foreground Host Bridge")
    for name in ["distribution", "user", "linux-cwd", "linux-python", "state-root"]:
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    bridge = Bridge(launcher_argv(args.distribution, args.user, args.linux_cwd, args.linux_python, args.state_root))
    try:
        import json
        print(json.dumps(bridge.request("hello"), ensure_ascii=False))
        # Keep foreground alive until explicit user stdin EOF, not room/seat idle.
        input("Press Enter to shut down the owned gateway session.\n")
    finally:
        bridge.close()


if __name__ == "__main__":
    main()
