#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""零依赖网格解析：STL（二进制/ASCII）+ 3MF。

不依赖 numpy / trimesh，标准库即可跑。
四面体体积法（散度定理）：V = |Σ v0 · (v1 × v2)| / 6
水密性用边计数法判定：每条边恰好被两个三角形共享即封闭。
"""
import os
import re
import struct
import zipfile
import xml.etree.ElementTree as ET

NS = '{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}'


# ---------- STL ----------

def _is_binary_stl(path):
    try:
        with open(path, 'rb') as f:
            head = f.read(84)
        if len(head) < 84:
            return False
        n = struct.unpack('<I', head[80:84])[0]
        return n > 0 and (os.path.getsize(path) - 84) == n * 50
    except Exception:
        return False


def read_stl_binary(path):
    tris = []
    with open(path, 'rb') as f:
        f.read(80)
        (n,) = struct.unpack('<I', f.read(4))
        data = f.read(n * 50)
    for i in range(n):
        o = i * 50 + 12  # 跳过法线
        v = struct.unpack_from('<9f', data, o)
        tris.append(((v[0], v[1], v[2]), (v[3], v[4], v[5]), (v[6], v[7], v[8])))
    return tris


def read_stl_ascii(path):
    tris, cur = [], []
    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            s = line.strip()
            if s.startswith('vertex'):
                p = s.split()[1:4]
                cur.append((float(p[0]), float(p[1]), float(p[2])))
                if len(cur) == 3:
                    tris.append(tuple(cur))
                    cur = []
    return tris


def read_stl(path):
    return read_stl_binary(path) if _is_binary_stl(path) else read_stl_ascii(path)


# ---------- 3MF ----------

def _parse_model_xml(xml):
    """解析单个 3MF model XML，返回 (面列表, object 数, 声明单位)"""
    tris = []
    root = ET.fromstring(xml)
    m = re.search(r'\bunit\s*=\s*["\']([^"\']+)', xml[:2000], re.I)
    unit = m.group(1) if m else None

    objects = len(list(root.iter(NS + 'object')))
    for obj in root.iter(NS + 'object'):
        vm = obj.find(NS + 'mesh')
        if vm is None:
            continue
        verts = []
        vv = vm.find(NS + 'vertices')
        if vv is not None:
            # 3MF 的 vertex 没有 id，按出现顺序索引
            verts = [(float(v.get('x')), float(v.get('y')), float(v.get('z')))
                     for v in vv.findall(NS + 'vertex')]
        tv = vm.find(NS + 'triangles')
        if tv is not None:
            n = len(verts)
            for t in tv.findall(NS + 'triangle'):
                try:
                    a, b, c = int(t.get('v1')), int(t.get('v2')), int(t.get('v3'))
                except (TypeError, ValueError):
                    continue
                if 0 <= a < n and 0 <= b < n and 0 <= c < n:
                    tris.append((verts[a], verts[b], verts[c]))
    return tris, objects, unit


def read_3mf_triangles(path):
    """返回 (面列表, 声明单位, object 数)。

    多物体 3MF 会把网格拆到 3D/Objects/*.model，主文件只留 component 引用，
    所以主文件和分文件都要读。object 的 transform 不展开（平移不影响体积）。
    """
    unit, tris, objects = None, [], 0
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if n.lower().endswith('.model')]
        if not names:
            return [], None, 0
        main = None
        for n in names:
            low = n.lower()
            if low.startswith('3d/') and '/' not in low[3:-6]:
                main = n
                break
        ordered = ([main] if main else []) + [x for x in names if x != main]

        for n in ordered:
            try:
                t, o, u = _parse_model_xml(z.read(n).decode('utf-8', 'ignore'))
            except Exception:
                continue
            tris.extend(t)
            objects += o
            if unit is None and u:
                unit = u
    return tris, unit, objects


# ---------- 统计 ----------

def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _key(p):
    return (round(p[0], 6), round(p[1], 6), round(p[2], 6))


def mesh_stats(tris):
    """体积 mm³、面积 mm²、包围盒、面数、水密性"""
    if not tris:
        return None
    vol6 = 0.0
    area = 0.0
    lo = [float('inf')] * 3
    hi = [float('-inf')] * 3
    edges = {}

    for t in tris:
        v0, v1, v2 = t
        vol6 += _dot(v0, _cross(v1, v2))
        e1 = (v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2])
        e2 = (v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2])
        c = _cross(e1, e2)
        area += (c[0] ** 2 + c[1] ** 2 + c[2] ** 2) ** 0.5 / 2.0
        for p in t:
            for i in range(3):
                if p[i] < lo[i]:
                    lo[i] = p[i]
                if p[i] > hi[i]:
                    hi[i] = p[i]
        for i in range(3):
            a, b = _key(t[i]), _key(t[(i + 1) % 3])
            k = (a, b) if a <= b else (b, a)
            edges[k] = edges.get(k, 0) + 1

    bbox = [hi[i] - lo[i] for i in range(3)]
    return {
        'volume_mm3': abs(vol6) / 6.0,
        'area_mm2': area,
        'bbox': bbox,
        'faces': len(tris),
        'watertight': all(c == 2 for c in edges.values()) if edges else False,
        'open_edges': sum(1 for c in edges.values() if c != 2),
    }


def load(path, kind):
    """按类型加载，返回 (stats, unit, objects)"""
    if kind in ('3mf', 'gcode_3mf'):
        tris, unit, objects = read_3mf_triangles(path)
    else:
        tris, unit, objects = read_stl(path), None, 1
    if not tris:
        return None, unit, objects
    return mesh_stats(tris), unit, objects
