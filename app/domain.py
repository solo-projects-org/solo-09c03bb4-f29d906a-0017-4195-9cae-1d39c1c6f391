"""领域层：把已生效数据（名字键）整理成求解器模型（下标键）并做一致性校验。"""
from __future__ import annotations

from dataclasses import dataclass, field

from .csvio import MAX_MAJORS


class DataError(ValueError):
    """已生效数据自身不一致（携带全部问题列表）。"""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


@dataclass
class Model:
    cohort_id: str
    majors1: list[str]
    majors2: list[str]
    majors3: list[str]
    a: dict = field(default_factory=dict)   # (i,j) -> int，下标键
    b: dict = field(default_factory=dict)   # (j,k) -> int
    z: dict = field(default_factory=dict)   # (i,k) -> int，仅核定对
    flows12_named: dict = field(default_factory=dict)
    flows23_named: dict = field(default_factory=dict)
    supp_named: dict = field(default_factory=dict)

    @property
    def n1(self):
        return len(self.majors1)

    @property
    def n2(self):
        return len(self.majors2)

    @property
    def n3(self):
        return len(self.majors3)

    @property
    def total_students(self) -> int:
        return sum(self.a.values())

    def idx1(self, name: str):
        return self.majors1.index(name) if name in self.majors1 else None

    def idx3(self, name: str):
        return self.majors3.index(name) if name in self.majors3 else None

    def col_a(self) -> dict:
        """第二学期各专业总人数（按 a 汇总）。"""
        out: dict[int, int] = {}
        for (i, j), n in self.a.items():
            out[j] = out.get(j, 0) + n
        return out

    def row_a(self) -> dict:
        out: dict[int, int] = {}
        for (i, j), n in self.a.items():
            out[i] = out.get(i, 0) + n
        return out

    def col_b(self) -> dict:
        out: dict[int, int] = {}
        for (j, k), n in self.b.items():
            out[k] = out.get(k, 0) + n
        return out


def build_model(cohort_id: str,
                flows12: dict[tuple[str, str], int],
                flows23: dict[tuple[str, str], int],
                supplements: dict[tuple[str, str], int]) -> Model:
    """校验并构造模型。任何不一致都抛 DataError（携带全部问题）。"""
    problems: list[str] = []
    majors1 = sorted({s for s, _ in flows12})
    majors2 = sorted({d for _, d in flows12} | {s for s, _ in flows23})
    majors3 = sorted({d for _, d in flows23})
    for label, ms in (("第一学期", majors1), ("第二学期", majors2),
                      ("第三学期", majors3)):
        if len(ms) > MAX_MAJORS:
            problems.append(f"{label}专业数 {len(ms)} 超过上限 {MAX_MAJORS}")
    if not majors1 or not majors2 or not majors3:
        problems.append("两份去向表不能为空")

    ix1 = {m: t for t, m in enumerate(majors1)}
    ix2 = {m: t for t, m in enumerate(majors2)}
    ix3 = {m: t for t, m in enumerate(majors3)}
    a = {(ix1[s], ix2[d]): n for (s, d), n in flows12.items()}
    b = {(ix2[s], ix3[d]): n for (s, d), n in flows23.items()}

    # 同一批学生：第二学期各专业人数，两张表必须一致
    for j, m in enumerate(majors2):
        na = sum(n for (i, jj), n in a.items() if jj == j)
        nb = sum(n for (jj, k), n in b.items() if jj == j)
        if na != nb:
            problems.append(
                f"第二学期专业「{m}」人数不一致：一至二学期表 {na} 人，"
                f"二至三学期表 {nb} 人")

    # 核定数必须落在已知专业内
    for (s, d), n in supplements.items():
        if s not in ix1:
            problems.append(f"核定数 {s} -> {d}：「{s}」不是第一学期专业")
        if d not in ix3:
            problems.append(f"核定数 {s} -> {d}：「{d}」不是第三学期专业")
        if n < 0:
            problems.append(f"核定数 {s} -> {d} 的人数 {n} 为负数")

    if problems:
        raise DataError(problems)

    z = {(ix1[s], ix3[d]): n for (s, d), n in supplements.items()}
    return Model(cohort_id=cohort_id, majors1=majors1, majors2=majors2,
                 majors3=majors3, a=a, b=b, z=z,
                 flows12_named=dict(flows12), flows23_named=dict(flows23),
                 supp_named=dict(supplements))


