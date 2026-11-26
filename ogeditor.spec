import json
from importlib.metadata import distribution
from pathlib import Path
import sys


app = Path(SPECPATH).resolve()
sys.path.insert(0, str(app))
from ogeditor.paths import PSP_REFERENCE, editor_data, psp_reference, workspace_root
from ogeditor.psp.native_tables import read_resource_catalog

workspace = workspace_root(app)
stage = psp_reference(workspace)
readers = Path('02_修改工具/Blender_MDL/当前版本/Blender_MDL编辑与替换工具_v1.1.0/io_scene_kgf_mdl')
workshop = Path('02_修改工具/猪猪修改工坊')
enc = Path('02_修改工具/PSP统一工具/Source/ENC')

# The shared reader scripts locate their sibling packages relative to the workspace root.
data = [(str(app / 'web'), 'web'), (str(app / 'ogeditor.ico'), '.'), (str(editor_data(app, workspace, 'select_page_bases.dat')), '.')]
data.append((str(editor_data(app, workspace, 'move_bank_layouts.dat')), '.'))
fbx_reader = distribution('assimp-py')
data.extend((str(fbx_reader.locate_file(path)), str(path.parent))
            for path in fbx_reader.files if 'licenses' in path.parts)
# Pack the fields used to open an image; source ISO paths and report columns
# are local build inputs. Keep baseline validation separate from resource loading.
generated = Path(workpath) / 'editor-reference'
generated.mkdir(parents=True, exist_ok=True)
baseline_name = '基准与ISO目录.json'
baseline = json.loads((stage / baseline_name).read_text(encoding='utf-8'))
required = {'eboot': {k: baseline['eboot'][k] for k in ('bytes', 'sha256')}, 'elf': baseline['elf']}
for name, content in ((baseline_name, required), ('resources.json', read_resource_catalog(stage))):
    path = generated / name
    path.write_text(json.dumps(content, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    data.append((str(path), str(PSP_REFERENCE)))
for name in ('build_view.py', 'scan_assets.py', 'EBOOT.BIN'):
    data.append((str(stage / '证据' / name), str(PSP_REFERENCE / '证据')))
for folder, files in (
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
    icon=str(app / 'ogeditor.ico'),
    console=False,
    upx=False,
)
