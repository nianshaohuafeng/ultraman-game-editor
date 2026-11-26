"""Paths for an independent source checkout and the packaged editor."""
import os
import sys
from pathlib import Path


def workspace_root(app):
    if getattr(sys, 'frozen', False):
        return app
    supplied = os.environ.get('OG_EDITOR_WORKSPACE')
    return Path(supplied).expanduser().resolve() if supplied else app.parent.parent


def editor_data(app, workspace, name):
    local = app / name
    if local.is_file() or getattr(sys, 'frozen', False):
        return local
    return workspace / '02_修改工具' / '奥格资源编辑器融合版本' / name


PSP_REFERENCE = Path('reference/psp')


def psp_reference(workspace):
    packaged = workspace / PSP_REFERENCE
    if getattr(sys, 'frozen', False) or (packaged / '基准与ISO目录.json').is_file():
        return packaged
    # Existing workspaces keep their reference files in the original directory.
    return workspace / '04_补丁与研究/KGF_v7_阶段1_EBOOT分析'
