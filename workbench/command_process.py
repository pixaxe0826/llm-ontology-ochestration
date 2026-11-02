"""User-mode process-tree lifetime for asynchronous commands, never an OS sandbox."""
import os,signal

class ProcessTree:
    def __init__(self,process):
        self.process=process;self.handle=None
        if os.name!='nt':return
        import ctypes as c
        from ctypes import wintypes as w
        class Basic(c.Structure):
            _fields_=[('process_time',c.c_longlong),('job_time',c.c_longlong),('flags',w.DWORD),
                ('min_working_set',c.c_size_t),('max_working_set',c.c_size_t),('active',w.DWORD),
                ('affinity',c.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
        class IO(c.Structure):_fields_=[(name,c.c_ulonglong) for name in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
        class Extended(c.Structure):_fields_=[('basic',Basic),('io',IO),('process_limit',c.c_size_t),('job_limit',c.c_size_t),('peak_process',c.c_size_t),('peak_job',c.c_size_t)]
        self.api=c.WinDLL('kernel32',use_last_error=True)
        self.api.CreateJobObjectW.argtypes=[c.c_void_p,w.LPCWSTR];self.api.CreateJobObjectW.restype=w.HANDLE
        self.api.SetInformationJobObject.argtypes=[w.HANDLE,c.c_int,c.c_void_p,w.DWORD];self.api.SetInformationJobObject.restype=w.BOOL
        self.api.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE];self.api.AssignProcessToJobObject.restype=w.BOOL
        self.api.CloseHandle.argtypes=[w.HANDLE];self.api.CloseHandle.restype=w.BOOL
        self.handle=self.api.CreateJobObjectW(None,None);info=Extended();info.basic.flags=0x2000 # KILL_ON_JOB_CLOSE
        if not self.handle or not self.api.SetInformationJobObject(self.handle,9,c.byref(info),c.sizeof(info)) or not self.api.AssignProcessToJobObject(self.handle,w.HANDLE(process._handle)):
            self.close();raise ValueError('Long command process-tree supervision could not be established; execution stopped.')

    def close(self):
        if os.name=='nt':
            if self.handle:self.api.CloseHandle(self.handle);self.handle=None
        else:
            try:os.killpg(self.process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
