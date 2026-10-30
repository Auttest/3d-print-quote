#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""3D 打印报价 — 文件解析与体检

用法:
    python analyze.py <文件路径> [--json]

输出 JSON，字段含义见 SKILL.md。
脚本只负责"算出数值 + 标出可疑点"，不替用户做判断。
单位判断、材料选择、损耗取值等语义决策交给 AI。
"""
import argparse
import json
import math
import os
import re
import sys
import zipfile

# 成型体积（mm），X × Y × Z。按真实机型填，改这里即可。
# 超长件可以沿平台对角线斜放，所以判断能不能打不能只比最长边——
# 341mm 的件斜着有可能塞进 330mm 的机器，得把绕 Z 轴的旋转算进去。
BUILD_VOLUME_MM = (330.0, 320.0, 325.0)
# 按 mm 解析后小于该值 → 怀疑文件其实用 inch 写的，×25.4 才是真实尺寸
INCH_SUSPECT_MM = 8.0
INCH = 25.4
# 一个工程常切成多盘（plate_1..plate_N），每盘都有独立 GCode。
# 大盘只读头部——切片统计都在开头（Bambu/Prusa/Cura 都是），没必要读几百 MB。
GCODE_HEAD_BYTES = 65536
# 小于这个体积的 GCode 读全文，能拿到完整换料指令（判断多材料更准）
GCODE_FULL_MAX_BYTES = 2 * 1024 * 1024


def fit_in_build(bbox, build=BUILD_VOLUME_MM):
    """判断包围盒能否放进成型体积，允许轴对齐置换 + 绕 Z 轴旋转（对角线摆放）。

    返回 dict：fits / mode(axis|diagonal|none) / angle_deg / placed_mm / note
    """
    dims = [float(x) for x in bbox]
    best = None
    for hi in range(3):                      # 件的哪条边朝上当高度
        h = dims[hi]
        rest = [dims[i] for i in range(3) if i != hi]
        u, v = rest[0], rest[1]
        for mzi in range(3):                 # 机器哪条轴当 Z
            mz = build[mzi]
            if h > mz + 1e-9:
                continue
            rest_m = [build[i] for i in range(3) if i != mzi]
            mx, my = rest_m[0], rest_m[1]
            for i in range(181):             # 绕 Z 轴 0~90°，步长 0.5°
                th = math.radians(i * 0.5)
                w = u * math.cos(th) + v * math.sin(th)
                d = u * math.sin(th) + v * math.cos(th)
                if w <= mx + 1e-9 and d <= my + 1e-9:
                    cand = {
                        '_i': i,
                        'fits': True,
                        'mode': 'axis' if i == 0 else 'diagonal',
                        'angle_deg': round(i * 0.5, 1),
                        'placed_mm': [round(w, 1), round(d, 1), round(h, 1)],
                        'build_mm': list(build),
                    }
                    if best is None or i < best['_i']:
                        best = cand
                    break
    if best is None:
        return {
            'fits': False,
            'mode': 'none',
            'angle_deg': None,
            'placed_mm': None,
            'build_mm': list(build),
            'note': '放不进 %.0f×%.0f×%.0f 成型空间（已试轴对齐与对角线旋转），需分件或换更大机型' % build,
        }
    best.pop('_i', None)
    w, d, h = best['placed_mm']
    if best['mode'] == 'axis':
        best['note'] = '轴对齐可放，占地 %.1f×%.1f×%.1f mm' % (w, d, h)
    else:
        best['note'] = '需沿平台对角线斜放（绕 Z 轴约 %.1f°），占地 %.1f×%.1f×%.1f mm' % (
            best['angle_deg'], w, d, h)
    return best


def detect_kind(path):
    low = path.lower()
    if low.endswith('.gcode.3mf'):
        return 'gcode_3mf'
    if low.endswith('.3mf'):
        return '3mf'
    if low.endswith('.bgcode'):
        return 'bgcode'
    if low.endswith('.gcode') or low.endswith('.gco'):
        return 'gcode'
    if low.endswith('.stl'):
        return 'stl'
    return 'unknown'


# ---------- GCode 文本解析 ----------

def parse_hms(s):
    """解析 '2h 47m 35s' / '1h23m45s' / '2h 47m' 为秒"""
    h = re.search(r'(\d+)\s*h', s, re.I)
    m = re.search(r'(\d+)\s*m(?!m)', s, re.I)
    sec = re.search(r'(\d+(?:\.\d+)?)\s*s', s, re.I)
    return (int(h.group(1)) if h else 0) * 3600 \
        + (int(m.group(1)) if m else 0) * 60 \
        + (float(sec.group(1)) if sec else 0.0)


def _f(pattern, text, group=1):
    m = re.search(pattern, text, re.I | re.M)
    return float(m.group(group)) if m else None


def parse_gcode_text(text):
    """从 GCode 注释里提取切片结果。兼容 Bambu/Orca、PrusaSlicer、Cura。"""
    out = {}
    warnings = []

    # --- 打印时长 ---
    t = None
    m = re.search(r'total estimated time\s*:\s*([0-9hms\s]+)', text, re.I)
    if not m:
        m = re.search(r'model printing time\s*:\s*([0-9hms\s]+)', text, re.I)
    if m:
        t = parse_hms(m.group(1))
    if t is None:
        m = re.search(r'estimated printing time[^=\n]*=\s*([0-9hms\s]+)', text, re.I)
        if m:
            t = parse_hms(m.group(1))
    if t is None:
        m = re.search(r'^\s*;TIME:\s*(\d+)', text, re.M)
        if m:
            t = float(m.group(1))
    if t:
        out['print_time_s'] = round(t, 1)
        out['print_time_hms'] = '%dh %dm' % (int(t // 3600), int((t % 3600) // 60))

    # --- 耗材重量（优先，最可靠）---
    g = _f(r'total filament weight\s*\[g\]\s*[:=]\s*([\d.]+)', text)
    if g is None:
        g = _f(r'(?:total )?filament used\s*\[g\]\s*[:=]\s*([\d.]+)', text)
    if g is None:
        g = _f(r'filament weight\s*[:=]\s*([\d.]+)', text)

    # --- 耗材长度 ---
    mm = _f(r'total filament length\s*\[mm\]\s*[:=]\s*([\d.]+)', text)
    if mm is None:
        m = re.search(r'Filament used:\s*([\d.]+)\s*m(?![m])', text, re.I)  # Cura 用米
        if m:
            mm = float(m.group(1)) * 1000.0
    if mm is None:
        m = re.search(r'filament used\s*=\s*([\d.]+)\s*mm', text, re.I)
        if m:
            mm = float(m.group(1))

    # --- 密度 / 丝径 ---
    # filament_density 是 AMS 槽位清单（逗号分隔），不全是实际用料。
    # 真正的用料要看换料指令 T#：全程只有 T0 就说明只用了一号槽的料。
    density = None
    dens_raw = re.search(r'filament_density\s*[:=]\s*([\d.,]+)', text)
    diam_raw = re.search(r'filament_diameter\s*[:=]\s*([\d.,]+)', text)

    tools = sorted({int(t) for t in re.findall(r'(?m)^\s*T(\d+)', text)
                    if int(t) < 100})
    out['tools_used'] = tools if tools else [0]

    if dens_raw:
        vals = [float(x) for x in dens_raw.group(1).split(',') if x]
        idx = tools[0] if tools and tools[0] < len(vals) else 0
        density = vals[idx]
        out['density_all_slots'] = vals

    diameter = None
    if diam_raw:
        dvals = [float(x) for x in diam_raw.group(1).split(',') if x]
        idx = tools[0] if tools and tools[0] < len(dvals) else 0
        diameter = dvals[idx]

    if len(tools) > 1:
        warnings.append('MULTI_MATERIAL: 检测到换料（T%s），多种材料的单价和清喉损耗需分别计' %
                        ', T'.join(str(t) for t in tools))
    if diameter is None:
        diameter = _f(r'^;\s*filament_diameter\s*=\s*([\d.]+)', text)

    # 长度换算克重（1.75mm 丝径时系数 2.405）
    if g is None and mm is not None and density:
        d = diameter or 1.75
        g = mm / 1000.0 * (math.pi * (d / 2.0) ** 2) * density

    if g is not None:
        out['filament_g'] = round(g, 2)
    if mm is not None:
        out['filament_mm'] = round(mm, 2)
    if density:
        out['density'] = density
    if diameter:
        out['diameter'] = diameter

    # Bambu 的 total filament volume [cm^3] 字段单位标错（实为 mm³），不采用
    if _f(r'total filament volume\s*\[cm\^3\]\s*[:=]\s*([\d.]+)', text) and g is not None:
        warnings.append('VOLUME_FIELD_UNRELIABLE: 切片头的 filament volume 字段单位标注有误，已改用重量字段')

    # --- 层高 / 层数 / 喷嘴 ---
    lh = _f(r'^\s*;\s*layer_height\s*=\s*([\d.]+)', text)
    if lh is None:
        lh = _f(r'^\s*;Layer height:\s*([\d.]+)', text)
    if lh:
        out['layer_height'] = lh

    layers = _f(r'total layer number\s*:\s*(\d+)', text)
    if layers:
        out['layers'] = int(layers)

    zh = _f(r'max_z_height\s*:\s*([\d.]+)', text)
    if zh:
        out['max_z_height'] = zh

    nz = _f(r'nozzle_diameter\s*[:=]\s*([\d.]+)', text)
    if nz:
        out['nozzle'] = nz

    # 材料类型（有则采信，多半没有）
    m = re.search(r'^\s*;\s*filament_type\s*=\s*(\S+)', text, re.M)
    if m:
        types = [x for x in re.split(r'[;,]', m.group(1)) if x]
        out['filament_type_all'] = types
        idx = tools[0] if tools and tools[0] < len(types) else 0
        out['filament_type'] = types[idx]

    # 物体数量（Bambu 的 object max height 是逗号分隔的多物体高度）
    m = re.search(r'object max height\s*:\s*([\d.,\s]+)', text)
    if m:
        parts = [p for p in m.group(1).split(',') if p.strip()]
        if len(parts) > 1:
            out['objects'] = len(parts)

    out['_warnings'] = warnings
    return out


# ---------- 3MF 容器 ----------

def read_3mf_package(path):
    """读取 3MF 包。返回 dict: model_xml / gcode_text / plates / plate_jsons / settings_text

    plates 是全部盘（plate_1..plate_N）的 GCode 列表，按盘号排序。
    多盘工程必须逐盘解析再累加——只读一盘会少算一个量级。
    """
    out = {'model_xml': None, 'gcode_text': None, 'plate_json': None,
           'settings_text': None, 'plates': [], 'plate_jsons': []}
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        for n in names:
            if n.lower().startswith('3d/') and n.lower().endswith('.model'):
                out['model_xml'] = z.read(n).decode('utf-8', 'ignore')
                break

        def _pno(n):
            m = re.search(r'plate_(\d+)', n)
            return int(m.group(1)) if m else 0

        gnames = [n for n in names if re.match(r'(?i)^metadata/plate_\d+\.gcode$', n)]
        gnames.sort(key=_pno)
        if not gnames:      # 没有 plate_N 命名，退回任意 gcode
            gnames = [n for n in names
                      if n.lower().endswith('.gcode') and not n.lower().endswith('.md5')]
            gnames.sort()
        for n in gnames:
            info = z.getinfo(n)
            with z.open(n) as f:
                if info.file_size <= GCODE_FULL_MAX_BYTES:
                    text = f.read().decode('utf-8', 'ignore')
                else:
                    text = f.read(GCODE_HEAD_BYTES).decode('utf-8', 'ignore')
            out['plates'].append({'name': n, 'text': text})
        if out['plates']:
            out['gcode_text'] = out['plates'][0]['text']

        for n in sorted([x for x in names
                         if re.match(r'(?i)^metadata/plate_\d+\.json$', x)], key=_pno):
            try:
                out['plate_jsons'].append(json.loads(z.read(n).decode('utf-8', 'ignore')))
            except Exception:
                pass
        if out['plate_jsons']:
            out['plate_json'] = out['plate_jsons'][0]
        # Bambu / Orca 工程文件: 完整切片参数（纯 JSON）
        for n in names:
            if n.lower().endswith('project_settings.config'):
                out['settings_text'] = z.read(n).decode('utf-8', 'ignore')
                break
    return out


def merge_plates(parsed):
    """多盘合并：耗材 / 时长 / 丝长累加，层数累加，最高件取最大。

    层高、喷嘴、密度、料型以首盘为准——各盘用料不同会在 warnings 里点出来。
    """
    if len(parsed) == 1:
        return parsed[0], []
    warns = []
    g = sum(p.get('filament_g') or 0.0 for p in parsed)
    s = sum(p.get('print_time_s') or 0.0 for p in parsed)
    mm = sum(p.get('filament_mm') or 0.0 for p in parsed)
    merged = dict(parsed[0])
    merged['filament_g'] = round(g, 2)
    merged['print_time_s'] = round(s, 1)
    merged['print_time_hms'] = '%dh %dm' % (int(s // 3600), int((s % 3600) // 60))
    if mm:
        merged['filament_mm'] = round(mm, 2)
    layers = [p.get('layers') for p in parsed if p.get('layers')]
    if layers:
        merged['layers'] = sum(layers)
    mz = [p.get('max_z_height') for p in parsed if p.get('max_z_height')]
    if mz:
        merged['max_z_height'] = max(mz)
    objs = [p.get('objects') for p in parsed if p.get('objects')]
    if objs:
        merged['objects'] = sum(objs)

    types = []
    for p in parsed:
        t = p.get('filament_type')
        if t and t not in types:
            types.append(t)
    if len(types) > 1:
        by_mat = {}
        for p in parsed:
            t = p.get('filament_type') or '未知'
            by_mat[t] = round(by_mat.get(t, 0.0) + (p.get('filament_g') or 0.0), 2)
        merged['filament_g_by_material'] = by_mat
        warns.append('MULTI_MATERIAL_PLATES: 各盘用料不同，按材料分开算（%s），'
                     '别用同一个单价' % ' / '.join('%s %.2fg' % (k, v)
                                                for k, v in sorted(by_mat.items())))
    tools = sorted({t for p in parsed for t in (p.get('tools_used') or [])})
    if tools:
        merged['tools_used'] = tools
    merged['plate_count'] = len(parsed)
    return merged, warns


# 报价用得上的切片参数，其余忽略
SLICE_PARAM_KEYS = [
    'layer_height', 'sparse_infill_density', 'sparse_infill_pattern',
    'wall_loops', 'top_shell_layers', 'bottom_shell_layers',
    'enable_support', 'nozzle_diameter',
]


def parse_slice_settings(text):
    """从 Bambu/Orca 工程 3MF 的 project_settings.config 提取切片参数。

    注意: 这些是「设置值」不是「结果值」——知道填充 15%，仍不知道真实耗材多少。
    所以只用于信息透明，不参与耗材计算。
    """
    if not text:
        return None
    try:
        cfg = json.loads(text)
    except Exception:
        return None
    if not isinstance(cfg, dict):
        return None
    out = {}
    for k in SLICE_PARAM_KEYS:
        v = cfg.get(k)
        if isinstance(v, list):          # 多挤出机时值是数组，取第一个
            v = v[0] if v else None
        if v is None or v == '':
            continue
        out[k] = str(v)
    return out or None


# ---------- 几何 ----------

def load_geometry(path, kind):
    """用内置零依赖解析器，不走 trimesh"""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import mesh as _mesh

    if kind in ('3mf', 'gcode_3mf'):
        tris, unit, objects = _mesh.read_3mf_triangles(path)
    else:
        tris, unit, objects = _mesh.read_stl(path), None, 1

    if not tris:
        raise ValueError('未能从文件里读到网格')
    s = _mesh.mesh_stats(tris)

    return {
        'volume_cm3': round(s['volume_mm3'] / 1000.0, 3),
        'area_cm2': round(s['area_mm2'] / 100.0, 3),
        'bbox_mm': [round(x, 3) for x in s['bbox']],
        'max_dim_mm': round(max(s['bbox']), 3),
        'faces': int(s['faces']),
        'watertight': bool(s['watertight']),
        'open_edges': int(s['open_edges']),
        'objects': objects,
    }, unit


def unit_from_model_xml(xml_text):
    m = re.search(r'<model[^>]*\bunit\s*=\s*["\']([^"\']+)', xml_text or '', re.I)
    return m.group(1) if m else None


# ---------- 主流程 ----------

def analyze(path):
    if not os.path.exists(path):
        return {'error': 'FILE_NOT_FOUND', 'file': path}

    kind = detect_kind(path)
    result = {
        'file': os.path.basename(path),
        'path': os.path.abspath(path),
        'kind': kind,
        'size_bytes': os.path.getsize(path),
    }
    warnings = []

    if kind == 'unknown':
        result['error'] = 'UNSUPPORTED_FORMAT'
        return result

    if kind == 'bgcode':
        result['error'] = 'BINARY_GCODE'
        result['hint'] = 'Prusa 二进制 .bgcode 无法直接解析，请导出纯文本 .gcode，或改用模型文件走克重模型'
        return result

    pkg = {'model_xml': None, 'gcode_text': None, 'plate_json': None,
           'settings_text': None, 'plates': [], 'plate_jsons': []}
    if kind in ('3mf', 'gcode_3mf'):
        try:
            pkg = read_3mf_package(path)
        except Exception as e:
            warnings.append('PACKAGE_READ_FAIL: %s' % e)
    model_xml = pkg['model_xml']
    inner_gcode = pkg['gcode_text']
    plate_json = pkg['plate_json']

    # 几何
    geom = None
    geom_unit = None
    try:
        geom, geom_unit = load_geometry(path, kind)
        result['geometry'] = geom
    except Exception as e:
        if kind == 'gcode_3mf':
            # 切片包常不保留完整几何，属正常，用 GCode 耗材量替代
            warnings.append('NO_GEOMETRY_IN_SLICE_PKG: 切片包内无完整网格，改用 GCode 的耗材量（更准）')
        else:
            warnings.append('GEOMETRY_FAIL: %s' % e)

    # 单位
    declared = unit_from_model_xml(model_xml) or geom_unit
    if declared:
        result['unit'] = {'declared': declared, 'source': '3mf-spec'}
    elif geom:
        result['unit'] = {'declared': None, 'source': 'guess',
                          'note': 'STL 无单位标记，需结合模型用途判断'}

    # 尺寸适配：独立于单位判断。3MF 声明了单位也要查——
    # 单位没疑义不代表打得下，超长件还得看能不能摆进平台。
    if geom:
        fit = fit_in_build(geom['bbox_mm'])
        result['fit'] = fit
        md = geom['max_dim_mm']
        b = geom['bbox_mm']
        if not declared and md < INCH_SUSPECT_MM:
            warnings.append('UNIT_CHECK_SMALL: 最大边仅 %.1fmm，若按 inch 换算为 %.1fmm，请确认' % (md, md * INCH))
        if not fit['fits']:
            if declared:
                warnings.append('OVERSIZE_BUILD: %.1f×%.1f×%.1fmm 放不进 %.0f×%.0f×%.0f 成型空间'
                                '（已含对角线旋转），需分件或换更大机型'
                                % (b[0], b[1], b[2],
                                   BUILD_VOLUME_MM[0], BUILD_VOLUME_MM[1], BUILD_VOLUME_MM[2]))
            else:
                warnings.append('UNIT_CHECK_LARGE: 最大边 %.1fmm，按 mm 解开放不进 %.0f×%.0f×%.0f，'
                                '确认单位或机型' % (md, BUILD_VOLUME_MM[0], BUILD_VOLUME_MM[1],
                                                 BUILD_VOLUME_MM[2]))

    # 切片数据
    slice_info = None
    if kind == 'gcode':
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            slice_info = parse_gcode_text(f.read())
    elif pkg['plates']:
        parsed = []
        for p in pkg['plates']:
            si = parse_gcode_text(p['text'])
            if si and (si.get('filament_g') or si.get('print_time_s')):
                si['_plate'] = p['name'].split('/')[-1]
                parsed.append(si)
        if parsed:
            # 各盘的告警先收上来去重，再合并数值
            for x in parsed:
                for w in (x.pop('_warnings', None) or []):
                    if w not in warnings:
                        warnings.append(w)
            slice_info, pw = merge_plates(parsed)
            for w in pw:
                if w not in warnings:
                    warnings.append(w)
            result['plates'] = [
                {'plate': x['_plate'], 'material': x.get('filament_type'),
                 'filament_g': x.get('filament_g'),
                 'print_time_s': x.get('print_time_s'), 'layers': x.get('layers')}
                for x in parsed]
            names_src = ', '.join(x['_plate'] for x in parsed)
            result['slice_source'] = ('%d 盘合计（%s）' % (len(parsed), names_src)
                                      if len(parsed) > 1 else names_src)
            if len(parsed) > 1:
                warnings.append('MULTI_PLATE: 工程切成 %d 盘，耗材与时长已按全部盘累加，'
                                '别只拿单盘的数去报' % len(parsed))

    if slice_info:
        sw = slice_info.pop('_warnings', [])
        for w in sw:
            if w not in warnings:
                warnings.append(w)
        # 从 plate_N.json 补字段（多盘时零件数按全部盘累加）
        pj_all = pkg.get('plate_jsons') or []
        if pj_all:
            if 'nozzle' not in slice_info and pj_all[0].get('nozzle_diameter'):
                slice_info['nozzle'] = pj_all[0]['nozzle_diameter']
            if 'layer_height' not in slice_info:
                for pj in pj_all:
                    objs = pj.get('bbox_objects') or []
                    if objs and objs[0].get('layer_height'):
                        slice_info['layer_height'] = round(objs[0]['layer_height'], 3)
                        break
            if 'objects' not in slice_info:
                n = sum(len(pj.get('bbox_objects') or []) for pj in pj_all)
                if n:
                    slice_info['objects'] = n
        result['slice'] = slice_info
    else:
        if kind in ('stl', '3mf'):
            result['slice'] = None
            warnings.append('NO_SLICE_DATA: 未切片，算不了机时，走克重模型')

    # 切片参数（Bambu/Orca 工程 3MF）—— 有参数 ≠ 有真值
    if not slice_info and pkg['settings_text']:
        sp = parse_slice_settings(pkg['settings_text'])
        if sp:
            result['slice_params'] = sp
            result['slice_params_source'] = 'Metadata/project_settings.config'
            warnings.append(
                'SLICE_PARAMS_ONLY: 文件自带切片参数（填充 %s / 支撑 %s / 层高 %s），'
                '但没有 GCode，拿不到真实耗材与时长 —— 耗材仍按 25%% 估算，机时不计'
                % (sp.get('sparse_infill_density', '?'),
                   sp.get('enable_support', '?'),
                   sp.get('layer_height', '?')))

    # 水密性
    if geom and not geom['watertight']:
        warnings.append('NOT_WATERTIGHT: 网格不封闭，体积/克重不可靠，需修复或改用切片文件的耗材量')

    result['warnings'] = warnings

    # 报价路径 —— 给 AI 的直接指令，别让它自己推理
    if slice_info and slice_info.get('filament_g'):
        result['quote_path'] = 'true_mass'
        result['quote_hint'] = (
            '有真值: 用 --mass %.2f --hours %.2f --waste 0.12（计入机时）'
            % (slice_info['filament_g'],
               round((slice_info.get('print_time_s') or 0) / 3600.0, 2)))
    elif geom and geom.get('volume_cm3'):
        result['quote_path'] = 'estimated'
        result['quote_hint'] = (
            '无真值: 用 --volume %.3f --fill 0.25 --waste 0.20（不计机时）'
            % geom['volume_cm3'])
    else:
        result['quote_path'] = 'blocked'
        result['quote_hint'] = '既无耗材真值也无可用几何体积，先处理上面的告警'

    # 信息层级
    if slice_info:
        result['level'] = 'sliced'
    elif result.get('slice_params'):
        result['level'] = 'geometry+slice_params'
    elif kind == '3mf' or declared:
        result['level'] = 'geometry+meta'
    else:
        result['level'] = 'geometry'
    return result


def main():
    ap = argparse.ArgumentParser(description='3D 打印报价 — 文件解析与体检')
    ap.add_argument('path')
    ap.add_argument('--json', action='store_true', help='输出 JSON（默认即 JSON）')
    args = ap.parse_args()
    print(json.dumps(analyze(args.path), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
