"""Windows pinned-handle rename/delete. No shell and no path-based recursive delete."""
import ctypes as C
from ctypes import wintypes as W
import hashlib
import json
import os
from pathlib import Path

from .paths import identity, snapshot


class PinnedTree:
    def __init__(self, path):
        if os.name != 'nt':raise PermissionError('Destructive cleanup currently requires Windows native handles')
        self.path=Path(path).resolve(strict=True)
        if self.path.drive.startswith('\\\\') and (not str(self.path).startswith('\\\\?\\') or str(self.path).upper().startswith('\\\\?\\UNC\\')):
            raise PermissionError('Network workspaces do not have supported native cleanup guarantees')
        self.handles={}
        self.parents=[]
        self.kernel=C.WinDLL('kernel32',use_last_error=True)
        self.kernel.CreateFileW.argtypes=[W.LPCWSTR,W.DWORD,W.DWORD,C.c_void_p,W.DWORD,W.DWORD,W.HANDLE]
        self.kernel.CreateFileW.restype=W.HANDLE
        self.kernel.CloseHandle.argtypes=[W.HANDLE]
        self.kernel.SetFileInformationByHandle.argtypes=[W.HANDLE,C.c_int,C.c_void_p,W.DWORD]
        self.kernel.ReadFile.argtypes=[W.HANDLE,C.c_void_p,W.DWORD,C.POINTER(W.DWORD),C.c_void_p]
        self.kernel.SetFilePointerEx.argtypes=[W.HANDLE,C.c_longlong,C.POINTER(C.c_longlong),W.DWORD]
        self.kernel.GetFileInformationByHandleEx.argtypes=[W.HANDLE,C.c_int,C.c_void_p,W.DWORD]
        self.nt=C.WinDLL('ntdll')
        self.nt.NtSetInformationFile.argtypes=[W.HANDLE,C.c_void_p,C.c_void_p,W.ULONG,C.c_int]
        self.nt.NtSetInformationFile.restype=C.c_long
        self.nt.RtlNtStatusToDosError.argtypes=[C.c_long]
        self.nt.RtlNtStatusToDosError.restype=W.ULONG

    def open(self,path,is_directory,mutable=True):
        expected=identity(path)
        access=0x80 | (0x10000 if mutable else 0) | (0 if is_directory else 0x80000000)
        name=str(Path(path).resolve(strict=True))
        if not name.startswith('\\\\?\\'):name='\\\\?\\'+name
        handle=self.kernel.CreateFileW(name,access,3 if is_directory else 1,None,3,0x02200000,None)
        if handle==C.c_void_p(-1).value:raise OSError(C.get_last_error(),'File is occupied or cannot be pinned')
        try:
            class FileId(C.Structure):
                _fields_=[('volume',C.c_ulonglong),('identifier',C.c_ubyte*16)]
            class Basic(C.Structure):
                _fields_=[('times',C.c_longlong*4),('attributes',W.DWORD)]
            identifier=FileId();basic=Basic()
            if not self.kernel.GetFileInformationByHandleEx(handle,18,C.byref(identifier),C.sizeof(identifier)) or not self.kernel.GetFileInformationByHandleEx(handle,0,C.byref(basic),C.sizeof(basic)):
                raise OSError(C.get_last_error(),'Cannot verify pinned file identity')
            actual=[identifier.volume,int.from_bytes(bytes(identifier.identifier),'little')]
            # CPython 3.11 exposes the 32-bit volume serial in st_dev, while
            # FILE_ID_INFO reports its 64-bit form. Keep the full file ID check.
            volume_matches = expected[0] in {actual[0], actual[0] & 0xffffffff}
            if not volume_matches or actual[1]!=expected[1] or basic.attributes & 0x400:
                raise PermissionError('Object was replaced or became a reparse point while opening')
        except BaseException:
            self.kernel.CloseHandle(handle)
            raise
        return handle

    def __enter__(self):
        try:
            initial=snapshot(self.path)
            for parent in reversed(self.path.parents):
                self.parents.append(self.open(parent,True,False))
            for record in initial['records']:
                relative=record[0]
                path=self.path if relative=='.' else self.path/relative
                handle=self.open(path,record[1]=='directory')
                self.handles[relative]=handle
                if identity(path)!=record[2]:raise PermissionError('File was replaced while locking')
            self.records=initial['records']
            actual=[]
            for record in self.records:
                if record[1]=='file':
                    digest=hashlib.sha256();size=0
                    handle=self.handles[record[0]]
                    self.kernel.SetFilePointerEx(handle,0,None,0)
                    buffer=C.create_string_buffer(1024*1024);used=W.DWORD()
                    while True:
                        if not self.kernel.ReadFile(handle,buffer,len(buffer),C.byref(used),None):raise OSError(C.get_last_error())
                        if not used.value:break
                        size+=used.value;digest.update(buffer.raw[:used.value])
                    actual.append([*record[:3],size,digest.hexdigest()])
                else:actual.append(record)
            self.digest=hashlib.sha256(json.dumps(actual,sort_keys=True).encode()).hexdigest()
            self.records=actual
            # Enumerate again under locks: newly inserted files invalidate the snapshot.
            known={r[0] for r in self.records}
            for record in self.records:
                if record[1]=='directory':
                    folder=self.path if record[0]=='.' else self.path/record[0]
                    for item in folder.iterdir():
                        if item.relative_to(self.path).as_posix() not in known:
                            raise PermissionError('Directory contents changed while locking')
            return self
        except BaseException:
            self.close();raise

    def move(self,destination):
        destination=Path(destination).parent.resolve(strict=True)/Path(destination).name
        if destination.exists():raise FileExistsError('Destination already exists')
        if destination.parent.stat().st_dev!=self.path.stat().st_dev:raise PermissionError('Cross-volume rename is not supported')
        # Pin the target parent chain too; newly created trash paths are validated by the service.
        for parent in reversed([destination.parent,*destination.parent.parents]):
            self.parents.append(self.open(parent,True,False))
        encoded=destination.name.encode('utf-16-le')
        class Rename(C.Structure):
            _fields_=[('flags',W.DWORD),('root',W.HANDLE),('length',W.DWORD),('name',W.WCHAR*1)]
        size=Rename.name.offset+len(encoded)
        buffer=C.create_string_buffer(size)
        header=Rename.from_buffer(buffer)
        header.flags=0;header.root=self.parents[-1];header.length=len(encoded)
        C.memmove(C.addressof(buffer)+Rename.name.offset,encoded,len(encoded))
        # Windows forbids directory rename with descendants opened without share-delete.
        # Keep the source object and both ancestor chains pinned; reopen/verify the
        # entire destination tree before any subsequent destructive action.
        for relative in list(self.handles):
            if relative!='.':
                self.kernel.CloseHandle(self.handles.pop(relative))
        status_buffer=C.create_string_buffer(2*C.sizeof(C.c_void_p))
        status=self.nt.NtSetInformationFile(self.handles['.'],status_buffer,buffer,size,10)
        if status<0:
            raise OSError(self.nt.RtlNtStatusToDosError(status),'Native rename refused')
        self.path=destination

    def purge(self):
        # Every deleted object is the same pinned object inspected above.
        for relative in sorted(self.handles,key=lambda value:(0 if value=='.' else len(Path(value).parts),len(value)),reverse=True):
            handle=self.handles[relative]
            flag=C.c_ubyte(1)
            if not self.kernel.SetFileInformationByHandle(handle,4,C.byref(flag),C.sizeof(flag)):
                raise OSError(C.get_last_error(),'Native delete refused; remaining objects retained')
            self.kernel.CloseHandle(handle)
            del self.handles[relative]

    def close(self):
        for handle in list(self.handles.values())+self.parents:self.kernel.CloseHandle(handle)
        self.handles.clear();self.parents.clear()

    def __exit__(self,*args):self.close()