def named_entries(model: Model, w: dict) -> list[dict]:
    """把下标键的三学期表转成带专业名的条目列表（仅非零，按名称排序）。"""
    out = [
        {"sem1": model.majors1[i], "sem2": model.majors2[j],
         "sem3": model.majors3[k], "count": n}
        for (i, j, k), n in w.items() if n > 0
    ]
    out.sort(key=lambda e: (e["sem1"], e["sem2"], e["sem3"]))
    return out


def named_margins(model: Model, w: dict) -> dict:
    """三学期表的三张二维汇总（专业名键），用于展示“旧表可由它汇总得到”。"""
    from .solver import margins_of
    a, b, z = margins_of(w)
    conv = lambda d, xs, ys: {
        f"{xs[i]} -> {ys[j]}": n for (i, j), n in sorted(d.items())}
    return {
        "s1_s2": conv(a, model.majors1, model.majors2),
        "s2_s3": conv(b, model.majors2, model.majors3),
        "s1_s3": conv(z, model.majors1, model.majors3),
    }


# ---------- 目录换码 ----------

SEM_LABELS = {"sem1": "第一学期", "sem2": "第二学期", "sem3": "第三学期"}


def majors_of_flows(flows12: dict, flows23: dict) -> dict[str, list[str]]:
    """由两份去向表导出三学期专业清单（排序保证确定性）。"""
    return {
        "sem1": sorted({s for s, _ in flows12}),
        "sem2": sorted({d for _, d in flows12} | {s for s, _ in flows23}),
        "sem3": sorted({d for _, d in flows23}),
    }


def check_recode(mappings: dict[str, dict[str, str]],
                 majors: dict[str, list[str]]) -> list[str]:
    """校验换码对照，返回问题列表（空为通过）。

    - 旧码须在该学期现行专业清单内
    - 新码不能为空
    - 换完后同一学期不得重码（两码互换合法：成对列出即可）
    """
    problems: list[str] = []
    for sem, label in SEM_LABELS.items():
        mapping = mappings.get(sem, {})
        current = majors[sem]
        valid: dict[str, str] = {}
        for old, new in mapping.items():
            if old not in current:
                problems.append(
                    f"{label}现行专业清单中无旧码「{old}」对应的专业")
            if not new:
                problems.append(f"{label}旧码「{old}」的新码不能为空")
            else:
                valid[old] = new
        after = [valid.get(m, m) for m in current]
        seen: set[str] = set()
        dups: set[str] = set()
        for code in after:
            if code in seen:
                dups.add(code)
            seen.add(code)
        for d in sorted(dups):
            problems.append(
                f"换码后{label}专业码「{d}」重码（如为互换须把两码成对列出）")
    return problems


def rename_table(table: dict, from_map: dict, to_map: dict) -> dict:
    """按对照改键名；未列出的码沿用。"""
    return {(from_map.get(s, s), to_map.get(d, d)): n
            for (s, d), n in table.items()}


def apply_mappings(flows12: dict, flows23: dict, supplements: dict,
                   mappings: dict[str, dict[str, str]]):
    """按对照换码，返回改名后的 (flows12, flows23, supplements)。"""
    m1, m2, m3 = mappings["sem1"], mappings["sem2"], mappings["sem3"]
    return (rename_table(flows12, m1, m2),
            rename_table(flows23, m2, m3),
            rename_table(supplements, m1, m3))


def convert_previous_version(items: list[tuple[str, str, int]],
                             recode: dict):
    """把上一版补报条目按最近一次换码前的专业清单与对照转成现行码。

    items: [(from_major, to_major, count), ...]（旧码）
    recode: {"mappings": ..., "majors_before": ...}（换码档案）
    返回 (转换后条目, 问题列表)；旧码无对应专业时记入问题列表。
    """
    majors_before = recode["majors_before"]
    mappings = recode["mappings"]
    problems: list[str] = []
    converted: list[tuple[str, str, int]] = []
    for f, t, n in items:
        if f in majors_before["sem1"]:
            nf = mappings["sem1"].get(f, f)
        else:
            nf = f
            problems.append(
                f"旧码「{f}」在上一版（换码前）第一学期专业清单中无对应专业")
        if t in majors_before["sem3"]:
            nt = mappings["sem3"].get(t, t)
        else:
            nt = t
            problems.append(
                f"旧码「{t}」在上一版（换码前）第三学期专业清单中无对应专业")
        converted.append((nf, nt, n))
    return converted, problems
