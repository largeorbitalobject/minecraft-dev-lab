"""Process ownership for this node's supervisor, never PID-only global cleanup."""
from __future__ import annotations
import os
import signal
import subprocess


class OwnedProcess:
    def __init__(self, command, log, *, cwd, env=None, stdin=subprocess.DEVNULL, stderr=subprocess.STDOUT):
        self.job = None
        self._stopped = False
        self.process = subprocess.Popen(
            command, stdin=stdin, stdout=log, stderr=stderr,
            cwd=cwd, env=env, start_new_session=os.name != "nt", shell=False,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        # The child is the bundled exec shim. It waits for a private gate file
        # before creating ANY recipe subprocess, so Windows Job assignment wins
        # the child-spawn race. Descendants then inherit this job automatically.
        if os.name == "nt":
            try:
                self.job = self._windows_job()
            except BaseException:
                self.process.kill()
                self.process.wait()
                raise

    def _windows_job(self):
        import ctypes
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", w.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", w.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(x, ctypes.c_uint64) for x in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]; k.CreateJobObjectW.restype = w.HANDLE
        k.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]; k.SetInformationJobObject.restype = w.BOOL
        k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]; k.AssignProcessToJobObject.restype = w.BOOL
        k.CloseHandle.argtypes = [w.HANDLE]; k.CloseHandle.restype = w.BOOL
        handle = k.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
        info = Extended(); info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)) or not k.AssignProcessToJobObject(handle, w.HANDLE(int(self.process._handle))):
            code = ctypes.get_last_error(); k.CloseHandle(handle)
            raise OSError(code, "Cannot establish exclusive Windows process job")
        return k, handle

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        if self.job:
            k, handle = self.job
            if not k.CloseHandle(handle):
                raise OSError("Windows job cleanup failed")
            self.job = None
        elif os.name != "nt":
            # This process object owns a new group; no wildcard or process-name kill.
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=10)

    def close(self):
        # Also close leftover descendants after the recipe's root exits.
        self.stop()
