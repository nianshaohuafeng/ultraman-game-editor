from importlib.metadata import distribution
from pathlib import Path
import sys


app = Path(SPECPATH).resolve()
sys.path.insert(0, str(app))
from ogeditor.paths import editor_data, workspace_root

workspace = workspace_root(app)
stage = Path('04_补丁与研究/KGF_v7_阶段1_EBOOT分析')
readers = Path('02_修改工具/Blender_MDL/当前版本/Blender_MDL编辑与替换工具_v1.1.0/io_scene_kgf_mdl')
workshop = Path('02_修改工具/猪猪修改工坊')
enc = Path('02_修改工具/PSP统一工具/Source/ENC')

# Keep the source layout for the shared readers and the v7 reference data.
data = [(str(app / 'web'), 'web'), (str(app / 'KGF-v7.ico'), '.'), (str(editor_data(app, workspace, 'select_page_bases.dat')), '.')]
data.append((str(editor_data(app, workspace, 'move_bank_layouts.dat')), '.'))
fbx_reader = distribution('assimp-py')
data.extend((str(fbx_reader.locate_file(path)), str(path.parent))
            for path in fbx_reader.files if 'licenses' in path.parts)
for folder, files in (
    (stage, ('基准与ISO目录.json', '角色资源绑定.json', '角色动作映射.csv', 'YUM资源索引.csv')),
    (stage / '证据', ('build_view.py', 'scan_assets.py', 'EBOOT.BIN')),
    (readers, ('iso_io.py', 'iso_backend.py', 'mdl_reader.py', 'layouts.py', 'mesh_merge.py', 'native_writer.py',
               'pom_to_png.py', 'pom_to_tm2.py', 'texture_fit.py')),
    (workshop, ('pig_attack_data.py',)),
    (enc, ('iso_resources.py', 'decrypt_enc.py')),
):
    data.extend((str(workspace / folder / name), str(folder)) for name in files)

a = Analysis(
    [str(app / 'viewer.py')],
    datas=data,
    hiddenimports=['webview.platforms.winforms', 'webview.platforms.edgechromium'],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='奥格资源编辑器融合版本',
    icon=str(app / 'KGF-v7.ico'),
    console=False,
    upx=False,
)
