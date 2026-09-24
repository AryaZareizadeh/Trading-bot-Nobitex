import ctypes
import sys
from ctypes import wintypes

import pytest


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows scheduler regression")


class _PowerThrottlingState(ctypes.Structure):
    _fields_ = [
        ("Version", wintypes.ULONG),
        ("ControlMask", wintypes.ULONG),
        ("StateMask", wintypes.ULONG),
    ]


def _kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessAffinityMask.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.GetProcessAffinityMask.restype = wintypes.BOOL
    kernel32.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
    kernel32.SetProcessAffinityMask.restype = wintypes.BOOL
    kernel32.GetPriorityClass.argtypes = [wintypes.HANDLE]
    kernel32.GetPriorityClass.restype = wintypes.DWORD
    kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.SetPriorityClass.restype = wintypes.BOOL
    kernel32.SetProcessInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD
    ]
    kernel32.SetProcessInformation.restype = wintypes.BOOL
    return kernel32


def test_worker_setup_uses_all_processors():
    """Catch workers remaining restricted to the four efficient CPU cores."""
    import optimizer

    kernel32 = _kernel32()
    process = kernel32.GetCurrentProcess()
    original_mask = ctypes.c_size_t()
    system_mask = ctypes.c_size_t()
    assert kernel32.GetProcessAffinityMask(
        process, ctypes.byref(original_mask), ctypes.byref(system_mask)
    )
    original_priority = kernel32.GetPriorityClass(process)
    one_processor = system_mask.value & -system_mask.value

    try:
        assert kernel32.SetProcessAffinityMask(process, one_processor)

        optimizer._expand_cpu_affinity()

        active_mask = ctypes.c_size_t()
        ignored_system_mask = ctypes.c_size_t()
        assert kernel32.GetProcessAffinityMask(
            process, ctypes.byref(active_mask), ctypes.byref(ignored_system_mask)
        )
        assert active_mask.value == system_mask.value
        assert kernel32.GetPriorityClass(process) == 0x00008000
    finally:
        kernel32.SetProcessAffinityMask(process, original_mask.value)
        kernel32.SetPriorityClass(process, original_priority)


def test_windows_worker_setup_explicitly_disables_eco_qos():
    import optimizer

    observed = {}

    class FakeSetProcessInformation:
        argtypes = None
        restype = None

        def __call__(self, process, info_class, state_ptr, size):
            state = ctypes.cast(
                state_ptr, ctypes.POINTER(_PowerThrottlingState)
            ).contents
            observed.update(
                process=process,
                info_class=info_class,
                version=state.Version,
                control=state.ControlMask,
                enabled=state.StateMask,
                size=size,
            )
            return True

    class FakeKernel32:
        SetProcessInformation = FakeSetProcessInformation()

    assert optimizer._disable_windows_eco_qos(FakeKernel32(), 123)
    assert observed == {
        "process": 123,
        "info_class": 4,
        "version": 1,
        "control": 1,
        "enabled": 0,
        "size": ctypes.sizeof(_PowerThrottlingState),
    }
