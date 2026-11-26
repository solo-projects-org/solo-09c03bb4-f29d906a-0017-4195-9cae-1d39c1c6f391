"""三学期去向表（三向运输问题）整数求解。

变量 w[i,j,k]：第一学期专业 i、第二学期专业 j、第三学期专业 k 的人数，非负整数。
约束：
  sum_k w[i,j,k] == a[i,j]   （第一至第二学期去向表，缺行按零）
  sum_i w[i,j,k] == b[j,k]   （第二至第三学期去向表，缺行按零）
  sum_j w[i,j,k] == z[i,k]   （已核定的首末专业对人数，仅部分对有此约束）

用 scipy.optimize.milp（HiGHS）求整数可行解 / 最优解。
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import lil_matrix

# (i, j) -> int
Margin2 = dict
# (i, j, k) -> int
Table3 = dict


class Infeasible(Exception):
    """约束整体不可行（不存在满足全部边际的非负整数三学期去向表）。"""


def _build(n1: int, n2: int, n3: int,
           a: Margin2, b: Margin2, z: Margin2):
    """构造变量与等式约束，返回 (vars, A_eq, rhs)。

    vars: [(i, j, k), ...] 仅保留上界 > 0 的变量。
    """
    vars_ = []
    ub = []
    for i in range(n1):
        for j in range(n2):
            aij = a.get((i, j), 0)
            if aij <= 0:
                continue
            for k in range(n3):
                bjk = b.get((j, k), 0)
                if bjk <= 0:
                    continue
                cap = min(aij, bjk)
                if (i, k) in z:
                    cap = min(cap, z[(i, k)])
                if cap > 0:
                    vars_.append((i, j, k))
                    ub.append(cap)
    nvar = len(vars_)
    index = {v: t for t, v in enumerate(vars_)}

    rows = []      # (系数行)
    rhs = []
    # sum_k w[i,j,k] == a[i,j]
    for i in range(n1):
        for j in range(n2):
            rows.append([(index[(i, j, k)], 1.0)
                         for k in range(n3) if (i, j, k) in index])
            rhs.append(float(a.get((i, j), 0)))
    # sum_i w[i,j,k] == b[j,k]
    for j in range(n2):
        for k in range(n3):
            rows.append([(index[(i, j, k)], 1.0)
                         for i in range(n1) if (i, j, k) in index])
            rhs.append(float(b.get((j, k), 0)))
    # sum_j w[i,j,k] == z[i,k]（仅核定的首末对）
    for (i, k), v in z.items():
        rows.append([(index[(i, j, k)], 1.0)
                     for j in range(n2) if (i, j, k) in index])
        rhs.append(float(v))

    ncon = len(rows)
    A = lil_matrix((ncon, nvar), dtype=float)
    for r, entries in enumerate(rows):
        for c, coef in entries:
            A[r, c] = coef
    return vars_, A.tocsr(), np.array(rhs, dtype=float)


def _solve(n1: int, n2: int, n3: int,
           a: Margin2, b: Margin2, z: Margin2,
           objective=None, maximize: bool = False,
           time_limit: float = 30.0):
    """求解。返回 (w, 最优值)；不可行时抛 Infeasible。

    objective: (i0, k0) 时优化 sum_j w[i0,j,k0]；为 None 时只求可行解。
    """
    vars_, A_eq, rhs = _build(n1, n2, n3, a, b, z)
    nvar = len(vars_)
    if nvar == 0:
        # 无任何可行路径变量：仅当全部边际都为零时可行（空表）
        if np.all(rhs == 0):
            return {}, 0 if objective is not None else None
        raise Infeasible("不存在满足全部生效汇总的三学期去向表")
    c = np.zeros(nvar)
    if objective is not None:
        i0, k0 = objective
        for t, (i, _j, k) in enumerate(vars_):
            if i == i0 and k == k0:
                c[t] = -1.0 if maximize else 1.0
    ub = np.empty(nvar)
    for t, (i, j, k) in enumerate(vars_):
        cap = min(a.get((i, j), 0), b.get((j, k), 0))
        if (i, k) in z:
            cap = min(cap, z[(i, k)])
        ub[t] = cap
    res = milp(
        c=c,
        constraints=LinearConstraint(A_eq, lb=rhs, ub=rhs),
        bounds=Bounds(np.zeros(nvar), ub),
        integrality=np.ones(nvar),
        options={"time_limit": time_limit},
    )
    if res.status == 2:
        raise Infeasible("不存在满足全部生效汇总的三学期去向表")
    if not res.success:
        raise RuntimeError(
            f"ILP 求解失败（status={res.status}）: {res.message}")
    w: Table3 = {}
    for t, v in enumerate(vars_):
        n = int(round(res.x[t]))
        if n > 0:
            w[v] = n
    value = None
    if objective is not None:
        i0, k0 = objective
        value = sum(n for (i, _j, k), n in w.items() if i == i0 and k == k0)
    return w, value


def find_table(n1, n2, n3, a, b, z) -> Table3:
    """任一满足全部边际的整数三学期去向表；不可行抛 Infeasible。"""
    w, _ = _solve(n1, n2, n3, a, b, z)
    return w


def bounds_for_pair(n1, n2, n3, a, b, z, pair):
    """首末专业对 pair=(i0,k0) 的人数下界与上界，各附一张达到界值的表。

    返回 (lo, w_lo, hi, w_hi)。
    """
    w_lo, lo = _solve(n1, n2, n3, a, b, z, objective=pair, maximize=False)
    w_hi, hi = _solve(n1, n2, n3, a, b, z, objective=pair, maximize=True)
    return lo, w_lo, hi, w_hi


def margins_of(w: Table3):
    """由三学期去向表汇总出三张二维表 a、b、z。"""
    a: Margin2 = {}
    b: Margin2 = {}
    z: Margin2 = {}
    for (i, j, k), n in w.items():
        a[(i, j)] = a.get((i, j), 0) + n
        b[(j, k)] = b.get((j, k), 0) + n
        z[(i, k)] = z.get((i, k), 0) + n
    return a, b, z


def check_witness(w: Table3, a: Margin2, b: Margin2, z: Margin2,
                  n1: int, n2: int, n3: int) -> list[str]:
    """校验 w 的各二维边际是否与生效汇总一致，返回问题列表（空为通过）。"""
    problems = []
    for (_i, _j, _k), n in w.items():
        if not isinstance(n, int) or n < 0:
            problems.append(f"路径人数 {n} 不是非负整数")
    wa, wb, wz = margins_of(w)
    for i in range(n1):
        for j in range(n2):
            want = a.get((i, j), 0)
            got = wa.get((i, j), 0)
            if want != got:
                problems.append(f"一二学期边际 ({i},{j}): 表内 {got} != 生效 {want}")
    for j in range(n2):
        for k in range(n3):
            want = b.get((j, k), 0)
            got = wb.get((j, k), 0)
            if want != got:
                problems.append(f"二三学期边际 ({j},{k}): 表内 {got} != 生效 {want}")
    for (i, k), want in z.items():
        got = wz.get((i, k), 0)
        if want != got:
            problems.append(f"首末核定数 ({i},{k}): 表内 {got} != 生效 {want}")
    return problems


def cheap_bounds(n2: int, a: Margin2, b: Margin2, col_a: dict,
                 i0: int, k0: int):
    """不计其它核定数时，z[i0,k0] 的简易上下界（用于报错提示）。

    lo = sum_j max(0, a[i0,j] + b[j,k0] - colA[j])
    hi = sum_j min(a[i0,j], b[j,k0])
    """
    lo = hi = 0
    for j in range(n2):
        aij = a.get((i0, j), 0)
        bjk = b.get((j, k0), 0)
        lo += max(0, aij + bjk - col_a.get(j, 0))
        hi += min(aij, bjk)
    return lo, hi
