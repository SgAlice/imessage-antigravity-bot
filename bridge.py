#!/usr/bin/env python3
"""Single-group iMessage -> persistent Antigravity (agy stream-json) bridge.

Runs cleanly inside a dedicated terminal with zero third-party dependencies.
AI permissions are strictly confined to this folder via agy_settings.json + --sandbox.
"""

import argparse
import asyncio
import collections
import datetime
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
CONFIG_EXAMPLE_PATH = BASE_DIR / "config.example.json"
SETTINGS_PATH = BASE_DIR / "agy_settings.json"
SETTINGS_TEMPLATE_PATH = BASE_DIR / "agy_settings.template.json"
STATE_PATH = BASE_DIR / "state.json"
INBOX_DIR = BASE_DIR / "_inbox"
OUTBOX_DIR = BASE_DIR / "_outbox"
BRAIN_ROOT = Path.home() / ".gemini" / "antigravity-cli" / "brain"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".heic", ".heif", ".bmp"}
OUTBOUND_EXTS = IMAGE_EXTS | {".svg"}
IMAGE_TAG_RE = re.compile(r"\[IMAGE:([^\]]+)\]")
FILE_LINK_RE = re.compile(r"\[([^\]]+)\]\(file://[^)]+\)")


def log(tag: str, msg: str) -> None:
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{tag}] {msg}", flush=True)


def resolve_bin(value: str, fallbacks: List[Path]) -> str:
    if value and Path(value).is_file():
        return value
    found = shutil.which(value)
    if found:
        return found
    for cand in fallbacks:
        if cand.is_file():
            return str(cand)
    return value


def ensure_settings_file() -> Path:
    if SETTINGS_PATH.exists():
        return SETTINGS_PATH
    if not SETTINGS_TEMPLATE_PATH.exists():
        raise FileNotFoundError(f"缺少权限模板文件: {SETTINGS_TEMPLATE_PATH}")
    rendered = SETTINGS_TEMPLATE_PATH.read_text(encoding="utf-8").replace(
        "__WORKDIR__", str(BASE_DIR)
    )
    SETTINGS_PATH.write_text(rendered, encoding="utf-8")
    log("INIT", f"已根据当前目录自动生成 {SETTINGS_PATH.name}")
    return SETTINGS_PATH


def load_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        if CONFIG_EXAMPLE_PATH.exists():
            shutil.copy2(CONFIG_EXAMPLE_PATH, CONFIG_PATH)
            log("INIT", f"已从 {CONFIG_EXAMPLE_PATH.name} 初始化 {CONFIG_PATH.name}")
        else:
            raise FileNotFoundError(f"缺少配置文件: {CONFIG_PATH}")
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["agy_bin"] = resolve_bin(
        str(cfg.get("agy_bin") or "agy"),
        [Path.home() / ".local" / "bin" / "agy", Path("/opt/homebrew/bin/agy"), Path("/usr/local/bin/agy")],
    )
    cfg["imsg_bin"] = resolve_bin(
        str(cfg.get("imsg_bin") or "imsg"),
        [Path("/opt/homebrew/bin/imsg"), Path("/usr/local/bin/imsg")],
    )
    return cfg


def load_state() -> Dict[str, Any]:
    if STATE_PATH.exists():
        try:
            with STATE_PATH.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            log("WARN", f"读取 state.json 失败，将重置状态: {exc}")
    return {}


def save_state(state: Dict[str, Any]) -> None:
    tmp = STATE_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def format_duration(seconds: int) -> str:
    minutes = max(0, seconds) // 60
    hours, mins = divmod(minutes, 60)
    if hours > 0:
        return f"{hours} 小时 {mins} 分钟"
    return f"{mins} 分钟"


def calc_cache_rate(input_tokens: int, cache_read_tokens: int) -> float:
    denom = input_tokens + cache_read_tokens
    if denom <= 0:
        return 0.0
    return (cache_read_tokens / denom) * 100.0


