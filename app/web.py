"""Flask 灾备恢复演练 Web 界面。

页面能力：

* 列出本地带卷目录中的全部合法带卷，并给出贪心策略下的额度预估；
* 选择一套带卷，填写读带块数上限与暂存块数上限后启动恢复演练；
* 恢复在后台线程执行，页面轮询展示实际读带块数、暂存峰值与事件流；
* 恢复中仍可下载上一次成品；成功后原子替换成品，失败（坏块/额度不足）
  则报错并保留上一次成品不变；
* 成品盘快满时，可选择两份尚未整理的等长成品做块级整理（去重）：
  同块号内容相同的块只留一份，整理期间额外占用受用户给定额度约束
  （临时文件里的镜像数据也算），页面展示实际省下的镜像数据块数；
* 已整理成品按块清单从块仓库拼流下载，逐块核对 SHA-256；重新恢复
  某套带卷只替换该套成品，另一套仍返回原字节，旧块按引用计数释放。
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from flask import (
    Flask,
    Response,
    abort,
    redirect,
    render_template,
    request,
    stream_with_context,
    url_for,
)

from .consolidate import consolidate
from .engine import FileImageSink, RecoveryResult, baseline, simulate_recovery
from .storage import BLOCK_SIZE, ManifestError, _SAFE_NAME, discover_sets, load_set, sha256_hex
from .store import BlockStore, StoreError

ARTIFACT_NAME = "image.bin"
MAX_EVENTS = 200


@dataclass
class JobState:
    running: bool = False
    kind: str | None = None               # "recover" | "consolidate"
    set_name: str | None = None           # 恢复目标
    pair: tuple[str, str] | None = None   # 整理对象
    read_quota: int = 0
    staging_quota: int = 0
    extra_quota: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0
    result: dict[str, Any] | None = None
    events: list[str] = field(default_factory=list)
    error: str | None = None


def create_app(
    tape_root: str | None = None,
    output_root: str | None = None,
    read_delay: float | None = None,
    consolidate_delay: float | None = None,
) -> Flask:
    app = Flask(__name__)
    tape_root = tape_root or os.environ.get("TAPE_ROOT", "data/tapes")
    output_root = output_root or os.environ.get("OUTPUT_ROOT", "data/output")
    if read_delay is None:
        read_delay = float(os.environ.get("READ_DELAY", "0.05"))
    if consolidate_delay is None:
        consolidate_delay = float(os.environ.get("CONSOLIDATE_DELAY", "0.05"))
    os.makedirs(output_root, exist_ok=True)

    # 块仓库：启动时依据磁盘清单重建引用关系并收敛中断的整理提交。
    store = BlockStore(output_root)
    store.recover()

    lock = threading.Lock()
    state = JobState()

    def artifact_path(set_name: str) -> str:
        return os.path.join(output_root, set_name, ARTIFACT_NAME)

    def run_recovery(set_name: str, read_quota: int, staging_quota: int) -> None:
        nonlocal state
        out_dir = os.path.join(output_root, set_name)
        os.makedirs(out_dir, exist_ok=True)
        tmp_path = os.path.join(out_dir, ARTIFACT_NAME + ".part")
        try:
            tape_set = load_set(tape_root, set_name)
            sink = FileImageSink(tmp_path)

            def on_sleep(_delay: float) -> None:
                time.sleep(_delay)

            result: RecoveryResult = simulate_recovery(
                tape_set,
                read_quota,
                staging_quota,
                sink=sink,
                read_delay=read_delay,
                sleep=on_sleep,
            )
            with lock:
                state.events = list(result.metrics.events[-MAX_EVENTS:])
                if result.success:
                    # 旧成品若是已整理的清单形态：先释放段引用（在途下载的
                    # fd 仍钉住旧数据），再原子发布新整镜像。
                    store.release_set_image(set_name)
                    os.replace(tmp_path, artifact_path(set_name))
                    result.artifact_path = artifact_path(set_name)
                else:
                    # 失败：丢弃半成品，上次成品原样保留。
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                state.result = _result_to_dict(result)
                state.error = result.error
        except Exception as exc:  # noqa: BLE001 - 线程内兜底，错误回传页面
            with lock:
                state.error = f"恢复任务异常: {exc}"
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
        finally:
            with lock:
                state.running = False
                state.finished_at = time.time()

    def run_consolidate(set_a: str, set_b: str, extra_quota: int) -> None:
        nonlocal state
        events: list[str] = ["整理任务已提交…"]
        try:
            result = consolidate(
                store,
                set_a,
                set_b,
                extra_quota,
                block_delay=consolidate_delay,
                sleep=time.sleep,
                log=events.append,
            )
            with lock:
                state.events = list(events[-MAX_EVENTS:])
                state.result = {
                    "kind": "consolidate",
                    "success": True,
                    "set_a": set_a,
                    "set_b": set_b,
                    "blocks": result.blocks,
                    "shared_blocks": result.shared_blocks,
                    "diff_blocks": result.diff_blocks,
                    "saved_blocks": result.saved_blocks,
                    "extra_peak_blocks": result.extra_peak_blocks,
                }
                state.error = None
        except Exception as exc:  # noqa: BLE001 - 线程内兜底，错误回传页面
            with lock:
                state.events = list(events[-MAX_EVENTS:])
                state.result = {
                    "kind": "consolidate",
                    "success": False,
                    "set_a": set_a,
                    "set_b": set_b,
                }
                state.error = str(exc)
        finally:
            with lock:
                state.running = False
                state.finished_at = time.time()

    def _result_to_dict(result: RecoveryResult) -> dict[str, Any]:
        return {
            "kind": "recover",
            "success": result.success,
            "set_name": result.set_name,
            "error": result.error,
            "blocks_written": result.blocks_written,
            "metrics": asdict(result.metrics) | {"events": []},
            "artifact_path": result.artifact_path,
        }

    @app.get("/")
    def index():  # type: ignore[override]
        sets = []
        for tape_set in discover_sets(tape_root):
            est = baseline(tape_set)
            sets.append(
                {
                    "name": tape_set.name,
                    "block_count": tape_set.block_count,
                    "tape_count": tape_set.tape_count,
                    "estimate": est,
                    "product": store.product(tape_set.name),
                }
            )
        products = store.list_products()
        plain_products = [p for p in products if p["state"] == "plain"]
        with lock:
            job = {
                "running": state.running,
                "kind": state.kind,
                "set_name": state.set_name,
                "pair": list(state.pair) if state.pair else None,
                "extra_quota": state.extra_quota,
                "result": state.result,
                "error": state.error,
                "events": list(state.events),
            }
        return render_template(
            "index.html",
            sets=sets,
            products=products,
            plain_products=plain_products,
            store_stats=store.stats(),
            job=job,
        )

    @app.post("/recover")
    def recover():  # type: ignore[override]
        set_name = request.form.get("set_name", "").strip()
        try:
            read_quota = int(request.form.get("read_quota", ""))
            staging_quota = int(request.form.get("staging_quota", ""))
        except (TypeError, ValueError):
            abort(400, "额度必须是整数")
        if read_quota <= 0 or staging_quota < 0:
            abort(400, "读带额度必须为正整数，暂存额度必须为非负整数")
        try:
            tape_set = load_set(tape_root, set_name)
        except ManifestError as exc:
            abort(400, str(exc))

        with lock:
            if state.running:
                abort(409, "已有任务在执行，请等待完成")
            state.running = True
            state.kind = "recover"
            state.set_name = tape_set.name
            state.pair = None
            state.read_quota = read_quota
            state.staging_quota = staging_quota
            state.extra_quota = 0
            state.started_at = time.time()
            state.finished_at = 0.0
            state.result = None
            state.events = ["任务已提交，开始恢复演练…"]
            state.error = None

        thread = threading.Thread(
            target=run_recovery,
            args=(tape_set.name, read_quota, staging_quota),
            daemon=True,
        )
        thread.start()
        return redirect(url_for("index"))

    @app.post("/consolidate")
    def consolidate_view():  # type: ignore[override]
        set_a = request.form.get("set_a", "").strip()
        set_b = request.form.get("set_b", "").strip()
        try:
            extra_quota = int(request.form.get("extra_quota", ""))
        except (TypeError, ValueError):
            abort(400, "额外占用上限必须是整数")
        if extra_quota < 0:
            abort(400, "额外占用上限必须是非负整数")
        if not set_a or not set_b or set_a == set_b:
            abort(400, "请选择两份不同的成品")
        for name in (set_a, set_b):
            if not _SAFE_NAME.fullmatch(name):
                abort(400, "非法成品名")

        # 资格校验：两份都必须是尚未整理的整镜像，且等长。
        info_a = store.product(set_a)
        info_b = store.product(set_b)
        if info_a is None or info_a["state"] != "plain":
            abort(400, f"成品 {set_a} 不存在或已整理，只能选择尚未整理的成品")
        if info_b is None or info_b["state"] != "plain":
            abort(400, f"成品 {set_b} 不存在或已整理，只能选择尚未整理的成品")
        if info_a["size"] != info_b["size"]:
            abort(400, "两份成品长度不一致，无法整理")

        with lock:
            if state.running:
                abort(409, "已有任务在执行，请等待完成")
            state.running = True
            state.kind = "consolidate"
            state.set_name = None
            state.pair = (set_a, set_b)
            state.extra_quota = extra_quota
            state.read_quota = 0
            state.staging_quota = 0
            state.started_at = time.time()
            state.finished_at = 0.0
            state.result = None
            state.events = ["整理任务已提交…"]
            state.error = None

        thread = threading.Thread(
            target=run_consolidate,
            args=(set_a, set_b, extra_quota),
            daemon=True,
        )
        thread.start()
        return redirect(url_for("index"))

    @app.get("/status")
    def status():  # type: ignore[override]
        with lock:
            return {
                "running": state.running,
                "kind": state.kind,
                "set_name": state.set_name,
                "pair": list(state.pair) if state.pair else None,
                "extra_quota": state.extra_quota,
                "result": state.result,
                "error": state.error,
                "events": list(state.events),
            }

    @app.get("/download/<set_name>")
    def download(set_name: str):  # type: ignore[override]
        if not _SAFE_NAME.fullmatch(set_name):
            abort(404)
        try:
            opened = store.open_product(set_name)
        except StoreError as exc:
            abort(500, f"块仓库读取失败: {exc}")
        if opened is None:
            abort(404, "该带卷尚无已交付的镜像成品")
        download_name = f"{set_name}-image.bin"

        if opened.kind == "plain":
            # 文件对象已在块仓库锁内打开，替换/删除整镜像不影响本次下载；
            # 生成器结束时统一关闭，不依赖 WSGI 服务器的关闭时机。
            fileobj = opened.fileobj

            def generate_plain():
                try:
                    while True:
                        chunk = fileobj.read(BLOCK_SIZE)
                        if not chunk:
                            break
                        yield chunk
                finally:
                    fileobj.close()

            response = Response(
                stream_with_context(generate_plain()),
                mimetype="application/octet-stream",
            )
        else:
            # 已整理成品：按清单从段文件拼流，逐块核对 SHA-256；
            # 段 fd 在请求开始时全部打开（钉住 inode），读完再关闭。
            assert opened.manifest is not None and opened.seg_fds is not None
            manifest = opened.manifest
            fds = opened.seg_fds

            def generate():
                try:
                    for i, entry in enumerate(manifest.entries):
                        data = os.pread(
                            fds[entry.seg], BLOCK_SIZE, entry.offset * BLOCK_SIZE
                        )
                        if len(data) != BLOCK_SIZE or sha256_hex(data) != entry.sha256:
                            raise StoreError(
                                f"段数据校验失败：{set_name} 块 {i} SHA-256 不符"
                            )
                        yield data
                finally:
                    for fd in fds.values():
                        os.close(fd)

            response = Response(
                stream_with_context(generate()),
                mimetype="application/octet-stream",
            )

        response.headers["Content-Length"] = str(opened.size)
        response.headers["Content-Disposition"] = (
            f'attachment; filename="{download_name}"'
        )
        return response

    return app
