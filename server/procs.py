"""Keep each analysis process tied to the service that started it.

* On Windows the process goes into a job object that ends everything in it when its last
  handle closes. The service holds that handle, so however the service stops (restart,
  crash, Stop-Process), the analysis and anything it started stop with it, rather than
  running on and writing results into a later job.
* Its process id is kept next to the job's log. If one is still there when the service
  starts (say the computer slept through a restart, or this is not Windows), it is stopped
  then, but only when that process is still an analysis (``-m cli.main daily``) so a
  reused process id never ends something else.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

ANALYSIS_MARK = "cli.main daily"


# --- Windows job object --------------------------------------------------------------

def contain(proc: subprocess.Popen):
    """Put ``proc`` in a kill-on-close job object; returns the handle (None off Windows or
    if Windows refuses, when the start-up check below is the fallback)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]

        class _Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class _Io(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class _Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", _Basic), ("IoInfo", _Io),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        job = k32.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        info = _Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)) or \
                not k32.AssignProcessToJobObject(job, int(proc._handle)):
            err = ctypes.get_last_error()
            k32.CloseHandle(job)
            raise ctypes.WinError(err)
        return job
    except Exception as exc:  # the start-up check still covers this process
        logger.warning("could not tie the analysis process to the service: %s", exc)
        return None


def release(job) -> None:
    """Close the job object: anything still in it (a cancelled run's children) ends."""
    if job is None:
        return
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle(job)
    except Exception:
        pass


# --- process id next to the job --------------------------------------------------------

def pid_file(work_dir: Path) -> Path:
    return Path(work_dir) / "pid"


def remember(work_dir: Path, pid: int) -> None:
    try:
        pid_file(work_dir).write_text(str(pid), encoding="ascii")
    except OSError:
        pass


def forget(work_dir: Path) -> None:
    try:
        pid_file(work_dir).unlink()
    except OSError:
        pass


def command_line(pid: int) -> str | None:
    """The process's command line, or None when there is no such process."""
    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
                capture_output=True, text=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout.strip() or None
    try:
        raw = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
    except OSError:
        return None
    return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip() or None


def stop_leftover(work_dir: Path) -> bool:
    """Stop the analysis a previous service left running for this job; True if one was."""
    path = pid_file(work_dir)
    try:
        pid = int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return False
    stopped = False
    line = command_line(pid)
    if line and ANALYSIS_MARK in line:
        try:
            if sys.platform == "win32":
                # /T: the whole tree, as the job object would have.
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                os.kill(pid, signal.SIGTERM)
            stopped = True
            logger.warning("stopped analysis process %s left running by the last server", pid)
        except OSError:
            pass
    forget(work_dir)
    return stopped
