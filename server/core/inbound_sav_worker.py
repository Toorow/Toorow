"""Isolated native ReadStat worker for the governed SAV adapter."""

from __future__ import annotations

import os
import pickle
import sys

from core.inbound_sav_v2 import (
    SAV_CORRUPT,
    SAV_MEMORY_LIMIT,
    SAV_WORKER_ERROR,
    SavAdapterError,
    _parse_sav_in_process,
)


def _limit_memory_windows(max_memory_bytes: int) -> None:
    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "read_ops",
                "write_ops",
                "other_ops",
                "read_bytes",
                "write_bytes",
                "other_bytes",
            )
        ]

    class BasicLimit(ctypes.Structure):
        _fields_ = [
            ("per_process_time", ctypes.c_longlong),
            ("per_job_time", ctypes.c_longlong),
            ("limit_flags", wintypes.DWORD),
            ("min_working_set", ctypes.c_size_t),
            ("max_working_set", ctypes.c_size_t),
            ("active_process_limit", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority_class", wintypes.DWORD),
            ("scheduling_class", wintypes.DWORD),
        ]

    class ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("basic", BasicLimit),
            ("io", IoCounters),
            ("process_memory", ctypes.c_size_t),
            ("job_memory", ctypes.c_size_t),
            ("peak_process_memory", ctypes.c_size_t),
            ("peak_job_memory", ctypes.c_size_t),
        ]

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("page_faults", wintypes.DWORD),
            ("peak_working_set", ctypes.c_size_t),
            ("working_set", ctypes.c_size_t),
            ("peak_paged_pool", ctypes.c_size_t),
            ("paged_pool", ctypes.c_size_t),
            ("peak_nonpaged_pool", ctypes.c_size_t),
            ("nonpaged_pool", ctypes.c_size_t),
            ("pagefile", ctypes.c_size_t),
            ("peak_pagefile", ctypes.c_size_t),
            ("private_usage", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.INT,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    job = kernel32.CreateJobObjectW(None, None)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    if not psapi.GetProcessMemoryInfo(
        kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    ):
        raise OSError(ctypes.get_last_error(), "GetProcessMemoryInfo failed")
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    info = ExtendedLimit()
    info.basic.limit_flags = 0x00000100
    info.process_memory = counters.private_usage + max_memory_bytes
    if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
    if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
        raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")


def _limit_memory_posix(max_memory_bytes: int) -> None:
    import resource

    try:
        with open("/proc/self/statm", encoding="ascii") as statm:
            page_count = int(statm.read().split()[0])
        current_vms = page_count * int(os.sysconf("SC_PAGE_SIZE"))
    except (OSError, ValueError, IndexError):
        import psutil

        current_vms = int(psutil.Process().memory_info().vms)
    ceiling = current_vms + max_memory_bytes
    _, hard = resource.getrlimit(resource.RLIMIT_AS)
    if hard != resource.RLIM_INFINITY and ceiling > hard:
        raise OSError("The SAV worker hard memory limit is below its required budget.")
    resource.setrlimit(resource.RLIMIT_AS, (ceiling, ceiling))
    soft, _ = resource.getrlimit(resource.RLIMIT_AS)
    if soft != ceiling:
        raise OSError("The SAV worker memory limit could not be verified.")


def _limit_memory(max_memory_bytes: int) -> None:
    try:
        import pandas  # noqa: F401

        if os.name == "nt":
            _limit_memory_windows(max_memory_bytes)
        else:
            _limit_memory_posix(max_memory_bytes)
    except (ImportError, OSError, ValueError) as exc:
        raise SavAdapterError(
            SAV_WORKER_ERROR, "The SAV worker memory limit is unavailable."
        ) from exc


def main() -> int:
    try:
        data, options = pickle.load(sys.stdin.buffer)
        _limit_memory(int(options["max_memory_bytes"]))
        payload = ("ok", _parse_sav_in_process(data, **options))
    except SavAdapterError as exc:
        payload = ("error", exc.code, exc.detail)
    except MemoryError:
        payload = ("error", SAV_MEMORY_LIMIT, "The SAV worker exhausted its memory budget.")
    except BaseException:
        payload = ("error", SAV_CORRUPT, "The isolated SAV worker failed.")
    pickle.dump(payload, sys.stdout.buffer, protocol=pickle.HIGHEST_PROTOCOL)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
