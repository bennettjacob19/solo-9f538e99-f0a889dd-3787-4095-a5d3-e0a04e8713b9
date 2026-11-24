"""成品整理（块级去重）。

规则（按演练题面）：

* 只读两份**尚未整理**的**等长**成品镜像（``image.bin``），不碰带卷；
* 同一块号上内容相同的块只留一份：第一份成品整体硬链为共享段（零拷贝、
  零额外占用），第二份成品的差异块写入新段；
* 整理期间额外占用的镜像数据恰好等于**差异块数 D**（临时段文件同样计入），
  因此余量刚够写一份镜像的差异块（额度 == D）时也能完成；
* 提交前任何失败都不改变两份成品，原链接依旧可完整下载；提交由
  :class:`~app.store.BlockStore` 原子完成，成功后两份成品按清单拼流，
  返回与整理前完全相同的字节。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable

from .storage import BLOCK_SIZE, sha256_hex
from .store import BlockRef, BlockStore, Manifest


class ConsolidateError(RuntimeError):
    """整理失败：成品状态不符、长度不一致或余量不足。"""


@dataclass
class ConsolidateResult:
    set_a: str
    set_b: str
    blocks: int                # 每份成品块数 N
    shared_blocks: int         # 同号同内容块数 S
    diff_blocks: int           # 差异块数 D
    saved_blocks: int          # 实际省下的镜像数据块数 = S
    extra_peak_blocks: int     # 整理期间额外占用峰值（块）= D
    events: list[str] = field(default_factory=list)


def _hash_blocks(path: str) -> list[str]:
    """顺序读取整镜像并逐块计算 SHA-256（只读成品）。"""
    hashes: list[str] = []
    with open(path, "rb") as fh:
        while True:
            data = fh.read(BLOCK_SIZE)
            if not data:
                break
            if len(data) != BLOCK_SIZE:
                raise ConsolidateError(
                    f"成品 {path} 长度不是 {BLOCK_SIZE} 的整数倍"
                )
            hashes.append(sha256_hex(data))
    return hashes


def consolidate(
    store: BlockStore,
    set_a: str,
    set_b: str,
    extra_quota: int,
    *,
    block_delay: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] | None = None,
) -> ConsolidateResult:
    """对两份等长未整理成品执行块级去重。

    extra_quota 是整理期间允许多占的 4 KiB 块数（临时文件中的镜像数据
    同样计入）。需要的额外占用恰好是差异块数 D，D <= extra_quota 即可成功。
    """
    events: list[str] = []

    def _log(message: str) -> None:
        events.append(message)
        if log is not None:
            log(message)

    if not isinstance(extra_quota, int) or extra_quota < 0:
        raise ValueError("整理期间额外占用上限必须是非负整数")
    if set_a == set_b:
        raise ConsolidateError("两份成品不能是同一套")

    path_a = store.image_bin_path(set_a)
    path_b = store.image_bin_path(set_b)
    for name, path in ((set_a, path_a), (set_b, path_b)):
        if store.manifest_for(name) is not None:
            raise ConsolidateError(f"成品 {name} 已整理过，不能重复整理")
        if not os.path.isfile(path):
            raise ConsolidateError(f"成品 {name} 不存在或不是未整理状态")

    size_a = os.path.getsize(path_a)
    size_b = os.path.getsize(path_b)
    if size_a != size_b:
        raise ConsolidateError(
            f"两份成品长度不一致（{size_a} 与 {size_b} 字节），无法整理"
        )
    if size_a == 0 or size_a % BLOCK_SIZE != 0:
        raise ConsolidateError("成品长度必须是 4 KiB 的整数倍且非空")
    n = size_a // BLOCK_SIZE
    _log(f"两份成品各 {n} 块（{size_a} 字节），开始逐块只读比对")

    # 第一步：只读两份成品，逐块算摘要、按块号比对。
    hashes_a = _hash_blocks(path_a)
    hashes_b = _hash_blocks(path_b)
    diff_index = [i for i in range(n) if hashes_a[i] != hashes_b[i]]
    d = len(diff_index)
    s = n - d
    _log(f"比对完成：同号同内容块 {s} 个，差异块 {d} 个")

    if d > extra_quota:
        raise ConsolidateError(
            f"余量不足：差异块 {d} 个，整理期间额外占用上限仅 {extra_quota} 块"
        )
    _log(f"整理期间额外占用峰值 {d} 块（临时段文件），不超过上限 {extra_quota} 块")

    # 第二步：把第二份成品的差异块写入临时段 —— 全程唯一多占的镜像数据。
    seg_shared = BlockStore.new_segment_id()
    seg_diff = BlockStore.new_segment_id() if d else None
    tmp_diff = store.tmp_path(seg_diff + ".part") if seg_diff else None
    try:
        if tmp_diff is not None:
            assert seg_diff is not None
            written = 0
            with open(path_b, "rb") as src, open(tmp_diff, "wb") as out:
                for i in diff_index:
                    src.seek(i * BLOCK_SIZE)
                    data = src.read(BLOCK_SIZE)
                    if len(data) != BLOCK_SIZE or sha256_hex(data) != hashes_b[i]:
                        raise ConsolidateError(
                            f"成品 {set_b} 在整理期间发生变化，已中止"
                        )
                    out.write(data)
                    written += 1
                    if written > extra_quota:
                        raise ConsolidateError("额外占用超过上限，已中止")
                    if block_delay:
                        sleep(block_delay)
                out.flush()
                os.fsync(out.fileno())
            _log(f"差异段临时文件就绪：{d} 个差异块（计入额度）")

        # 第三步：构建两份清单。共享块指向共享段内的同号偏移；
        # 差异块指向差异段内按顺序排列的偏移。
        entries_a = tuple(
            BlockRef(sha256=hashes_a[i], seg=seg_shared, offset=i)
            for i in range(n)
        )
        diff_position = {idx: k for k, idx in enumerate(diff_index)}
        entries_b = tuple(
            BlockRef(
                sha256=hashes_b[i],
                seg=seg_shared if hashes_b[i] == hashes_a[i] else seg_diff,  # type: ignore[arg-type]
                offset=i if hashes_b[i] == hashes_a[i] else diff_position[i],
            )
            for i in range(n)
        )
        manifest_a = Manifest(set_name=set_a, blocks=n, entries=entries_a)
        manifest_b = Manifest(set_name=set_b, blocks=n, entries=entries_b)

        # 第四步：原子提交（硬链共享段零拷贝，随后两份成品切换为清单形态）。
        store.publish_consolidation(
            set_a,
            set_b,
            seg_shared,
            manifest_a,
            manifest_b,
            seg_diff=seg_diff,
            tmp_diff=tmp_diff,
        )
        _log(f"整理完成：同号同内容块只留一份，实际省下 {s} 个镜像数据块")
    except BaseException:
        # 失败：清理临时文件；两份成品保持原样、原链接可继续完整下载。
        if tmp_diff is not None and os.path.exists(tmp_diff):
            os.unlink(tmp_diff)
        raise

    return ConsolidateResult(
        set_a=set_a,
        set_b=set_b,
        blocks=n,
        shared_blocks=s,
        diff_blocks=d,
        saved_blocks=s,
        extra_peak_blocks=d,
        events=events,
    )
