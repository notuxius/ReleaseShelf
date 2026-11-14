from pathlib import Path
from runpy import run_path

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
)

root = Path(SPECPATH)
version = run_path(str(root / 'shelf_version.py'))['__version__']
# Windows stores four numbers in the fixed resource and the full SemVer string in the text fields.
core_version = version.split('+')[0].split('-')[0]
windows_version = (*map(int, core_version.split('.')), 0)
metadata = VSVersionInfo(
    FixedFileInfo(filevers=windows_version, prodvers=windows_version),
    [StringFileInfo([StringTable('040904B0', [
        StringStruct('ProductName', 'Relf'),
        StringStruct('FileDescription', 'Relf'),
        StringStruct('FileVersion', version),
        StringStruct('ProductVersion', version),
    ])]), VarFileInfo([VarStruct('Translation', [0x0409, 1200])])],
)

app = Analysis([str(root / 'relf_ui.pyw')], datas=[
    (str(root / 'assets'), 'assets'), (str(root / 'locales' / '*.json'), 'locales'),
])
exe = EXE(
    PYZ(app.pure), app.scripts, app.binaries, app.datas, [],
    name='Relf', console=False, icon=str(root / 'assets' / 'relf.ico'), version=metadata,
)
