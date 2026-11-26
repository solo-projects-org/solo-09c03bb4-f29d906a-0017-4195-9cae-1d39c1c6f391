"""去向 CSV 解析：from_major,to_major,count 三列，缺行按零。"""
from __future__ import annotations

import csv
import io

MAX_MAJORS = 6


class CsvError(ValueError):
    pass


def parse_flows_csv(text: str) -> dict[tuple[str, str], int]:
    """解析一份去向 CSV，返回 {(from_major, to_major): count}。

    - 表头必须是 from_major,to_major,count（大小写不敏感，允许空白）
    - count 必须为非负整数
    - 同一 (from,to) 不得重复出现
    """
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not rows:
        raise CsvError("CSV 为空")
    header = [c.strip().lower() for c in rows[0]]
    if header != ["from_major", "to_major", "count"]:
        raise CsvError(
            "表头必须是 from_major,to_major,count，实际为: "
            + ",".join(rows[0]))
    out: dict[tuple[str, str], int] = {}
    for lineno, row in enumerate(rows[1:], start=2):
        if len(row) != 3:
            raise CsvError(f"第 {lineno} 行应有 3 列，实际 {len(row)} 列")
        src, dst, raw = (c.strip() for c in row)
        if not src or not dst:
            raise CsvError(f"第 {lineno} 行专业名不能为空")
        try:
            n = int(raw)
        except ValueError:
            raise CsvError(f"第 {lineno} 行人数 {raw!r} 不是整数") from None
        if n < 0:
            raise CsvError(f"第 {lineno} 行人数 {n} 为负数")
        key = (src, dst)
        if key in out:
            raise CsvError(f"第 {lineno} 行与前面重复: {src} -> {dst}")
        out[key] = n
    if not out:
        raise CsvError("CSV 没有数据行")
    return out


def majors_of(flows: dict[tuple[str, str], int]):
    """返回 (源专业集合, 目标专业集合)，按名称排序保证确定性。"""
    srcs = sorted({s for s, _ in flows})
    dsts = sorted({d for _, d in flows})
    return srcs, dsts
