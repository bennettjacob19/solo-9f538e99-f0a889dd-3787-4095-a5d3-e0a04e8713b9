"""成品块仓库：段文件 + 块清单 + 引用计数。

成品有两种形态：

* **未整理**：``<output_root>/<set>/image.bin`` 整镜像文件（恢复演练的默认产出）；
* **已整理**：``<output_root>/<set>/image.json`` 块清单，逐块指向
  ``<output_root>/_store/segments/<seg>.bin`` 段文件中的 4 KiB 块。

整理（去重，见 :mod:`app.consolidate`）把两份等长成品切换为清单形态：
同一块号上内容相同的块只留一份 —— 第一份成品整体**硬链**为共享段
（零拷贝、零额外占用），第二份成品的差异块写入新段（这是整理期间
唯一多占的镜像数据，临时文件里的这部分数据同样计入额度）。

下载按清单从段文件拼流，并逐块核对 SHA-256。段文件按引用计数管理：
清单不再引用时即删除文件名；在途下载已打开的 fd 会钉住 inode，
占用空间随最后一个 fd 关闭由操作系统回收 —— 旧块在成品和在途下载
都用不到时释放。

重启后 :meth:`BlockStore.recover` 依据磁盘上的清单重建引用关系，
并清理中断提交留下的孤儿段与临时文件；提交顺序（先落段、再写清单、
最后删整镜像）保证崩溃后 recover 能收敛到一致状态，无需额外日志。
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from dataclasses import dataclass
from typing import Any

from .storage import BLOCK_SIZE, _SAFE_NAME

STORE_DIR_NAME = "_store"
SEGMENTS_DIR = "segments"
TMP_DIR = "tmp"
MANIFEST_NAME = "image.json"
IMAGE_BIN_NAME = "image.bin"
MANIFEST_FORMAT = 1
_SEG_NAME = re.compile(r"^seg-[0-9a-f]{16}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class StoreError(RuntimeError):
    """块仓库内部错误：清单损坏、段缺失或状态冲突。"""


@dataclass(frozen=True)
class BlockRef:
    """清单中一个镜像块的引用：内容摘要 + 段内位置。"""

    sha256: str
    seg: str
    offset: int  # 段内块号（从 0 开始）


@dataclass(frozen=True)
class Manifest:
    """已整理成品的块清单。"""

    set_name: str
    blocks: int
    entries: tuple[BlockRef, ...]

    @property
    def length(self) -> int:
        return self.blocks * BLOCK_SIZE

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": MANIFEST_FORMAT,
            "set": self.set_name,
            "blocks": self.blocks,
            "entries": [
                {"sha256": e.sha256, "seg": e.seg, "offset": e.offset}
                for e in self.entries
            ],
        }

    @classmethod
    def from_dict(cls, set_name: str, raw: object) -> "Manifest":
        if not isinstance(raw, dict):
            raise StoreError("清单顶层必须是对象")
        if raw.get("format") != MANIFEST_FORMAT:
            raise StoreError(f"清单格式版本不支持: {raw.get('format')!r}")
        if raw.get("set") != set_name:
            raise StoreError("清单 set 与目录名不一致")
        blocks = raw.get("blocks")
        entries = raw.get("entries")
        if not isinstance(blocks, int) or blocks <= 0:
            raise StoreError("清单 blocks 必须是正整数")
        if not isinstance(entries, list) or len(entries) != blocks:
            raise StoreError("清单条目数与块数不符")
        refs: list[BlockRef] = []
        for i, item in enumerate(entries):
            if not isinstance(item, dict):
                raise StoreError(f"清单第 {i} 条不是对象")
            digest = item.get("sha256")
            seg = item.get("seg")
            offset = item.get("offset")
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                raise StoreError(f"清单第 {i} 条 sha256 非法")
            if not isinstance(seg, str) or not _SEG_NAME.fullmatch(seg):
                raise StoreError(f"清单第 {i} 条段名非法")
            if not isinstance(offset, int) or offset < 0:
                raise StoreError(f"清单第 {i} 条偏移非法")
            refs.append(BlockRef(sha256=digest, seg=seg, offset=offset))
        return cls(set_name=set_name, blocks=blocks, entries=tuple(refs))


@dataclass
class OpenedProduct:
    """一次下载打开的成品快照：文件描述符已在锁内打开，钉住底层块。"""

    kind: str                       # "plain" | "consolidated"
    size: int                       # 成品字节数
    fileobj: Any = None             # plain：已打开的 image.bin 文件对象
    manifest: Manifest | None = None
    seg_fds: dict[str, int] | None = None  # consolidated：段名 -> fd


def _fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class BlockStore:
    """成品块仓库（单进程语义，全部状态变更走同一把锁）。"""

    def __init__(self, output_root: str) -> None:
        self._root = os.path.abspath(output_root)
        self._store_dir = os.path.join(self._root, STORE_DIR_NAME)
        self._segments_dir = os.path.join(self._store_dir, SEGMENTS_DIR)
        self._tmp_dir = os.path.join(self._store_dir, TMP_DIR)
        self._lock = threading.RLock()
        self._manifests: dict[str, Manifest] = {}
        self._refs: dict[str, int] = {}        # 段名 -> 引用它的清单条目数
        self._seg_blocks: dict[str, int] = {}  # 段名 -> 块数（统计用）
        os.makedirs(self._segments_dir, exist_ok=True)
        os.makedirs(self._tmp_dir, exist_ok=True)

    # ---- 路径 ----

    def image_bin_path(self, set_name: str) -> str:
        return os.path.join(self._root, set_name, IMAGE_BIN_NAME)

    def manifest_path(self, set_name: str) -> str:
        return os.path.join(self._root, set_name, MANIFEST_NAME)

    def segment_path(self, seg: str) -> str:
        return os.path.join(self._segments_dir, seg)

    def tmp_path(self, name: str) -> str:
        return os.path.join(self._tmp_dir, name)

    @staticmethod
    def new_segment_id() -> str:
        return f"seg-{uuid.uuid4().hex[:16]}"

    # ---- 启动恢复：重建引用、收敛中断的提交 ----

    def recover(self) -> None:
        with self._lock:
            # 1) 清掉整理/写清单留下的临时文件。
            for name in os.listdir(self._tmp_dir):
                path = os.path.join(self._tmp_dir, name)
                if os.path.isfile(path):
                    os.unlink(path)

            # 2) 扫描成品目录，加载有效清单（段齐全且在界内）。
            manifests: dict[str, Manifest] = {}
            for name in sorted(os.listdir(self._root)):
                if name == STORE_DIR_NAME or not _SAFE_NAME.fullmatch(name):
                    continue
                set_dir = os.path.join(self._root, name)
                if not os.path.isdir(set_dir):
                    continue
                mpath = os.path.join(set_dir, MANIFEST_NAME)
                if not os.path.isfile(mpath):
                    continue
                try:
                    with open(mpath, "r", encoding="utf-8") as fh:
                        manifest = Manifest.from_dict(name, json.load(fh))
                    for entry in manifest.entries:
                        seg_path = self.segment_path(entry.seg)
                        if not os.path.isfile(seg_path):
                            raise StoreError(f"段文件缺失: {entry.seg}")
                        if (entry.offset + 1) * BLOCK_SIZE > os.path.getsize(seg_path):
                            raise StoreError(f"段内偏移越界: {entry.seg}")
                except (OSError, ValueError, StoreError):
                    # 清单损坏：回滚为未整理（image.bin 仍在）或丢弃坏清单。
                    os.unlink(mpath)
                    continue
                manifests[name] = manifest

            # 3) 清单有效但整镜像残留：提交在删除整镜像前中断，补删。
            #    （对第一份成品而言 image.bin 与共享段是同一 inode 的两个
            #    名字，删除名字不丢数据；对第二份则是释放旧整镜像。）
            for name in manifests:
                bin_path = os.path.join(self._root, name, IMAGE_BIN_NAME)
                if os.path.isfile(bin_path):
                    os.unlink(bin_path)

            # 4) 依据清单重建引用计数；无引用的段是中断提交的孤儿，删除。
            refs: dict[str, int] = {}
            for manifest in manifests.values():
                for entry in manifest.entries:
                    refs[entry.seg] = refs.get(entry.seg, 0) + 1
            seg_blocks: dict[str, int] = {}
            for seg in os.listdir(self._segments_dir):
                path = os.path.join(self._segments_dir, seg)
                if not os.path.isfile(path):
                    continue
                if seg not in refs:
                    os.unlink(path)
                else:
                    seg_blocks[seg] = os.path.getsize(path) // BLOCK_SIZE

            self._manifests = manifests
            self._refs = refs
            self._seg_blocks = seg_blocks

    # ---- 查询 ----

    def manifest_for(self, set_name: str) -> Manifest | None:
        with self._lock:
            return self._manifests.get(set_name)

    def product(self, set_name: str) -> dict[str, Any] | None:
        """成品现状：None 无成品；否则 state=plain/consolidated + size。"""
        with self._lock:
            manifest = self._manifests.get(set_name)
            if manifest is not None:
                return {"state": "consolidated", "size": manifest.length}
            path = self.image_bin_path(set_name)
            if os.path.isfile(path):
                return {"state": "plain", "size": os.path.getsize(path)}
            return None

    def list_products(self) -> list[dict[str, Any]]:
        """列出全部成品（含已整理），供整理表单与页面展示。"""
        with self._lock:
            products = []
            for name in sorted(os.listdir(self._root)):
                if name == STORE_DIR_NAME or not _SAFE_NAME.fullmatch(name):
                    continue
                if not os.path.isdir(os.path.join(self._root, name)):
                    continue
                info = self.product(name)
                if info is not None:
                    products.append({"name": name, **info})
            return products

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "segments": len(self._seg_blocks),
                "segment_blocks": sum(self._seg_blocks.values()),
                "refs": sum(self._refs.values()),
                "consolidated_sets": len(self._manifests),
            }

    # ---- 整理提交 ----

    def publish_consolidation(
        self,
        set_a: str,
        set_b: str,
        seg_shared: str,
        manifest_a: Manifest,
        manifest_b: Manifest,
        seg_diff: str | None = None,
        tmp_diff: str | None = None,
    ) -> None:
        """把两份未整理成品原子切换为清单形态。

        提交顺序保证崩溃后 recover() 能收敛：先落差异段、再硬链共享段、
        再写两份清单（临时文件 + replace），最后才删除整镜像。
        """
        with self._lock:
            for name in (set_a, set_b):
                if self._manifests.get(name) is not None:
                    raise StoreError(f"成品 {name} 已是清单形态，不能重复整理")
                if not os.path.isfile(self.image_bin_path(name)):
                    raise StoreError(f"成品 {name} 的整镜像不存在")

            # 1) 差异段就位（临时文件 -> 段目录）。
            if seg_diff is not None and tmp_diff is not None:
                os.replace(tmp_diff, self.segment_path(seg_diff))
            # 2) 共享段：硬链第一份成品的整镜像（零拷贝）；
            #    个别文件系统不支持硬链时退化为改名（瞬间完成）。
            try:
                os.link(self.image_bin_path(set_a), self.segment_path(seg_shared))
            except OSError:
                os.rename(self.image_bin_path(set_a), self.segment_path(seg_shared))
            # 3) 写两份清单（临时文件 + replace）。
            self._write_manifest(set_a, manifest_a)
            self._write_manifest(set_b, manifest_b)
            # 4) 删除整镜像（第一份可能已被改名掉）。
            for name in (set_a, set_b):
                bin_path = self.image_bin_path(name)
                if os.path.isfile(bin_path):
                    os.unlink(bin_path)

            # 5) 更新内存状态。
            self._manifests[set_a] = manifest_a
            self._manifests[set_b] = manifest_b
            for manifest in (manifest_a, manifest_b):
                for entry in manifest.entries:
                    self._refs[entry.seg] = self._refs.get(entry.seg, 0) + 1
            self._seg_blocks[seg_shared] = manifest_a.blocks
            if seg_diff is not None:
                self._seg_blocks[seg_diff] = sum(
                    1 for e in manifest_b.entries if e.seg == seg_diff
                )
            _fsync_dir(self._segments_dir)
            _fsync_dir(os.path.join(self._root, set_a))
            _fsync_dir(os.path.join(self._root, set_b))

    def _write_manifest(self, set_name: str, manifest: Manifest) -> None:
        tmp = self.tmp_path(f"{set_name}.manifest.part")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(manifest.to_dict(), fh, indent=1)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.manifest_path(set_name))

    # ---- 重新恢复时释放旧成品 ----

    def release_set_image(self, set_name: str) -> None:
        """发布新整镜像前调用：若旧成品是清单形态，释放其段引用。

        引用归零的段立即删除文件名；在途下载持有的 fd 会钉住 inode，
        空间随 fd 关闭释放。
        """
        with self._lock:
            manifest = self._manifests.pop(set_name, None)
            if manifest is None:
                return
            mpath = self.manifest_path(set_name)
            if os.path.isfile(mpath):
                os.unlink(mpath)
            dead: list[str] = []
            for entry in manifest.entries:
                self._refs[entry.seg] -= 1
                if self._refs[entry.seg] == 0:
                    dead.append(entry.seg)
            for seg in dead:
                del self._refs[seg]
                self._seg_blocks.pop(seg, None)
                path = self.segment_path(seg)
                if os.path.isfile(path):
                    os.unlink(path)
            _fsync_dir(self._segments_dir)

    # ---- 下载 ----

    def open_product(self, set_name: str) -> OpenedProduct | None:
        """打开成品快照供下载：在锁内打开全部文件描述符。

        之后即使整理/重新恢复替换或删除了底层文件，已打开的 fd 仍指向
        当时的成品内容（POSIX inode 语义），在途下载可读完当时的数据。
        """
        with self._lock:
            manifest = self._manifests.get(set_name)
            if manifest is None:
                path = self.image_bin_path(set_name)
                if not os.path.isfile(path):
                    return None
                return OpenedProduct(
                    kind="plain",
                    size=os.path.getsize(path),
                    fileobj=open(path, "rb"),  # noqa: SIM115 - 由响应方关闭
                )
            fds: dict[str, int] = {}
            try:
                for entry in manifest.entries:
                    if entry.seg not in fds:
                        fds[entry.seg] = os.open(
                            self.segment_path(entry.seg), os.O_RDONLY
                        )
            except OSError as exc:
                for fd in fds.values():
                    os.close(fd)
                raise StoreError(f"段文件打开失败: {exc}") from exc
            return OpenedProduct(
                kind="consolidated",
                size=manifest.length,
                manifest=manifest,
                seg_fds=fds,
            )
