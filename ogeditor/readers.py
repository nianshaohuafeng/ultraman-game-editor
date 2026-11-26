"""Locate the existing native readers in the source tree or the standalone EXE."""

from pathlib import Path
import sys

from ogeditor.paths import editor_data, workspace_root


HERE = Path(__file__).resolve().parents[1]

ROOT = workspace_root(HERE)
STAGE = ROOT / '04_补丁与研究/KGF_v7_阶段1_EBOOT分析'
for folder in (STAGE / '证据', ROOT / '02_修改工具/PSP统一工具/Source/ENC'):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

# scan_assets registers the shared Blender-independent kgf_readers package.
from scan_assets import iso_directory, read_mdl, read_yum
from kgf_readers.pom_to_png import render_pom_png
from kgf_readers.pom_to_tm2 import get_pom_layout
from decrypt_enc import encode_dfs_enc
from iso_resources import list_iso_files, read_iso_file
