# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all
from pathlib import Path

datas = []
binaries = []
hiddenimports = []
tmp_ret = collect_all('playwright')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('rapidocr_onnxruntime')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('onnxruntime')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# The CUDA wheel also ships optional FFT, random-number, and BLAS libraries.
# They are not used by ONNX Runtime's CUDA execution provider, and including
# them makes the NSIS data block approach the 2 GiB limit on Windows.
for package_name in ('nvidia.cu13', 'nvidia.cudnn'):
    try:
        tmp_ret = collect_all(package_name)
    except ImportError:
        continue
    datas += tmp_ret[0]
    if package_name == 'nvidia.cu13':
        optional_cuda_dlls = {
            'cufft64_12.dll',
            'cufftw64_12.dll',
            'curand64_10.dll',
            'nvblas64_13.dll',
        }
        binaries += [
            item for item in tmp_ret[1]
            if Path(item[0]).name.lower() not in optional_cuda_dlls
        ]
    else:
        binaries += tmp_ret[1]
    hiddenimports += tmp_ret[2]
tmp_ret = collect_all('cv2')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('PIL')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('webview')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('clr_loader')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pythonnet')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['package_entry.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
# Some third-party PyInstaller hooks add the complete CUDA wheel again after
# the explicit collection above. Filter the final TOCs as a last line of
# defense so optional libraries cannot re-enter the sidecar.
optional_cuda_names = {
    'cufft64_12.dll',
    'cufftw64_12.dll',
    'curand64_10.dll',
    'nvblas64_13.dll',
}
a.binaries = [
    item for item in a.binaries
    if Path(item[0]).name.lower() not in optional_cuda_names
]
a.datas = [
    item for item in a.datas
    if Path(item[0]).name.lower() not in optional_cuda_names
]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='NO钱包自动化',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    # The Tauri host starts this executable with CREATE_NO_WINDOW.  Keep the
    # console subsystem here so a dashboard-launched child can redirect its
    # stdout/stderr into dashboard-bot.log instead of losing every print().
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='NO钱包自动化',
)