def default_stats() -> Dict[str, int]:
    return {
        "total_turns": 0,
        "input_tokens": 0,
        "cache_read_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "last_turn_input_tokens": 0,
        "last_turn_cache_read_tokens": 0,
        "uptime_seconds": 0,
    }


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def prune_dir(directory: Path, max_files: int) -> None:
    if max_files <= 0 or not directory.exists():
        return
    files = [p for p in directory.iterdir() if p.is_file()]
    if len(files) <= max_files:
        return
    files.sort(key=lambda p: p.stat().st_mtime)
    for old in files[: len(files) - max_files]:
        try:
            old.unlink()
        except Exception:
            pass


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def stage_inbound_image(src_path: Path, rowid: int, idx: int, max_bytes: int, max_keep: int) -> Optional[Path]:
    """Copy an inbound iMessage image into _inbox/ (converting HEIC/HEIF -> JPG via sips)."""
    if not src_path.exists() or not src_path.is_file():
        return None
    if src_path.stat().st_size > max_bytes:
        log("IMG", f"跳过过大图片 ({src_path.stat().st_size} bytes): {src_path.name}")
        return None

    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    ext = src_path.suffix.lower() or ".jpg"
    stem = f"{rowid or int(time.time())}_{idx}"

    if ext in (".heic", ".heif"):
        dst = INBOX_DIR / f"{stem}.jpg"
        proc = subprocess.run(
            ["/usr/bin/sips", "-s", "format", "jpeg", str(src_path), "--out", str(dst)],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0 or not dst.exists():
            log("IMG-WARN", f"HEIC 转换失败，回退直接复制: {proc.stderr.strip()}")
            dst = INBOX_DIR / f"{stem}{ext}"
            shutil.copy2(src_path, dst)
    else:
        dst = INBOX_DIR / f"{stem}{ext}"
        shutil.copy2(src_path, dst)

    prune_dir(INBOX_DIR, max_keep)
    return dst


def extract_outbound_images(
    text: str,
    new_brain_images: List[Path],
    conversation_id: str,
    max_keep: int,
) -> Tuple[str, List[Path]]:
    """Parse [IMAGE:...] tags + newly generated brain images, enforcing strict path allowlist."""
    allowed_roots = [BASE_DIR]
    if conversation_id:
        allowed_roots.append(BRAIN_ROOT / conversation_id)

    raw_candidates: List[Path] = []
    for m in IMAGE_TAG_RE.finditer(text):
        raw = m.group(1).strip().strip("'\"")
        if raw.startswith("file://"):
            raw = raw[len("file://") :]
        p = Path(raw)
        if not p.is_absolute():
            p = BASE_DIR / p
        raw_candidates.append(p)

    # Also include any newly created images in the current conversation's brain dir
    raw_candidates.extend(new_brain_images)

    # Strip [IMAGE:...] tags and local file:// links from outbound text
    cleaned = IMAGE_TAG_RE.sub("", text)
    cleaned = FILE_LINK_RE.sub(r"\1", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    selected: List[Path] = []
    seen_real: Set[Path] = set()
    for cand in raw_candidates:
        try:
            real = cand.resolve()
        except Exception:
            continue
        if real in seen_real or not real.exists() or not real.is_file():
            continue
        if real.suffix.lower() not in OUTBOUND_EXTS:
            log("IMG-DENY", f"拒绝发送非图片文件: {real}")
            continue
        if not any(is_within(real, root) for root in allowed_roots):
            log("IMG-DENY", f"拒绝发送越权路径图片: {real}")
            continue
        seen_real.add(real)

        # Auto-convert .svg -> high-res .png so iMessage renders an inline image preview
        if real.suffix.lower() == ".svg":
            try:
                import svg2png

                rendered = svg2png.render_svg_to_png(real, OUTBOX_DIR / f"{int(time.time())}_{real.stem}.png")
                prune_dir(OUTBOX_DIR, max_keep)
                selected.append(rendered)
                log("IMG", f"已自动将 SVG 渲染为 PNG: {rendered.name}")
            except Exception as exc:
                log("IMG-WARN", f"SVG 渲染 PNG 失败: {exc}")
            continue

        # If image came from brain dir, archive a copy into _outbox/ inside chatbot folder
        if not is_within(real, BASE_DIR):
            dst = OUTBOX_DIR / f"{int(time.time())}_{real.name}"
            shutil.copy2(real, dst)
            prune_dir(OUTBOX_DIR, max_keep)
            selected.append(dst)
        else:
            selected.append(real)

    return cleaned, selected


def split_message(text: str, max_chars: int) -> List[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    chunks: List[str] = []
    remaining = text
    while len(remaining) > max_chars:
        cut = remaining.rfind("\n", 0, max_chars)
        if cut < max_chars // 2:
            cut = remaining.rfind(" ", 0, max_chars)
        if cut < max_chars // 2:
            cut = max_chars
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


class AgyStreamWorker:
    """Maintains a single warm persistent `agy --input-format stream-json` process."""

    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.stderr_task: Optional[asyncio.Task] = None
        self.lock = asyncio.Lock()
        self.conversation_id: str = ""
        self.num_turns: int = 0
        self.session_input_tokens: int = 0
        self.session_cache_read_tokens: int = 0
        self.session_output_tokens: int = 0
        self.total_tokens: int = 0
        self.started_at: float = 0.0

    async def start(self) -> None:
        if self.proc and self.proc.returncode is None:
            return

        settings_file = ensure_settings_file()
        cmd = [
            self.cfg["agy_bin"],
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--sandbox",
            "--disable-slash-commands",
            "--settings",
            str(settings_file),
        ]
        if self.cfg.get("model"):
            cmd.extend(["--model", str(self.cfg["model"])])
        if self.cfg.get("effort"):
            cmd.extend(["--effort", str(self.cfg["effort"])])

        log("AGY", f"启动常驻进程 (cwd={BASE_DIR}, model={self.cfg.get('model') or 'default'})")
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(BASE_DIR),
            limit=4 * 1024 * 1024,
        )
        self.num_turns = 0
        self.session_input_tokens = 0
        self.session_cache_read_tokens = 0
        self.session_output_tokens = 0
        self.total_tokens = 0
        self.started_at = time.time()
        self.stderr_task = asyncio.create_task(self._drain_stderr())

        # Read the initial `init` event
        init_line = await asyncio.wait_for(self._read_stdout_json(), timeout=30)
        if not init_line:
            raise RuntimeError("agy 启动后意外关闭，未收到 init 事件")
        if init_line.get("event") == "init":
            self.conversation_id = init_line.get("conversation_id", "")
            info = init_line.get("init", {})
            log(
                "AGY",
                f"已就绪 | conv={self.conversation_id[:8]} | "
                f"model={info.get('model', 'default')} | "
                f"mode={info.get('permission_mode', '?')}",
            )
        else:
            log("AGY", f"收到非预期首包: {init_line}")

    async def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        try:
            while True:
                raw = await self.proc.stderr.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    log("AGY-ERR", line)
        except asyncio.CancelledError:
            pass

    async def _read_stdout_json(self) -> Optional[Dict[str, Any]]:
        assert self.proc and self.proc.stdout
        while True:
            raw = await self.proc.stdout.readline()
            if not raw:
                return None
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                log("AGY-RAW", line[:200])

    async def stop(self) -> None:
        proc = self.proc
        self.proc = None
        if self.stderr_task:
            self.stderr_task.cancel()
            self.stderr_task = None
        if proc and proc.returncode is None:
            try:
                if proc.stdin and not proc.stdin.is_closing():
                    proc.stdin.close()
                await asyncio.wait_for(proc.wait(), timeout=3)
            except Exception:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except Exception:
                    proc.kill()
                    await proc.wait()

    async def reset(self) -> None:
        async with self.lock:
            log("AGY", "重置会话，重启常驻 agy 进程...")
            await self.stop()
            await self.start()

    def _list_brain_images(self) -> Set[Path]:
        if not self.conversation_id:
            return set()
        bdir = BRAIN_ROOT / self.conversation_id
        if not bdir.exists():
            return set()
        return {
            p.resolve()
            for p in bdir.iterdir()
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS
        }

    async def ask(self, prompt: str) -> Tuple[str, Dict[str, Any]]:
        async with self.lock:
            max_turns = int(self.cfg.get("max_turns_before_reset") or 0)
            if max_turns > 0 and self.num_turns >= max_turns:
                log("AGY", f"已达 {max_turns} 轮上限，自动开启新会话以节省上下文")
                await self.stop()

            if not self.proc or self.proc.returncode is not None:
                await self.start()

            before_brain_imgs = self._list_brain_images()

            assert self.proc and self.proc.stdin
            payload = {"event": "user", "message": {"content": prompt}}
            self.proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            await self.proc.stdin.drain()

            timeout = float(self.cfg.get("turn_timeout_seconds") or 180)
            t0 = time.time()
            last_resp_step: Optional[int] = None
            last_resp_parts: List[str] = []
            try:
                while True:
                    remaining = timeout - (time.time() - t0)
                    if remaining <= 0:
                        raise asyncio.TimeoutError()
                    ev = await asyncio.wait_for(self._read_stdout_json(), timeout=remaining)
                    if ev is None:
                        raise RuntimeError("agy 进程在生成回复期间意外退出")

                    ev_type = ev.get("event")
                    if ev_type == "step_update":
                        su = ev.get("step_update", {})
                        st = su.get("step_type")
                        state = su.get("state")
                        if st == "agent_response":
                            sidx = su.get("step_index")
                            delta = su.get("text_delta") or ""
                            if sidx != last_resp_step:
                                last_resp_step = sidx
                                last_resp_parts = []
                            if delta:
                                last_resp_parts.append(delta)
                        elif st == "tool" and state in ("DONE", "ERROR"):
                            tname = su.get("tool_name") or su.get("tool_info", {}).get("name", "tool")
                            err = su.get("tool_info", {}).get("error")
                            if state == "ERROR" or err:
                                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                                log("TOOL-DENY", f"{tname} 被拦截/失败: {msg.splitlines()[0]}")
                            else:
                                log("TOOL", f"{tname} 完成 ({su.get('duration_seconds', 0):.2f}s)")
                    elif ev_type == "result":
                        res = ev.get("result", {})
                        status = res.get("status", "UNKNOWN")
                        prev_turns = self.num_turns
                        prev_in = self.session_input_tokens
                        prev_cache = self.session_cache_read_tokens
                        prev_out = self.session_output_tokens
                        prev_total = self.total_tokens

                        self.num_turns = int(res.get("num_turns", self.num_turns + 1))
                        usage = res.get("usage", {})
                        self.session_input_tokens = int(usage.get("input_tokens", prev_in))
                        self.session_cache_read_tokens = int(usage.get("cache_read_tokens", prev_cache))
                        self.session_output_tokens = int(usage.get("output_tokens", prev_out))
                        self.total_tokens = int(usage.get("total_tokens", prev_total))

                        turn_delta = {
                            "turns": max(0, self.num_turns - prev_turns),
                            "input_tokens": max(0, self.session_input_tokens - prev_in),
                            "cache_read_tokens": max(0, self.session_cache_read_tokens - prev_cache),
                            "output_tokens": max(0, self.session_output_tokens - prev_out),
                            "total_tokens": max(0, self.total_tokens - prev_total),
                        }
                        res["_turn_delta"] = turn_delta
                        turn_cache_rate = calc_cache_rate(
                            turn_delta["input_tokens"], turn_delta["cache_read_tokens"]
                        )

                        denied = res.get("denied_actions") or []
                        elapsed = time.time() - t0
                        log(
                            "AGY",
                            f"回复完成 | status={status} | {elapsed:.1f}s | "
                            f"turns={self.num_turns} | cache={turn_cache_rate:.1f}% | "
                            f"total_tokens={self.total_tokens}"
                            + (f" | denied={len(denied)}" if denied else ""),
                        )
                        if status != "SUCCESS":
                            err_msg = res.get("error") or f"status={status}"
                            # Restart process if stream ended in ERROR
                            await self.stop()
                            return f"处理出错：{err_msg}", res
                        if denied:
                            actions = ", ".join(
                                str(a.get("display_name") or a.get("action") or "受限操作")
                                for a in denied
                            )
                            return (
                                f"抱歉，该操作超出权限范围，已被安全沙箱拦截（{actions}）。"
                                f"我只能读写 {BASE_DIR.name} 文件夹内的普通文件。"
                            ), res
                        final_text = "".join(last_resp_parts).strip() or (res.get("response") or "").strip()
                        after_brain_imgs = self._list_brain_images()
                        new_imgs = sorted(
                            after_brain_imgs - before_brain_imgs,
                            key=lambda p: p.stat().st_mtime,
                        )
                        res["_new_brain_images"] = new_imgs
                        return final_text, res
            except asyncio.TimeoutError:
                log("WARN", f"agy 回复超时 ({timeout}s)，将重启进程")
                await self.stop()
                kw0 = (self.cfg.get("keywords") or [""])[0]
                return f"思考超时了，请稍后再试或发「{kw0} /new」重置对话。", {}
            except Exception as exc:
                log("ERROR", f"agy 调用异常: {exc}")
                await self.stop()
                return f"内部异常，已自动重置会话：{exc}", {}


class GroupChatBridge:
    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg
        self.state = load_state()
        self._ensure_stats()
        self.agy = AgyStreamWorker(cfg)
        buf_limit = int(cfg.get("context_buffer_size") or 0)
        self.context_buf: Deque[str] = collections.deque(
            maxlen=buf_limit if buf_limit > 0 else None
        )
        self.pending_triggers: List[Tuple[str, str]] = []
        self.debounce_task: Optional[asyncio.Task] = None
        self.process_lock = asyncio.Lock()
        self.stop_event = asyncio.Event()
        self.watch_proc: Optional[asyncio.subprocess.Process] = None
        self.boot_at: float = time.time()
        self.uptime_flushed_at: float = self.boot_at
        self.recent_sent_hashes: Deque[str] = collections.deque(maxlen=50)

    def _ensure_stats(self) -> Dict[str, int]:
        stats = self.state.get("stats")
        if not isinstance(stats, dict):
            stats = default_stats()
            self.state["stats"] = stats
        else:
            for k, v in default_stats().items():
                stats.setdefault(k, v)
        return stats

    def _flush_uptime(self) -> None:
        now = time.time()
        delta = int(now - self.uptime_flushed_at)
        if delta > 0:
            stats = self._ensure_stats()
            stats["uptime_seconds"] = int(stats.get("uptime_seconds", 0)) + delta
            self.uptime_flushed_at += delta

    def _record_turn_stats(self, meta: Dict[str, Any]) -> None:
        delta = meta.get("_turn_delta") if isinstance(meta, dict) else None
        if not isinstance(delta, dict):
            return
        stats = self._ensure_stats()
        stats["total_turns"] += int(delta.get("turns", 0))
        stats["input_tokens"] += int(delta.get("input_tokens", 0))
        stats["cache_read_tokens"] += int(delta.get("cache_read_tokens", 0))
        stats["output_tokens"] += int(delta.get("output_tokens", 0))
        stats["total_tokens"] += int(delta.get("total_tokens", 0))
        if delta.get("input_tokens", 0) or delta.get("cache_read_tokens", 0):
            stats["last_turn_input_tokens"] = int(delta.get("input_tokens", 0))
            stats["last_turn_cache_read_tokens"] = int(delta.get("cache_read_tokens", 0))
        self._flush_uptime()
        save_state(self.state)

    def build_status_message(self) -> str:
        self._flush_uptime()
        save_state(self.state)
        stats = self._ensure_stats()
        cur_uptime = int(time.time() - self.boot_at)
        total_uptime = int(stats.get("uptime_seconds", 0))
        last_rate = calc_cache_rate(
            int(stats.get("last_turn_input_tokens", 0)),
            int(stats.get("last_turn_cache_read_tokens", 0)),
        )
        cum_in = int(stats.get("input_tokens", 0))
        cum_cache = int(stats.get("cache_read_tokens", 0))
        cum_rate = calc_cache_rate(cum_in, cum_cache)
        cum_prompt = cum_in + cum_cache
        return (
            f"在线运行中\n"
            f"模型: {self.cfg.get('model') or 'default'}\n"
            f"当前会话 / 累计轮数: {self.agy.num_turns} / {stats['total_turns']}\n"
            f"最近一轮缓存率: {last_rate:.1f}%\n"
            f"累计缓存率: {cum_rate:.1f}% ({cum_cache:,} / {cum_prompt:,})\n"
            f"累计消耗 Token: {stats['total_tokens']:,}\n"
            f"本次 / 累计运行: {format_duration(cur_uptime)} / {format_duration(total_uptime)}"
        )

    def _chat_id(self) -> int:
        cid = self.cfg.get("chat_id")
        if cid is None:
            raise ValueError("config.json 中尚未设置 chat_id")
        return int(cid)

    async def _imsg_latest_rowid(self, chat_id: int) -> int:
        proc = await asyncio.create_subprocess_exec(
            self.cfg["imsg_bin"],
            "history",
            "--chat-id",
            str(chat_id),
            "--limit",
            "1",
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            log("WARN", f"获取最新消息 rowid 失败: {err.decode('utf-8', errors='replace').strip()}")
            return 0
        for raw in out.decode("utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                d = json.loads(raw)
                rid = d.get("id") or d.get("rowid") or 0
                if rid:
                    return int(rid)
            except Exception:
                pass
        return 0

    async def send_imessage(self, text: str, images: Optional[List[Path]] = None) -> None:
        prefix = self.cfg.get("reply_prefix", "🤖 ")
        max_chars = int(self.cfg.get("max_reply_chars") or 1500)
        chunks = split_message(text, max(100, max_chars - len(prefix)))
        imgs = list(images or [])
        chat_id = self._chat_id()
        service = self.cfg.get("send_service", "imessage")

        async def _dispatch(body: Optional[str], img: Optional[Path], label: str) -> None:
            cmd = [
                self.cfg["imsg_bin"],
                "send",
                "--chat-id",
                str(chat_id),
                "--service",
                service,
            ]
            if body:
                cmd.extend(["--text", body])
            if img:
                try:
                    self.recent_sent_hashes.append(file_sha256(img))
                except Exception:
                    pass
                cmd.extend(["--file", str(img)])
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err = await proc.communicate()
            if proc.returncode != 0:
                log("SEND-ERR", err.decode("utf-8", errors="replace").strip())
            else:
                preview = (body or "").replace("\n", " ")[:80]
                img_note = f" + [图:{img.name}]" if img else ""
                log("SEND", f"已发送{label}: {preview}{img_note}")

        if not chunks and imgs:
            for idx, img in enumerate(imgs):
                cap = f"{prefix}图来了" if idx == 0 else None
                await _dispatch(cap, img, f" (图 {idx + 1}/{len(imgs)})")
            return

        for idx, chunk in enumerate(chunks):
            body = f"{prefix}{chunk}"
            first_img = imgs.pop(0) if (idx == 0 and imgs) else None
            suffix = f" ({idx + 1}/{len(chunks)})" if len(chunks) > 1 else ""
            await _dispatch(body, first_img, suffix)

        for idx, extra_img in enumerate(imgs, start=2):
            await _dispatch(None, extra_img, f" (附图 {idx})")

    def _match_keyword(self, text: str) -> Optional[str]:
        keywords = self.cfg.get("keywords") or []
        exact_keywords = self.cfg.get("exact_keywords") or []
        case_sensitive = bool(self.cfg.get("case_sensitive", False))
        haystack = text if case_sensitive else text.lower()
        for kw in keywords:
            if not kw:
                continue
            needle = kw if case_sensitive else kw.lower()
            if needle in haystack:
                return kw
        if exact_keywords:
            # Strip whitespace and common Chinese/English punctuation for exact triggers
            normalized = re.sub(r"[\s\.,!?;:'\"()\[\]{}<>，。！？；：、（）【】《》“”‘’~`—…·]+", "", haystack)
            for kw in exact_keywords:
                if not kw:
                    continue
                needle = kw if case_sensitive else kw.lower()
                if normalized == needle:
                    return kw
        return None

    def _strip_keyword(self, text: str, matched_kw: str) -> str:
        case_sensitive = bool(self.cfg.get("case_sensitive", False))
        if case_sensitive:
            return text.replace(matched_kw, "", 1).strip()
        idx = text.lower().find(matched_kw.lower())
        if idx >= 0:
            return (text[:idx] + text[idx + len(matched_kw) :]).strip()
        return text.strip()

    def _extract_inbound_images(self, msg: Dict[str, Any], rowid: int, is_from_me: bool) -> List[Path]:
        if not self.cfg.get("enable_images", True):
            return []
        atts = msg.get("attachments") or []
        if not atts:
            return []
        max_bytes = int(float(self.cfg.get("max_image_mb") or 20) * 1024 * 1024)
        max_keep = int(self.cfg.get("max_inbox_images") or 30)
        staged: List[Path] = []
        for idx, att in enumerate(atts):
            if not isinstance(att, dict) or att.get("missing"):
                continue
            mime = str(att.get("converted_mime_type") or att.get("mime_type") or "").lower()
            raw_path = att.get("converted_path") or att.get("original_path") or ""
            if not raw_path:
                continue
            src = Path(raw_path).expanduser()
            is_img = mime.startswith("image/") or src.suffix.lower() in IMAGE_EXTS
            if not is_img or not src.exists():
                continue
            if is_from_me and self.recent_sent_hashes:
                try:
                    if file_sha256(src) in self.recent_sent_hashes:
                        log("IMG", f"跳过机器人自己刚发出的图片: {src.name}")
                        continue
                except Exception:
                    pass
            dst = stage_inbound_image(src, rowid, idx, max_bytes, max_keep)
            if dst:
                staged.append(dst)
                log("IMG", f"已收图入库: {dst.name}")
        return staged

    async def _handle_message(self, msg: Dict[str, Any]) -> None:
        # Defensive field extraction across imsg versions
        rowid = int(msg.get("id") or msg.get("rowid") or 0)
        chat_id = int(msg.get("chat_id") or msg.get("chatId") or self._chat_id())
        if chat_id != self._chat_id():
            return

        last_rowid = int(self.state.get("last_rowid") or 0)
        if rowid and rowid <= last_rowid:
            return
        if rowid:
            self.state["last_rowid"] = rowid
            save_state(self.state)

        if msg.get("is_reaction") or msg.get("isReaction"):
            return

        text = (msg.get("text") or "").strip()
        # Strip Apple's invisible object replacement char (U+FFFC) attached to media messages
        clean_text = text.replace("\ufffc", "").strip()

        prefix = self.cfg.get("reply_prefix", "🤖 ")
        if prefix and clean_text.startswith(prefix):
            # Ignore our own bot replies to guarantee zero self-trigger loops
            return

        is_from_me = bool(msg.get("is_from_me") if "is_from_me" in msg else msg.get("isFromMe", False))
        sender = (
            msg.get("sender")
            or msg.get("sender_name")
            or msg.get("handle")
            or ("我" if is_from_me else "群成员")
        )

        if is_from_me and not self.cfg.get("allow_self_trigger", True):
            return

        allow_senders = self.cfg.get("allow_senders") or []
        if allow_senders and not is_from_me and sender not in allow_senders:
            log("SKIP", f"非白名单成员 {sender}: {clean_text[:40]}")
            return

        inbound_imgs = self._extract_inbound_images(msg, rowid, is_from_me)
        if not clean_text and not inbound_imgs:
            return

        img_tags = " ".join(f"[图片附件: {p}]" for p in inbound_imgs)
        combined = f"{clean_text} {img_tags}".strip() if img_tags else clean_text

        matched_kw = self._match_keyword(clean_text) if clean_text else None
        if not matched_kw:
            # Buffer silently (including standalone photos!) without touching the AI
            self.context_buf.append(f"[{sender}]: {combined}")
            log("BUF", f"静默缓存 (累计 {len(self.context_buf)} 条) [{sender}]: {combined[:60]}")
            return

        log("HIT", f"命中关键词「{matched_kw}」[{sender}]: {combined[:80]}")
        cleaned = self._strip_keyword(clean_text, matched_kw)

        # Built-in slash commands inside the group
        lower_cmd = cleaned.lower()
        if lower_cmd in ("/new", "重置对话", "清空上下文"):
            async with self.process_lock:
                self.context_buf.clear()
                self.pending_triggers.clear()
                await self.agy.reset()
                await self.send_imessage("已清空上下文，开启新对话。")
            return

        if lower_cmd in ("/status", "状态"):
            await self.send_imessage(self.build_status_message())
            return

        # Debounce rapid consecutive trigger messages (keep full original text + image tags)
        self.pending_triggers.append((sender, combined))
        if self.debounce_task and not self.debounce_task.done():
            self.debounce_task.cancel()
        delay = float(self.cfg.get("debounce_seconds") or 0)
        self.debounce_task = asyncio.create_task(self._flush_after_delay(delay))

    async def _flush_after_delay(self, delay: float) -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            await self._process_pending()
        except asyncio.CancelledError:
            pass

    async def _process_pending(self) -> None:
        async with self.process_lock:
            if not self.pending_triggers:
                return
            batch = list(self.pending_triggers)
            self.pending_triggers.clear()

            parts: List[str] = []
            buf_count = len(self.context_buf)
            if self.context_buf:
                parts.append("[最近群聊背景]")
                parts.extend(self.context_buf)
                self.context_buf.clear()
                parts.append("")

            parts.append("[当前触发消息]")
            for sender, body in batch:
                parts.append(f"[{sender}]: {body}")
            prompt = "\n".join(parts)
            log("PACK", f"打包本轮输入: {buf_count} 条静默缓存 + {len(batch)} 条触发消息")

            ack_delay = float(self.cfg.get("ack_delay_seconds") or 0)
            ack_task: Optional[asyncio.Task] = None
            if ack_delay > 0:
                async def _send_ack() -> None:
                    await asyncio.sleep(ack_delay)
                    await self.send_imessage(self.cfg.get("ack_text", "⏳ 正在处理中..."))
                ack_task = asyncio.create_task(_send_ack())

            try:
                reply, meta = await self.agy.ask(prompt)
                self._record_turn_stats(meta)
            finally:
                if ack_task and not ack_task.done():
                    ack_task.cancel()

            if reply or (isinstance(meta, dict) and meta.get("_new_brain_images")):
                if self.cfg.get("enable_images", True):
                    clean_reply, out_imgs = extract_outbound_images(
                        reply or "",
                        meta.get("_new_brain_images") or [],
                        self.agy.conversation_id,
                        int(self.cfg.get("max_outbox_images") or 30),
                    )
                    if clean_reply or out_imgs:
                        await self.send_imessage(clean_reply, out_imgs)
                elif reply:
                    await self.send_imessage(reply)

    async def run(self) -> None:
        chat_id = self._chat_id()

        # Initialize last_rowid on first run so we never replay old messages (preserve persisted stats)
        if self.state.get("chat_id") != chat_id or not self.state.get("last_rowid"):
            latest = await self._imsg_latest_rowid(chat_id)
            self.state["chat_id"] = chat_id
            self.state["last_rowid"] = latest
            self._ensure_stats()
            save_state(self.state)
            log("INIT", f"首次绑定群 chat_id={chat_id}，起点 rowid={latest}（不会回放历史消息）")
        else:
            self._ensure_stats()
            log("INIT", f"恢复监听群 chat_id={chat_id}，起点 rowid={self.state['last_rowid']}")

        # Pre-warm persistent agy process
        await self.agy.start()

        keywords_str = " / ".join(self.cfg.get("keywords") or [])
        exact_str = " / ".join(self.cfg.get("exact_keywords") or [])
        extra = f" | 整句触发: {exact_str}" if exact_str else ""
        log("READY", f"机器人已就绪！触发关键词: {keywords_str}{extra} | Ctrl+C 退出")

        backoff = 1.0
        while not self.stop_event.is_set():
            since = int(self.state.get("last_rowid") or 0)
            cmd = [
                self.cfg["imsg_bin"],
                "watch",
                "--chat-id",
                str(chat_id),
                "--json",
                "--debounce",
                "250ms",
            ]
            if self.cfg.get("enable_images", True):
                cmd.extend(["--attachments", "--convert-attachments"])
            if since > 0:
                cmd.extend(["--since-rowid", str(since)])

            log("WATCH", f"启动监听: {' '.join(cmd)}")
            self.watch_proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=2 * 1024 * 1024,
            )

            async def _drain_watch_err(p: asyncio.subprocess.Process) -> None:
                assert p.stderr
                while True:
                    raw = await p.stderr.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", errors="replace").rstrip()
                    if line:
                        log("IMSG-ERR", line)

            err_task = asyncio.create_task(_drain_watch_err(self.watch_proc))
            assert self.watch_proc.stdout

            try:
                while not self.stop_event.is_set():
                    raw = await self.watch_proc.stdout.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        log("IMSG-RAW", line[:120])
                        continue
                    backoff = 1.0
                    await self._handle_message(msg)
            finally:
                err_task.cancel()
                if self.watch_proc and self.watch_proc.returncode is None:
                    self.watch_proc.terminate()
                    await self.watch_proc.wait()

            if not self.stop_event.is_set():
                log("WARN", f"imsg watch 断开，{backoff:.0f}s 后重连...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 15.0)

        self._flush_uptime()
        save_state(self.state)
        await self.agy.stop()

    def request_stop(self) -> None:
        self.stop_event.set()
        if self.watch_proc and self.watch_proc.returncode is None:
            self.watch_proc.terminate()


def list_chats(imsg_bin: str) -> None:
    proc = subprocess.run(
        [imsg_bin, "chats", "--limit", "20", "--json"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print("获取会话列表失败，请确认终端已授予「完全磁盘访问权限」：")
        print(proc.stderr)
        sys.exit(1)

    print(f"{'chat_id':<10} {'类型':<6} {'服务':<10} {'群名/对象'}")
    print("-" * 64)
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            c = json.loads(line)
        except Exception:
            continue
        kind = "群聊" if c.get("is_group") else "私聊"
        name = c.get("display_name") or c.get("name") or ",".join(c.get("participants", [])[:3])
        print(f"{str(c.get('id')):<10} {kind:<6} {str(c.get('service', '')):<10} {name}")


async def run_test(cfg: Dict[str, Any], prompt: str) -> None:
    worker = AgyStreamWorker(cfg)
    try:
        await worker.start()
        reply, meta = await worker.ask(prompt)
        print("\n=== 回复内容 ===")
        print(reply)
        print("================")
    finally:
        await worker.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="单群 iMessage -> Antigravity 常驻机器人")
    parser.add_argument("--list-chats", action="store_true", help="列出最近 iMessage 会话和 chat_id")
    parser.add_argument("--test", metavar="TEXT", help="不连 iMessage，直接测试常驻 agy 沙箱对话")
    args = parser.parse_args()

    cfg = load_config()
    if args.list_chats:
        list_chats(cfg["imsg_bin"])
        return

    if args.test:
        asyncio.run(run_test(cfg, args.test))
        return

    if cfg.get("chat_id") is None:
        print("尚未在 config.json 中填写目标群的 chat_id！先为你列出最近的会话：\n")
        list_chats(cfg["imsg_bin"])
        print(f"\n请将目标群聊的 chat_id 填入 {CONFIG_PATH} 后重新运行 ./start.sh")
        sys.exit(1)

    bridge = GroupChatBridge(cfg)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, bridge.request_stop)

    try:
        loop.run_until_complete(bridge.run())
    finally:
        loop.close()
        log("EXIT", "机器人已安全停止。")


if __name__ == "__main__":
    main()
