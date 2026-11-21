"""k=5 匿名分组纯逻辑（无 IO，便于单元测试）。

攻击背景：行业对标只公开"样本数/失效数"。若某次发布的入选件与
历史发布的入选组合高度重合，几份公开数字相减即可推知单件是否失效。
防御：把冻结批次内每件按它在【历次发布 + 本次申请】中的入选组合
（即 0/1 向量）分组；除"从未入选"的全零组外，任一组不足 k 件
则本次申请拒发。已撤下发布仍是向量的一维——撤下只撤公开页，
不改历史核对口径。
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence

from .constants import K_ANONYMITY

# 向量末位（本次申请）的可读标签
CANDIDATE_LABEL = "本次申请"


def build_vectors(
    part_ids: Iterable[int],
    release_codes: Sequence[str],
    historical: dict[int, set[str]],
    candidate: set[int],
) -> dict[int, tuple[int, ...]]:
    """为每件生成入选向量。

    release_codes 为全部历次发布编码（含已撤下，顺序固定）；
    historical[p] 为该件曾入选的发布编码集合；
    candidate 为本次申请选件集合。
    """
    vectors: dict[int, tuple[int, ...]] = {}
    codes = list(release_codes)
    for pid in part_ids:
        hist = historical.get(pid, set())
        vec = tuple(1 if code in hist else 0 for code in codes) + (
            1 if pid in candidate else 0,
        )
        vectors[pid] = vec
    return vectors


def group_parts(
    vectors: dict[int, tuple[int, ...]],
) -> dict[tuple[int, ...], list[int]]:
    groups: dict[tuple[int, ...], list[int]] = {}
    for pid, vec in vectors.items():
        groups.setdefault(vec, []).append(pid)
    return groups


def evaluate_groups(
    vectors: dict[int, tuple[int, ...]],
    release_codes: Sequence[str],
    k: int = K_ANONYMITY,
) -> list[dict]:
    """返回违规组描述（不含部件身份，只描述组合与组大小）。

    全零向量 = 从未入选任何发布且未参与本次申请，豁免。
    其余向量组大小 < k 即违规。
    """
    groups = group_parts(vectors)
    violations: list[dict] = []
    zero_vec = tuple([0] * (len(release_codes) + 1))
    for vec, members in sorted(groups.items(), key=lambda kv: (kv[0],)):
        if vec == zero_vec:
            continue
        if len(members) < k:
            violations.append(
                {
                    "vector": list(vec),
                    "group_size": len(members),
                    "combination": describe_vector(vec, release_codes),
                }
            )
    return violations


def describe_vector(vec: Sequence[int], release_codes: Sequence[str]) -> str:
    """把向量翻译成不含部件身份的组合描述。"""
    labels = list(release_codes) + [CANDIDATE_LABEL]
    selected = [labels[i] for i, bit in enumerate(vec) if bit == 1]
    return " + ".join(selected) if selected else "（空组合）"


def rejection_message(violations: list[dict], k: int = K_ANONYMITY) -> str:
    lines = [
        f"拒发：入选组合匿名门槛为每组至少 {k} 件（从未入选任何发布与本次申请的组豁免）。",
        f"检测到 {len(violations)} 个不足 {k} 件的入选组合组：",
    ]
    for v in violations:
        lines.append(
            f"- 组合[{v['combination']}] 仅 {v['group_size']} 件，"
            f"差额 {k - v['group_size']} 件"
        )
    lines.append("说明：已撤下发布仍计入历史组合；请调整选件后以新的申请重试。")
    return "\n".join(lines)
