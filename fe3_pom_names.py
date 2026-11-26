"""Names for standalone FE3 textures identified by their native consumers."""


def standalone_pom_names(tables, roster):
    names = {
        0x0A65: 'rogo',                 # Logo flow 0x2952C8
        0x06EF: 'IPU 播放器贴图',        # 0x32B4C8
        0x0A5C: '剧情影片叠加图',         # 0x21ACC8
        tables.pointer(0x4B9210, 0): '练习菜单字体',
    }
    for index, resource in enumerate(tables.words(0x40A9B8, 2)):
        names[resource] = f'战斗 HUD 图集 {index + 1}'
    # The two tutorial presets share every sheet except the charge prompt.
    auto = tables.words(0x495270, 7)
    manual = tables.words(0x495290, 7)
    for index, (left, right) in enumerate(zip(auto, manual)):
        if left == right:
            names[left] = f'教学提示图集 {index + 1}'
        else:
            names[left] = '教学充能提示 · 自动'
            names[right] = '教学充能提示 · 手动'
    # The subsequent entries in this table are FUM children with their own names.
    for index, resource in enumerate(tables.words(0x40FD30, 103)):
        names[resource] = f'解锁通知图块 {index:03d}'
    for role, (_, name) in enumerate(roster):
        resource, _ = tables.skill_loading_source(role)
        names[resource] = f'{name} · 加载页技能名称'
    return names
