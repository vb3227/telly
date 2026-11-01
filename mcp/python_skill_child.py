"""Trusted subprocess bootstrap; skill source receives no host credentials or I/O."""

import json
import math
import os
import operator
import statistics
import sys
from types import SimpleNamespace

from RestrictedPython import compile_restricted
from RestrictedPython.Guards import (
    full_write_guard,
    guarded_iter_unpack_sequence,
    guarded_unpack_sequence,
    safe_builtins,
    safer_getattr,
)


def memory_limit():
    """Hard process allocation cap, not an OS security sandbox."""
    limit = 256 * 1024 * 1024
    if os.name != "nt":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        return None
    import ctypes as c
    from ctypes import wintypes as w

    class Basic(c.Structure):
        _fields_ = [
            ("process_time", c.c_int64),
            ("job_time", c.c_int64),
            ("flags", w.DWORD),
            ("min_ws", c.c_size_t),
            ("max_ws", c.c_size_t),
            ("active", w.DWORD),
            ("affinity", c.c_size_t),
            ("priority", w.DWORD),
            ("scheduling", w.DWORD),
        ]

    class IO(c.Structure):
        _fields_ = [
            (n, c.c_uint64)
            for n in ("reads", "writes", "other", "read_bytes", "write_bytes", "other_bytes")
        ]

    class Extended(c.Structure):
        _fields_ = [
            ("basic", Basic),
            ("io", IO),
            ("process_memory", c.c_size_t),
            ("job_memory", c.c_size_t),
            ("peak_process", c.c_size_t),
            ("peak_job", c.c_size_t),
        ]

    kernel = c.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
    kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    kernel.GetCurrentProcess.restype = w.HANDLE
    handle = kernel.CreateJobObjectW(None, None)
    info = Extended()
    info.basic.flags = 0x100 | 0x2000  # PROCESS_MEMORY, KILL_ON_JOB_CLOSE
    info.process_memory = limit
    if (
        not handle
        or not kernel.SetInformationJobObject(handle, 9, c.byref(info), c.sizeof(info))
        or not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess())
    ):
        raise RuntimeError("Cannot enforce Python process memory limit")
    return handle  # Keep alive until the process exits.


def send(value):
    raw = json.dumps(value, ensure_ascii=True, allow_nan=False)
    if len(raw) > 65536:
        raise ValueError("Python result exceeds 64 KB")
    sys.stdout.write(raw + "\n")
    sys.stdout.flush()


class Telly:
    def call(self, method, **arguments):
        send({"call": method, "arguments": arguments})
        line = sys.stdin.readline(65537)
        if not line:
            raise RuntimeError("MCP runtime disconnected")
        reply = json.loads(line)
        if "error" in reply:
            raise RuntimeError(reply["error"])
        return reply["value"]

    def run(self, program):
        return self.call("run", program=program)

    def wait(self, job_id, timeout=10):
        return self.call("wait", job_id=job_id, timeout=timeout)

    def sensors(self):
        return self.call("sensors")

    def show(self, text):
        return self.call("show", text=text)

    def rgb(self, r=0, g=0, b=0, brightness=5):
        return self.run(
            {"steps": [{"op": "rgb.set", "r": r, "g": g, "b": b, "brightness": brightness}]}
        )

    def sleep(self, seconds):
        return self.call("sleep", seconds=seconds)

    def load(self):
        return self.call("load")

    def save(self, value):
        return self.call("save", value=value)


class Output:
    def __init__(self):
        self.text = ""

    def write(self, value):
        if len(self.text) + len(value) > 4096:
            raise ValueError("Python output exceeds 4096 characters")
        self.text += value

    def _call_print(self, *args, **kwargs):
        if "file" in kwargs:
            raise ValueError("print(file=...) is unavailable")
        print(*args, file=self, **kwargs)

    def __call__(self):
        return self.text


def main():
    _memory_job = memory_limit()
    packet = json.loads(sys.stdin.readline(65537))
    output = Output()
    builtins = dict(safe_builtins)
    builtins.update(
        {
            "list": list,
            "dict": dict,
            "set": set,
            "sum": sum,
            "min": min,
            "max": max,
            "enumerate": enumerate,
            "zip": zip,
            "sorted": sorted,
            "any": any,
            "all": all,
            "reversed": reversed,
        }
    )
    environment = {
        "__builtins__": builtins,
        "__name__": "telly_skill",
        "__metaclass__": type,
        "_getattr_": safer_getattr,
        "_getitem_": lambda obj, key: obj[key],
        "_getiter_": iter,
        "_write_": full_write_guard,
        "_iter_unpack_sequence_": guarded_iter_unpack_sequence,
        "_unpack_sequence_": guarded_unpack_sequence,
        "_print_": lambda *args: output,
        "_inplacevar_": lambda op, left, right: {
            "+=": operator.add,
            "-=": operator.sub,
            "*=": operator.mul,
            "/=": operator.truediv,
            "//=": operator.floordiv,
            "%=": operator.mod,
            "**=": operator.pow,
        }[op](left, right),
        "math": SimpleNamespace(
            **{
                n: getattr(math, n)
                for n in ("sqrt", "sin", "cos", "pi", "floor", "ceil", "isfinite")
            }
        ),
        "statistics": SimpleNamespace(
            **{n: getattr(statistics, n) for n in ("mean", "median", "stdev", "pstdev")}
        ),
        "telly": Telly(),
    }
    code = compile_restricted(packet["source"], filename="<telly-skill>", mode="exec")
    exec(code, environment)
    send({"done": True, "value": environment.get("result"), "stdout": output.text})
    return _memory_job


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        send({"done": True, "error": type(exc).__name__ + ": " + str(exc)[:400]})
