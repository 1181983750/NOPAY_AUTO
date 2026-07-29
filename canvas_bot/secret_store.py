"""Per-user DPAPI-encrypted storage for dashboard secrets on Windows."""
from __future__ import annotations
import base64, ctypes, json, os
from ctypes import wintypes
from pathlib import Path

class Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

def path() -> Path:
    return Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "NO钱包自动化" / "dashboard-secrets.dat"

def crypt(raw: bytes, decrypt: bool = False) -> bytes:
    source = ctypes.create_string_buffer(raw)
    incoming = Blob(len(raw), ctypes.cast(source, ctypes.POINTER(ctypes.c_byte)))
    outgoing = Blob()
    api = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not api(ctypes.byref(incoming), None, None, None, None, 0, ctypes.byref(outgoing)):
        raise ctypes.WinError()
    try: return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally: ctypes.windll.kernel32.LocalFree(outgoing.pbData)

def load() -> dict[str, str]:
    try:
        return {k: str(v) for k, v in json.loads(crypt(base64.b64decode(path().read_bytes()), True)).items() if k in {"payment_password", "pushplus_token"}}
    except Exception: return {}

def save(values: dict[str, str]) -> None:
    target = path(); target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".tmp")
    temp.write_bytes(base64.b64encode(crypt(json.dumps(values, ensure_ascii=False).encode())))
    temp.replace(target)
