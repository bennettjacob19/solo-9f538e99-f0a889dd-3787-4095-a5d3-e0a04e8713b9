import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers import (  # noqa: E402  先导入：helpers 会把 .pylibs 注入 sys.path
    PAIR_LAYOUT,
    PAIR_SHARED_BLOCKS,
    build_set,
    pair_content_fn,
)

from app.consolidate import ConsolidateError, consolidate  # noqa: E402
from app.storage import BLOCK_SIZE  # noqa: E402
from app.store import SEGMENTS_DIR, STORE_DIR_NAME, BlockStore  # noqa: E402
from app.web import create_app  # noqa: E402

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
N = 12
D = N - PAIR_SHARED_BLOCKS  # 差异块 4
S = PAIR_SHARED_BLOCKS      # 相同块 8


def pair_blocks(payload: str) -> list[bytes]:
    fn = pair_content_fn(payload)
    return [fn(i) for i in range(N)]


def write_plain_image(output_root: str, name: str, blocks: list[bytes]) -> str:
    set_dir = os.path.join(output_root, name)
    os.makedirs(set_dir, exist_ok=True)
    path = os.path.join(set_dir, "image.bin")
    with open(path, "wb") as fh:
        for data in blocks:
            assert len(data) == BLOCK_SIZE
            fh.write(data)
    return path


def read_product(store: BlockStore, name: str) -> bytes:
    opened = store.open_product(name)
    assert opened is not None
    if opened.kind == "plain":
        with opened.fileobj:
            return opened.fileobj.read()
    assert opened.manifest is not None and opened.seg_fds is not None
    try:
        return b"".join(
            os.pread(
                opened.seg_fds[e.seg], BLOCK_SIZE, e.offset * BLOCK_SIZE
            )
            for e in opened.manifest.entries
        )
    finally:
        for fd in opened.seg_fds.values():
            os.close(fd)


def _fresh_store(root: str) -> BlockStore:
    """模拟进程重启：新实例必须先 recover() 再使用。"""
    store = BlockStore(root)
    store.recover()
    return store


class ConsolidateEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.output = os.path.join(self.tmp, "output")
        os.makedirs(self.output, exist_ok=True)
        self.blocks_a = pair_blocks("a")
        self.blocks_b = pair_blocks("b")
        write_plain_image(self.output, "pair-a", self.blocks_a)
        write_plain_image(self.output, "pair-b", self.blocks_b)
        self.store = BlockStore(self.output)
        self.store.recover()

    def test_exact_quota_succeeds_and_dedups(self):
        result = consolidate(self.store, "pair-a", "pair-b", extra_quota=D)
        self.assertEqual(result.blocks, N)
        self.assertEqual(result.shared_blocks, S)
        self.assertEqual(result.diff_blocks, D)
        self.assertEqual(result.saved_blocks, S)
        self.assertEqual(result.extra_peak_blocks, D)

        # 整镜像切换为清单形态
        for name in ("pair-a", "pair-b"):
            self.assertFalse(
                os.path.isfile(os.path.join(self.output, name, "image.bin"))
            )
            self.assertTrue(
                os.path.isfile(os.path.join(self.output, name, "image.json"))
            )

        # 两个段：共享段 12 块 + 差异段 4 块；临时目录清空
        seg_dir = os.path.join(self.output, STORE_DIR_NAME, SEGMENTS_DIR)
        seg_files = sorted(os.listdir(seg_dir))
        self.assertEqual(len(seg_files), 2)
        sizes = sorted(os.path.getsize(os.path.join(seg_dir, f)) for f in seg_files)
        self.assertEqual(sizes, [D * BLOCK_SIZE, N * BLOCK_SIZE])
        self.assertEqual(os.listdir(os.path.join(self.output, STORE_DIR_NAME, "tmp")), [])

        # 引用计数：两份清单各 12 条
        stats = self.store.stats()
        self.assertEqual(stats["segments"], 2)
        self.assertEqual(stats["segment_blocks"], N + D)
        self.assertEqual(stats["refs"], 2 * N)
        self.assertEqual(stats["consolidated_sets"], 2)

        # 原链接语义：拼流结果与整理前字节完全一致
        self.assertEqual(read_product(self.store, "pair-a"), b"".join(self.blocks_a))
        self.assertEqual(read_product(self.store, "pair-b"), b"".join(self.blocks_b))

    def test_quota_shortfall_fails_without_touching_products(self):
        with self.assertRaisesRegex(ConsolidateError, "余量不足"):
            consolidate(self.store, "pair-a", "pair-b", extra_quota=D - 1)

        # 两份成品原样保留、可完整下载
        self.assertEqual(read_product(self.store, "pair-a"), b"".join(self.blocks_a))
        self.assertEqual(read_product(self.store, "pair-b"), b"".join(self.blocks_b))
        for name in ("pair-a", "pair-b"):
            self.assertTrue(
                os.path.isfile(os.path.join(self.output, name, "image.bin"))
            )
        # 没有任何段或临时文件残留
        seg_dir = os.path.join(self.output, STORE_DIR_NAME, SEGMENTS_DIR)
        self.assertEqual(os.listdir(seg_dir), [])
        self.assertEqual(
            os.listdir(os.path.join(self.output, STORE_DIR_NAME, "tmp")), []
        )
        # 新实例“重启”后状态同样干净
        fresh = BlockStore(self.output)
        fresh.recover()
        self.assertEqual(fresh.product("pair-a")["state"], "plain")
        self.assertEqual(fresh.product("pair-b")["state"], "plain")

    def test_identical_images_need_zero_extra_quota(self):
        write_plain_image(self.output, "dup-x", self.blocks_a)
        write_plain_image(self.output, "dup-y", self.blocks_a)
        result = consolidate(self.store, "dup-x", "dup-y", extra_quota=0)
        self.assertEqual(result.diff_blocks, 0)
        self.assertEqual(result.saved_blocks, N)
        seg_dir = os.path.join(self.output, STORE_DIR_NAME, SEGMENTS_DIR)
        # 无差异段：只剩一个 12 块的共享段
        self.assertEqual(len(os.listdir(seg_dir)), 1)
        only = os.listdir(seg_dir)[0]
        self.assertEqual(os.path.getsize(os.path.join(seg_dir, only)), N * BLOCK_SIZE)
        self.assertEqual(read_product(self.store, "dup-x"), b"".join(self.blocks_a))
        self.assertEqual(read_product(self.store, "dup-y"), b"".join(self.blocks_a))

    def test_rejects_bad_inputs_and_states(self):
        # 长度不一致
        write_plain_image(self.output, "short", self.blocks_a[:6])
        with self.assertRaisesRegex(ConsolidateError, "长度不一致"):
            consolidate(self.store, "pair-a", "short", extra_quota=100)
        # 同一套
        with self.assertRaises(ConsolidateError):
            consolidate(self.store, "pair-a", "pair-a", extra_quota=100)
        # 不存在的成品
        with self.assertRaises(ConsolidateError):
            consolidate(self.store, "pair-a", "ghost", extra_quota=100)
        # 已整理过的成品不能重复整理
        consolidate(self.store, "pair-a", "pair-b", extra_quota=D)
        write_plain_image(self.output, "pair-c", self.blocks_a)
        with self.assertRaisesRegex(ConsolidateError, "已整理"):
            consolidate(self.store, "pair-a", "pair-c", extra_quota=100)

    def test_rerestore_releases_segments_and_keeps_other_bytes(self):
        consolidate(self.store, "pair-a", "pair-b", extra_quota=D)
        original_b = b"".join(self.blocks_b)

        # 模拟重新恢复 pair-a：先释放旧清单引用，再发布新整镜像
        self.store.release_set_image("pair-a")
        new_blocks = [bytes([i ^ 0x5A]) * BLOCK_SIZE for i in range(N)]
        write_plain_image(self.output, "pair-a", new_blocks)

        self.assertEqual(self.store.product("pair-a")["state"], "plain")
        self.assertEqual(read_product(self.store, "pair-a"), b"".join(new_blocks))
        # 另一份仍返回原字节
        self.assertEqual(read_product(self.store, "pair-b"), original_b)

        # pair-b 仍引用两个段，段文件不能释放
        seg_dir = os.path.join(self.output, STORE_DIR_NAME, SEGMENTS_DIR)
        self.assertEqual(len(os.listdir(seg_dir)), 2)

        # pair-b 也重新恢复后，旧块无人引用，段全部释放
        self.store.release_set_image("pair-b")
        self.assertEqual(os.listdir(seg_dir), [])
        stats = self.store.stats()
        self.assertEqual(stats["segments"], 0)
        self.assertEqual(stats["refs"], 0)
        self.assertEqual(stats["consolidated_sets"], 0)

    def test_inflight_download_reads_snapshot_after_release(self):
        consolidate(self.store, "pair-a", "pair-b", extra_quota=D)
        original_b = b"".join(self.blocks_b)

        # 已开始的下载持有段 fd：释放清单、段文件被 unlink 后仍可读完
        opened = self.store.open_product("pair-b")
        self.assertEqual(opened.kind, "consolidated")
        seg_dir = os.path.join(self.output, STORE_DIR_NAME, SEGMENTS_DIR)
        segs = {e.seg for e in opened.manifest.entries}
        seg_shared = self.store.manifest_for("pair-a").entries[0].seg
        (seg_diff,) = segs - {seg_shared}
        self.store.release_set_image("pair-b")
        # 差异段引用归零：文件名立即释放；共享段因 pair-a 仍引用而保留
        self.assertFalse(os.path.isfile(os.path.join(seg_dir, seg_diff)))
        self.assertTrue(os.path.isfile(os.path.join(seg_dir, seg_shared)))
        assert opened.manifest is not None and opened.seg_fds is not None
        try:
            data = b"".join(
                os.pread(opened.seg_fds[e.seg], BLOCK_SIZE, e.offset * BLOCK_SIZE)
                for e in opened.manifest.entries
            )
        finally:
            for fd in opened.seg_fds.values():
                os.close(fd)
        self.assertEqual(data, original_b)  # 在途下载读完当时的成品

        # 未整理成品同理：替换整镜像不影响已打开的文件对象
        write_plain_image(self.output, "plain-c", self.blocks_a)
        opened_plain = self.store.open_product("plain-c")
        self.assertEqual(opened_plain.kind, "plain")
        new_path = self.store.image_bin_path("plain-c")
        tmp_path = new_path + ".new"
        with open(tmp_path, "wb") as fh:
            fh.write(bytes([0x77]) * (N * BLOCK_SIZE))
        os.replace(tmp_path, new_path)
        with opened_plain.fileobj:
            self.assertEqual(opened_plain.fileobj.read(), b"".join(self.blocks_a))

    def test_restart_rebuilds_store_and_repairs_interrupted_commit(self):
        consolidate(self.store, "pair-a", "pair-b", extra_quota=D)
        original_a = b"".join(self.blocks_a)
        original_b = b"".join(self.blocks_b)

        # 重启：新实例从磁盘清单重建
        restarted = BlockStore(self.output)
        restarted.recover()
        self.assertEqual(restarted.product("pair-a")["state"], "consolidated")
        self.assertEqual(read_product(restarted, "pair-a"), original_a)
        self.assertEqual(read_product(restarted, "pair-b"), original_b)
        self.assertEqual(restarted.stats()["refs"], 2 * N)

        # 中断场景 1：段已就位但清单未写（孤儿段），重启后清理、成品保持未整理
        plain_root = tempfile.mkdtemp()
        write_plain_image(plain_root, "x", self.blocks_a)
        write_plain_image(plain_root, "y", self.blocks_b)
        orphan_store = BlockStore(plain_root)
        orphan_store.recover()
        seg_id = BlockStore.new_segment_id()
        os.link(
            os.path.join(plain_root, "x", "image.bin"),
            os.path.join(plain_root, STORE_DIR_NAME, SEGMENTS_DIR, seg_id),
        )
        with open(
            os.path.join(plain_root, STORE_DIR_NAME, SEGMENTS_DIR,
                         BlockStore.new_segment_id()),
            "wb",
        ) as fh:
            fh.write(b"x" * BLOCK_SIZE)
        BlockStore(plain_root).recover()
        self.assertEqual(
            os.listdir(os.path.join(plain_root, STORE_DIR_NAME, SEGMENTS_DIR)), []
        )
        self.assertEqual(
            _fresh_store(plain_root).product("x")["state"], "plain"
        )

        # 中断场景 2：清单已写、整镜像残留，重启后补删整镜像、清单仍可用
        leftover = BlockStore(self.output).image_bin_path("pair-a")
        with open(leftover, "wb") as fh:
            fh.write(b"stale")
        self.assertEqual(
            read_product(_fresh_store(self.output), "pair-a"), original_a
        )


class ConsolidateWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tapes = os.path.join(self.tmp, "tapes")
        self.output = os.path.join(self.tmp, "output")
        build_set(self.tapes, "pair-a", PAIR_LAYOUT, content_fn=pair_content_fn("a"))
        build_set(self.tapes, "pair-b", PAIR_LAYOUT, content_fn=pair_content_fn("b"))
        self.app = create_app(
            tape_root=self.tapes,
            output_root=self.output,
            read_delay=0.0,
            consolidate_delay=0.0,
        )
        self.client = self.app.test_client()
        self._restore_both()
        self.original_a = self.client.get("/download/pair-a").data
        self.original_b = self.client.get("/download/pair-b").data
        # 前 8 块相同、后 4 块不同
        self.assertEqual(self.original_a[: S * BLOCK_SIZE],
                         self.original_b[: S * BLOCK_SIZE])
        self.assertNotEqual(self.original_a[S * BLOCK_SIZE:],
                            self.original_b[S * BLOCK_SIZE:])

    def _wait_done(self, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = self.client.get("/status").get_json()
            if not data["running"]:
                return data
            time.sleep(0.02)
        self.fail("任务超时未完成")

    def _restore_both(self):
        for name in ("pair-a", "pair-b"):
            resp = self.client.post(
                "/recover",
                data={"set_name": name, "read_quota": N, "staging_quota": 0},
            )
            self.assertEqual(resp.status_code, 302)
            self._wait_done()

    def _consolidate(self, quota=D):
        resp = self.client.post(
            "/consolidate",
            data={"set_a": "pair-a", "set_b": "pair-b", "extra_quota": quota},
        )
        return resp

    def test_consolidate_flow_shows_saved_blocks(self):
        resp = self._consolidate(quota=D)
        self.assertEqual(resp.status_code, 302)
        status = self._wait_done()
        self.assertEqual(status["kind"], "consolidate")
        self.assertTrue(status["result"]["success"])
        self.assertEqual(status["result"]["saved_blocks"], S)
        self.assertEqual(status["result"]["shared_blocks"], S)
        self.assertEqual(status["result"]["diff_blocks"], D)
        self.assertEqual(status["result"]["extra_peak_blocks"], D)

        # 原链接返回相同字节
        self.assertEqual(self.client.get("/download/pair-a").data, self.original_a)
        self.assertEqual(self.client.get("/download/pair-b").data, self.original_b)

        # 页面展示已整理状态与实际省下的块数
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn("已整理", body)
        self.assertIn("实际省下的镜像数据块", body)
        self.assertIn(f">{S}<", body)

    def test_consolidate_failure_keeps_both_products(self):
        status = self._wait_done_for(self._consolidate(quota=D - 1))
        self.assertFalse(status["result"]["success"])
        self.assertIn("余量不足", status["error"])
        # 两份成品仍可从原链接完整下载，且仍是未整理整镜像
        self.assertEqual(self.client.get("/download/pair-a").data, self.original_a)
        self.assertEqual(self.client.get("/download/pair-b").data, self.original_b)
        for name in ("pair-a", "pair-b"):
            self.assertTrue(
                os.path.isfile(os.path.join(self.output, name, "image.bin"))
            )
            self.assertFalse(
                os.path.isfile(os.path.join(self.output, name, "image.json"))
            )

    def _wait_done_for(self, resp):
        self.assertEqual(resp.status_code, 302)
        return self._wait_done()

    def test_downloads_work_while_consolidation_running(self):
        slow = create_app(
            tape_root=self.tapes,
            output_root=self.output,
            read_delay=0.0,
            consolidate_delay=0.2,
        )
        client = slow.test_client()
        resp = client.post(
            "/consolidate",
            data={"set_a": "pair-a", "set_b": "pair-b", "extra_quota": D},
        )
        self.assertEqual(resp.status_code, 302)
        running_seen = False
        for _ in range(50):
            st = client.get("/status").get_json()
            if st["running"]:
                running_seen = True
                # 整理期间两份成品都能从原链接完整下载
                self.assertEqual(client.get("/download/pair-a").data, self.original_a)
                self.assertEqual(client.get("/download/pair-b").data, self.original_b)
                break
            time.sleep(0.02)
        self.assertTrue(running_seen, "未观察到运行中的整理任务")
        status = self._wait_done_with(client)
        self.assertTrue(status["result"]["success"])
        # 成功后仍是相同字节
        self.assertEqual(client.get("/download/pair-a").data, self.original_a)
        self.assertEqual(client.get("/download/pair-b").data, self.original_b)

    def _wait_done_with(self, client, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = client.get("/status").get_json()
            if not data["running"]:
                return data
            time.sleep(0.02)
        self.fail("任务超时未完成")

    def test_validation_errors_and_concurrent_rejection(self):
        # 同一份成品选两次
        resp = self.client.post(
            "/consolidate",
            data={"set_a": "pair-a", "set_b": "pair-a", "extra_quota": 10},
        )
        self.assertEqual(resp.status_code, 400)
        # 不存在 / 非法额度
        resp = self.client.post(
            "/consolidate",
            data={"set_a": "pair-a", "set_b": "ghost", "extra_quota": 10},
        )
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post(
            "/consolidate",
            data={"set_a": "pair-a", "set_b": "pair-b", "extra_quota": -1},
        )
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post(
            "/consolidate",
            data={"set_a": "pair-a", "set_b": "pair-b", "extra_quota": "x"},
        )
        self.assertEqual(resp.status_code, 400)
        # 长度不一致
        short_dir = os.path.join(self.output, "shorty")
        os.makedirs(short_dir, exist_ok=True)
        with open(os.path.join(short_dir, "image.bin"), "wb") as fh:
            fh.write(b"q" * BLOCK_SIZE)
        resp = self.client.post(
            "/consolidate",
            data={"set_a": "pair-a", "set_b": "shorty", "extra_quota": 10},
        )
        self.assertEqual(resp.status_code, 400)

        # 并发拒绝：慢应用使用独立输出目录（其块仓库与主应用互不感知）
        slow_output = os.path.join(self.tmp, "slow-output")
        for name in ("pair-a", "pair-b"):
            os.makedirs(os.path.join(slow_output, name), exist_ok=True)
            with open(os.path.join(self.output, name, "image.bin"), "rb") as src, \
                    open(os.path.join(slow_output, name, "image.bin"), "wb") as dst:
                dst.write(src.read())
        slow = create_app(
            tape_root=self.tapes,
            output_root=slow_output,
            read_delay=0.0,
            consolidate_delay=0.2,
        )
        client = slow.test_client()
        client.post(
            "/consolidate",
            data={"set_a": "pair-a", "set_b": "pair-b", "extra_quota": D},
        )
        conflict = client.post(
            "/consolidate",
            data={"set_a": "pair-a", "set_b": "pair-b", "extra_quota": D},
        )
        self.assertEqual(conflict.status_code, 409)
        # 恢复任务也被拒绝（同一把任务锁）
        conflict_recover = client.post(
            "/recover",
            data={"set_name": "pair-a", "read_quota": N, "staging_quota": 0},
        )
        self.assertEqual(conflict_recover.status_code, 409)
        self._wait_done_with(client)

        # 整理成功后不能再次选择已整理成品
        resp = self.client.post(
            "/consolidate",
            data={"set_a": "pair-a", "set_b": "pair-b", "extra_quota": D},
        )
        self.assertEqual(resp.status_code, 302)
        self._wait_done()
        resp = self.client.post(
            "/consolidate",
            data={"set_a": "pair-a", "set_b": "pair-b", "extra_quota": D},
        )
        self.assertEqual(resp.status_code, 400)

    def test_rerestore_publishes_new_bytes_other_link_unchanged(self):
        self._wait_done_for(self._consolidate(quota=D))
        seg_dir = os.path.join(self.output, STORE_DIR_NAME, SEGMENTS_DIR)
        self.assertEqual(len(os.listdir(seg_dir)), 2)

        # 重新恢复 pair-a：带卷内容换成全新版本
        v2 = [bytes([i ^ 0x3C]) * BLOCK_SIZE for i in range(N)]
        build_set(
            self.tapes,
            "pair-a",
            PAIR_LAYOUT,
            content_fn=lambda i: v2[i],
        )
        resp = self.client.post(
            "/recover",
            data={"set_name": "pair-a", "read_quota": N, "staging_quota": 0},
        )
        self.assertEqual(resp.status_code, 302)
        status = self._wait_done()
        self.assertTrue(status["result"]["success"])

        # 新下载拿到新成品；另一份仍返回原字节
        self.assertEqual(self.client.get("/download/pair-a").data, b"".join(v2))
        self.assertEqual(self.client.get("/download/pair-b").data, self.original_b)
        self.assertEqual(
            self.app.test_client().get("/download/pair-a").data, b"".join(v2)
        )

        # 另一套也重新恢复后，旧段全部释放
        build_set(
            self.tapes,
            "pair-b",
            PAIR_LAYOUT,
            content_fn=lambda i: v2[i],
        )
        self.client.post(
            "/recover",
            data={"set_name": "pair-b", "read_quota": N, "staging_quota": 0},
        )
        self._wait_done()
        self.assertEqual(os.listdir(seg_dir), [])

    def test_restart_with_new_app_serves_consolidated_bytes(self):
        self._wait_done_for(self._consolidate(quota=D))
        restarted = create_app(
            tape_root=self.tapes,
            output_root=self.output,
            read_delay=0.0,
            consolidate_delay=0.0,
        )
        client = restarted.test_client()
        self.assertEqual(client.get("/download/pair-a").data, self.original_a)
        self.assertEqual(client.get("/download/pair-b").data, self.original_b)
        body = client.get("/").get_data(as_text=True)
        self.assertIn("已整理", body)

    def test_tampered_segment_fails_download(self):
        self._wait_done_for(self._consolidate(quota=D))
        seg_dir = os.path.join(self.output, STORE_DIR_NAME, SEGMENTS_DIR)
        seg = os.path.join(seg_dir, os.listdir(seg_dir)[0])
        with open(seg, "r+b") as fh:
            fh.seek(17)
            byte = fh.read(1)
            fh.seek(17)
            fh.write(bytes([byte[0] ^ 0xFF]))
        # 拼流时逐块核对 SHA-256：取响应内容即触发校验并报错
        with self.assertRaises(Exception):
            self.client.get("/download/pair-b").data

    def test_sample_pair_generated_by_script(self):
        gen_root = os.path.join(self.tmp, "generated")
        subprocess.run(
            [
                sys.executable,
                "-I",
                os.path.join(_REPO, "scripts", "make_samples.py"),
                "--root",
                gen_root,
            ],
            check=True,
            capture_output=True,
        )
        gen_output = os.path.join(self.tmp, "gen-output")
        app = create_app(
            tape_root=gen_root,
            output_root=gen_output,
            read_delay=0.0,
            consolidate_delay=0.0,
        )
        client = app.test_client()

        def wait_idle():
            deadline = time.time() + 10
            while time.time() < deadline:
                if not client.get("/status").get_json()["running"]:
                    return
                time.sleep(0.02)
            self.fail("任务超时")

        for name in ("sample-pair-a", "sample-pair-b"):
            resp = client.post(
                "/recover",
                data={"set_name": name, "read_quota": N, "staging_quota": 0},
            )
            self.assertEqual(resp.status_code, 302)
            wait_idle()
        img_a = client.get("/download/sample-pair-a").data
        img_b = client.get("/download/sample-pair-b").data
        self.assertEqual(len(img_a), N * BLOCK_SIZE)
        self.assertEqual(len(img_b), N * BLOCK_SIZE)
        # 同号块：前 8 个相同、后 4 个不同
        self.assertEqual(img_a[: S * BLOCK_SIZE], img_b[: S * BLOCK_SIZE])
        self.assertNotEqual(img_a[S * BLOCK_SIZE:], img_b[S * BLOCK_SIZE:])

        resp = client.post(
            "/consolidate",
            data={
                "set_a": "sample-pair-a",
                "set_b": "sample-pair-b",
                "extra_quota": D,
            },
        )
        self.assertEqual(resp.status_code, 302)
        deadline = time.time() + 10
        while time.time() < deadline:
            st = client.get("/status").get_json()
            if not st["running"]:
                self.assertTrue(st["result"]["success"])
                self.assertEqual(st["result"]["saved_blocks"], S)
                break
            time.sleep(0.02)
        else:
            self.fail("整理任务超时")
        self.assertEqual(client.get("/download/sample-pair-a").data, img_a)
        self.assertEqual(client.get("/download/sample-pair-b").data, img_b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
