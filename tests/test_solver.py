"""求解器单元测试：2x2x2 手算案例。

专业：S1={a,b}, S2={p,q}, S3={x,y}
一至二表 a: a->p=10, a->q=5, b->q=15（b->p=0 缺行按零）
二至三表 b: p->x=6, p->y=4, q->x=8, q->y=12
手算结果：
  z(a,x) ∈ [6, 11]；z(b,y) ∈ [7, 12]；z(b,x) ∈ [3, 8]
  补 z(a,x)=8 后：z(a,x)=[8,8]，且全表被唯一确定，z(b,y)=[9,9]
  z(a,x)=5 或 12 均不可行
"""
import pytest

from app import solver


@pytest.fixture
def case():
    a = {(0, 0): 10, (0, 1): 5, (1, 1): 15}
    b = {(0, 0): 6, (0, 1): 4, (1, 0): 8, (1, 1): 12}
    return dict(n1=2, n2=2, n3=2, a=a, b=b)


def test_bounds_without_supplements(case):
    lo, w_lo, hi, w_hi = solver.bounds_for_pair(
        **case, z={}, pair=(0, 0))
    assert (lo, hi) == (6, 11)
    for w, want in ((w_lo, lo), (w_hi, hi)):
        assert not solver.check_witness(w, case["a"], case["b"], {}, 2, 2, 2)
        assert sum(n for (i, _j, k), n in w.items() if i == 0 and k == 0) == want


def test_bounds_other_pairs(case):
    assert solver.bounds_for_pair(**case, z={}, pair=(1, 1))[::2] == (7, 12)
    assert solver.bounds_for_pair(**case, z={}, pair=(1, 0))[::2] == (3, 8)


def test_supplement_pins_table(case):
    z = {(0, 0): 8}
    lo, _, hi, _ = solver.bounds_for_pair(**case, z=z, pair=(0, 0))
    assert (lo, hi) == (8, 8)
    # 其余首末对也随之唯一确定
    assert solver.bounds_for_pair(**case, z=z, pair=(1, 1))[::2] == (9, 9)
    assert solver.bounds_for_pair(**case, z=z, pair=(0, 1))[::2] == (7, 7)


@pytest.mark.parametrize("bad", [5, 12])
def test_infeasible_supplement(case, bad):
    with pytest.raises(solver.Infeasible):
        solver.find_table(**case, z={(0, 0): bad})


def test_find_table_margins(case):
    w = solver.find_table(**case, z={})
    wa, wb, _ = solver.margins_of(w)
    for key, v in case["a"].items():
        assert wa.get(key, 0) == v
    for key, v in case["b"].items():
        assert wb.get(key, 0) == v
    assert all(isinstance(n, int) and n >= 0 for n in w.values())


def test_cheap_bounds(case):
    col_a = {0: 10, 1: 20}
    assert solver.cheap_bounds(2, case["a"], case["b"], col_a, 0, 0) == (6, 11)
