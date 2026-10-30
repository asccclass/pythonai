"""User-scoped Windows Credential Manager storage for ContactAI refresh tokens."""
import ctypes
from ctypes import wintypes
import os


_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168
_MAX_BLOB_BYTES = 2560


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ('Flags', wintypes.DWORD),
        ('Type', wintypes.DWORD),
        ('TargetName', wintypes.LPWSTR),
        ('Comment', wintypes.LPWSTR),
        ('LastWritten', wintypes.FILETIME),
        ('CredentialBlobSize', wintypes.DWORD),
        ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)),
        ('Persist', wintypes.DWORD),
        ('AttributeCount', wintypes.DWORD),
        ('Attributes', ctypes.c_void_p),
        ('TargetAlias', wintypes.LPWSTR),
        ('UserName', wintypes.LPWSTR),
    ]


class WindowsCredentialStore:
    """Store one opaque token under the current Windows user's credentials."""

    def __init__(self, target):
        if os.name != 'nt':
            raise OSError('Windows Credential Manager is unavailable')
        if (not isinstance(target, str) or not 1 <= len(target) <= 512
                or any(ord(character) < 32 for character in target)):
            raise ValueError('Invalid ContactAI credential target')
        self.target = target
        self.api = ctypes.WinDLL('Advapi32.dll', use_last_error=True)
        self.api.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
        self.api.CredWriteW.restype = wintypes.BOOL
        self.api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                       ctypes.POINTER(ctypes.POINTER(_CREDENTIALW))]
        self.api.CredReadW.restype = wintypes.BOOL
        self.api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self.api.CredDeleteW.restype = wintypes.BOOL
        self.api.CredFree.argtypes = [ctypes.c_void_p]
        self.api.CredFree.restype = None

    def read(self):
        pointer = ctypes.POINTER(_CREDENTIALW)()
        if not self.api.CredReadW(self.target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            error = ctypes.get_last_error()
            if error == _ERROR_NOT_FOUND:
                return None
            raise ctypes.WinError(error)
        try:
            credential = pointer.contents
            raw = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
            return raw.decode('utf-8')
        finally:
            self.api.CredFree(pointer)

    def write(self, value):
        if not isinstance(value, str):
            raise TypeError('Credential value must be text')
        raw = value.encode('utf-8')
        if not 1 <= len(raw) <= _MAX_BLOB_BYTES:
            raise ValueError('ContactAI refresh token is too large for Credential Manager')
        blob = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        credential = _CREDENTIALW()
        credential.Type = _CRED_TYPE_GENERIC
        credential.TargetName = self.target
        credential.CredentialBlobSize = len(raw)
        credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = 'ContactAI refresh token'
        if not self.api.CredWriteW(ctypes.byref(credential), 0):
            raise ctypes.WinError(ctypes.get_last_error())

    def delete(self):
        if self.api.CredDeleteW(self.target, _CRED_TYPE_GENERIC, 0):
            return True
        error = ctypes.get_last_error()
        if error == _ERROR_NOT_FOUND:
            return False
        raise ctypes.WinError(error)


def credential_store(target):
    """Return the platform store, or None when Windows storage is unavailable."""
    return WindowsCredentialStore(target) if target and os.name == 'nt' else None
