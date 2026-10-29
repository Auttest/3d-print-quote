#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""3D 打印报价 — 纯算术，不碰文件

用法:
    # 切片文件路径——直接给真实耗材克重
    python quote.py --mass 103.41 --price 0.10 [--hours 2.79] [--qty 1]
                    [--waste 0.12] [--setup 30] [--rate 1.2] [--margin 0.30]

    # 几何文件路径（STL/3MF）——给体积，脚本按填充系数估算耗材
    python quote.py --volume 355.139 --material PLA [--fill 0.25]
                    [--waste 0.20] [--qty 1] [--margin 0.30]

约定:
    --margin 是毛利率（不是加价率）。报价 = 成本 / (1 - 毛利率)
    起步价只收一次，不随数量放大
    损耗率区分两种口径:
        --waste 0.12  用于切片文件路径（耗材已含支撑，只补失败与清喉）
        --waste 0.20  用于 STL/3MF 路径
    填充系数 --fill 默认 0.25，只在几何路径生效:
        STL/3MF 给的是「实心体积」，真实打印不实心。
        实测配对样本: 实心 440.4g vs 实际耗材 95.57g → 系数 21.7%
        不同模型填充占比不同，没有固定值，25% 是拍板的默认值。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from materials import lookup as _lookup  # noqa: E402


def quote(mass_g, unit_price, qty=1, hours=0.0, hourly_rate=1.2,
          waste=0.20, setup=0.0, margin=0.30, min_profit_per_piece=0.0):
    mat = mass_g * unit_price * qty
    waste_cost = mat * waste
    machine = hours * hourly_rate * qty if hours else 0.0
    cost = mat + waste_cost + setup + machine

    price = cost / (1.0 - margin) if margin < 1 else 0.0

    profit = price - cost
    notes = []
    if min_profit_per_piece:
        floor = cost + min_profit_per_piece * qty
        floor_price = floor / (1.0 - margin) if margin < 1 else 0.0
        if floor_price > price:
            notes.append('触发单件毛利下限，报价已上调至 %.2f' % floor_price)
            price = floor_price
            profit = price - cost

    return {
        'input': {
            'mass_g': round(mass_g, 2), 'unit_price': unit_price, 'qty': qty,
            'hours': round(hours, 2), 'hourly_rate': hourly_rate,
            'waste': waste, 'setup': setup, 'margin': margin,
        },
        'breakdown': {
            'a_material': round(mat, 2),
            'b_waste': round(waste_cost, 2),
            'c_setup': round(setup, 2),
            'd_machine': round(machine, 2),
            'cost_total': round(cost, 2),
        },
        'price': round(price, 2),
        'profit': round(profit, 2),
        'margin_check': round(profit / price, 4) if price else 0,
        'unit_price_out': round(price / qty, 2) if qty else 0,
        'notes': notes,
    }


def main():
    ap = argparse.ArgumentParser(description='3D 打印报价计算')
    ap.add_argument('--mass', type=float, default=None,
                    help='真实耗材克重 g（切片文件路径用这个）')
    ap.add_argument('--volume', type=float, default=None,
                    help='几何体积 cm³（STL/3MF 路径用这个，配 --fill 估算耗材）')
    ap.add_argument('--fill', type=float, default=0.25,
                    help='填充系数，默认 0.25；只在 --volume 路径生效')
    ap.add_argument('--density', type=float, default=None,
                    help='密度 g/cm³；不给则按 --material 查表')
    ap.add_argument('--material', help='材料名，自动查 materials.md 取单价和密度')
    ap.add_argument('--price', type=float, default=None,
                    help='材料单价 元/克；不给则按 --material 查表')
    ap.add_argument('--qty', type=int, default=1)
    ap.add_argument('--hours', type=float, default=0.0, help='单件打印小时数')
    ap.add_argument('--rate', type=float, default=1.2, help='机时费率 元/小时（折旧口径）')
    ap.add_argument('--waste', type=float, default=0.20, help='损耗率，小数')
    ap.add_argument('--setup', type=float, default=0.0, help='起步价 元（隐藏项，用户不说就不管）')
    ap.add_argument('--margin', type=float, default=0.30, help='毛利率，小数')
    ap.add_argument('--min-profit', type=float, default=0.0, help='单件毛利下限 元')
    a = ap.parse_args()

    mat = _lookup(a.material) if a.material else None
    if a.material and not mat:
        ap.error('材料表里查不到「%s」' % a.material)

    price = a.price
    if price is None:
        if not mat or mat.get('price') is None:
            ap.error('需要 --price，或给一个材料表里能查到单价的 --material')
        price = mat['price']

    # 耗材克重: 真实值（切片路径）与估算值（几何路径）二选一
    if a.mass is not None and a.volume is not None:
        ap.error('--mass 和 --volume 只能给一个')
    if a.mass is not None:
        mass, mass_source, dens = a.mass, 'given', a.density
    elif a.volume is not None:
        dens = a.density
        if dens is None:
            if not mat or mat.get('density') is None:
                ap.error('用 --volume 需要密度: 给 --density，或给带密度的 --material')
            dens = mat['density']
        mass = a.volume * dens * a.fill
        mass_source = 'estimated'
    else:
        ap.error('需要 --mass（真实耗材）或 --volume（几何体积）之一')

    r = quote(mass, price, a.qty, a.hours, a.rate,
              a.waste, a.setup, a.margin, a.min_profit)
    r['input']['mass_source'] = mass_source
    if mass_source == 'estimated':
        r['input']['volume_cm3'] = round(a.volume, 3)
        r['input']['density'] = round(dens, 4)
        r['input']['fill'] = a.fill
        r['notes'].append(
            '耗材 %.2fg 为估算值: %.3f cm³ × %.2f g/cm³ × %.0f%% 填充'
            % (mass, a.volume, dens, a.fill * 100))
    if mat:
        r['material'] = mat['name']
        r['density'] = mat['density']
    print(json.dumps(r, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
