"""Real T02 Windows -> wsl.exe -> gateway round trip; no shell commands.

Caller supplies observed distro/user and exact Linux source copy. Only owned
random Linux temporary directory and supplied Windows evidence directory mutate.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from darkharness.host_bridge import Bridge, launcher_argv, windows_to_linux_path


def wsl(args, code, *extra):
    result = subprocess.run(["wsl.exe", "--distribution", args.distribution, "--user", args.user,
                             "--cd", "/", "--exec", args.linux_python, "-B", "-c", code, *extra],
                            capture_output=True)
    # Keep distinct command exits, never let final shell status hide an earlier failure.
    if result.returncode:
        raise RuntimeError(f"Linux fixture command exit={result.returncode}; stderr={result.stderr.decode('utf-8', errors='replace')}")
    return result.stdout.decode("utf-8").strip()


def main():
    parser = argparse.ArgumentParser()
    for name in ["distribution", "user", "linux-python", "source-linux", "windows-evidence"]:
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("this is a Windows integration test, not a fixture")
    setup = '''import json,pathlib,shutil,sys,tempfile
root=pathlib.Path(tempfile.mkdtemp(prefix="dh-t02-"))
cwd=root / "한글 공백 ' \\" $(echo reinterpreted)"
cwd.mkdir()
startup=root / "한글 startup '"
startup.mkdir()
shutil.copytree(pathlib.Path(sys.argv[1])/"darkharness",startup/"darkharness",ignore=shutil.ignore_patterns("__pycache__"))
sentinel=cwd/"sentinel ' \\".txt"
sentinel.write_bytes("Linux sentinel 한글".encode())
print(json.dumps(dict(root=str(root),cwd=str(cwd),startup=str(startup),sentinel=str(sentinel)),ensure_ascii=False))'''
    fixture = json.loads(wsl(args, setup, args.source_linux))
    windows_root = Path(args.windows_evidence)
    windows_root.mkdir(parents=True, exist_ok=True)
    windows_temp = Path(tempfile.mkdtemp(prefix="한글 공백 ", dir=windows_root))
    sentinel = windows_temp / "sentinel 한글.txt"
    sentinel.write_bytes("Windows sentinel 한글".encode())
    try:
        # Different Windows cwd is an intentional negative control for cwd inheritance.
        probe_arg = "원문 공백 ' $(echo not-shell); &"
        argv = launcher_argv(args.distribution, args.user, fixture["startup"], args.linux_python,
                             fixture["root"] + "/state", [probe_arg])
        bridge = Bridge(argv, windows_cwd=windows_temp)
        try:
            hello = bridge.request("hello", timeout=15)
            assert hello["execution_status"] == "SUCCEEDED", hello
            entered = bridge.request("workspace.enter", {"linux_cwd": fixture["cwd"]}, timeout=15)
            assert entered["execution_status"] == "SUCCEEDED", entered
            assert entered["data"]["received_path_hex"] == fixture["cwd"].encode().hex()
            for path, raw in [(fixture["sentinel"], "Linux sentinel 한글".encode()),
                              (windows_to_linux_path(args.distribution, args.user, str(sentinel.resolve())), sentinel.read_bytes())]:
                result = bridge.request("path.probe", {"sentinel": path}, timeout=15)
                assert result["execution_status"] == "SUCCEEDED", result
                data = result["data"]
                assert data["cwd_realpath"] == fixture["cwd"], "Windows cwd inherited or shell path reinterpreted"
                assert data["argv"][-1] == probe_arg, "argv not byte-preserving"
                assert data["argv_hex"][-1] == probe_arg.encode().hex()
                assert data["sha256"] == hashlib.sha256(raw).hexdigest()
                assert data["sentinel_realpath"] == path
            # No shell substitution artifact and active foreground recovery binding.
            status = bridge.request("core.status", timeout=15)
            assert status["data"]["recovery_subject"]["alive"]
        finally:
            exit_code = bridge.close()
        assert exit_code == 0, f"gateway shutdown exit={exit_code}"
        print(json.dumps({"T02": "PARTIAL", "framed_path_route": "PASS", "double_quote_argv_route": "UNSUPPORTED_SAFE_REJECTION", "level": "WINDOWS_WSL_INTEGRATION", "sentinels": 2,
                          "argv_utf8_and_hex": "MATCH", "cwd": "EXPLICIT_MATCH", "hashes": "MATCH",
                          "shell_reinterpretation": "NOT_OBSERVED", "gateway_exit": exit_code,
                          "linux_python": hello["data"]["backend"]["environment"]["python"],
                          "state_filesystem": hello["data"]["backend"]["environment"]["state_filesystem"]}))
    finally:
        # Only random temporary roots created by this test are removed.
        # Retain Windows sentinel in evidence: WSL can retain the launch cwd
        # handle even after wsl.exe exits. Never terminate WSL to remove it.
        wsl(args, "import pathlib,shutil,sys; p=pathlib.Path(sys.argv[1]); assert p.parent==pathlib.Path('/tmp') and p.name.startswith('dh-t02-'); shutil.rmtree(p)", fixture["root"])


if __name__ == "__main__":
    main()
