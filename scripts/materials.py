#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""材料表查询。

数据源就是 references/materials.md，改那张表即生效，不在别处重复维护。
用法:
    python materials.py           列出全部
    python materials.py PLA       查某一种（支持模糊）
"""
import os
import re
import sys

MD = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                  '..', 'references', 'materials.md')


def _cell(s):
    return s.replace('*', '').replace('`', '').strip()


def load_table(path=None):
    """解析 markdown 表格，返回 [{name, density, price, note}]"""
    path = path or MD
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line.startswith('|'):
                continue
            cells = [_cell(c) for c in line.strip('|').split('|')]
            if len(cells) < 4:
                continue
            name, dens, price = cells[0], cells[1], cells[2]
            if not re.match(r'^[\d.]+$', dens):
                continue  # 表头或分隔行
            try:
                d = float(dens)
                p = float(price) if re.match(r'^[\d.]+$', price) else None
            except ValueError:
                continue
            # 「你的单价」列有值就优先
            yours = cells[5] if len(cells) > 5 else ''
            if re.match(r'^[\d.]+$', yours):
                p = float(yours)
            out.append({'name': name, 'density': d, 'price': p,
                        'confidence': cells[4] if len(cells) > 4 else ''})
    return out


def lookup(query, table=None):
    """按名称查材料，先精确后包含"""
    table = table or load_table()
    q = query.strip().lower()
    for m in table:
        if m['name'].lower() == q:
            return m
    for m in table:
        if q in m['name'].lower() or m['name'].lower() in q:
            return m
    return None


def main():
    table = load_table()
    if len(sys.argv) > 1:
        m = lookup(sys.argv[1], table)
        if not m:
            print('未找到：%s' % sys.argv[1])
            print('可用材料：%s' % '、'.join(x['name'] for x in table))
            return
        print('%s  密度 %s g/cm³  单价 %s 元/g  置信 %s'
              % (m['name'], m['density'], m['price'], m['confidence'] or '-'))
        return
    print('共 %d 种\n' % len(table))
    for m in table:
        print('%-24s 密度 %-6s 单价 %-8s %s'
              % (m['name'], m['density'], m['price'], m['confidence']))


if __name__ == '__main__':
    main()
