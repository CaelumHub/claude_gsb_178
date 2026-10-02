# -*- coding: utf-8 -*-
"""
业务服务层：把编译器、解释器、调试器、剖析器、存储组织成可供 HTTP 接口调用的能力。

职责划分：
  * 项目管理与历史版本（创建/列表/重命名/删除/保存版本/恢复/对比）；
  * 编译流水线（带产物缓存）；
  * 普通运行（可选性能剖析）；
  * 交互式调试会话（断点/单步/续跑，会话状态驻留内存，支持跨请求同步）；
  * 内存快照（运行/调试暂停时产出）。

所有持久化都走 storage 层的"文件锁 + 原子写"，保证频繁写入下的并发安全。
"""

import os
import shutil
import time
import difflib
import hashlib
from typing import Dict, List, Optional

from . import config
from . import storage
from . import compiler as compiler_mod
from . import vm as vm_mod
from . import debugger as debugger_mod
from . import profiler as profiler_mod
from . import tracer as tracer_mod
from . import diagnostics as diag
from . import memory_model


# ---------------------------------------------------------------------------
# 版本 / 运行记录结构
# ---------------------------------------------------------------------------
def _empty_project(name, source, language="minilang"):
    now = storage.now_iso()
    pid = storage.new_id("proj")
    meta = {
        "id": pid,
        "name": name,
        "language": language,
        "created_at": now,
        "updated_at": now,
        "version_count": 0,
        "last_source": source,
        "description": "",
    }
    return meta


def _empty_version(pid, source, message, compiled=None):
    now = storage.now_iso()
    vid = storage.new_id("ver")
    manifest = {
        "id": vid,
        "project_id": pid,
        "message": message or "保存版本",
        "created_at": now,
        "source_hash": _hash(source),
        "source_len": len(source),
        "line_count": source.count("\n"),
        "compiled_ok": bool(compiled and compiled.success),
        "error_count": len(compiled.diagnostics.errors()) if compiled else 0,
    }
    return manifest


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _diff_lines(a: str, b: str) -> List[dict]:
    """行级 diff，返回结构化结果供前端渲染。"""
    al = a.splitlines()
    bl = b.splitlines()
    sm = difflib.SequenceMatcher(None, al, bl, autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        out.append({
            "op": tag,  # insert / delete / replace
            "old_start": i1 + 1, "old_end": i2,   # 1-based 行号
            "new_start": j1 + 1, "new_end": j2,
            "old_lines": al[i1:i2],
            "new_lines": bl[j1:j2],
        })
    return out


# ---------------------------------------------------------------------------
# 服务主体
# ---------------------------------------------------------------------------
class Service:
    def __init__(self):
        config.ensure_dirs()
        self.debug_sessions: Dict[str, "DebugSession"] = {}
        self.trace_sessions: Dict[str, "TraceSession"] = {}
        self._session_counter = 0

    # ==================================================================
    # 项目管理
    # ==================================================================
    def list_projects(self):
        out = []
        if not os.path.isdir(config.PROJECTS_DIR):
            return out
        for name in os.listdir(config.PROJECTS_DIR):
            meta_path = os.path.join(config.PROJECTS_DIR, name, "meta.json")
            meta = storage.read_json(meta_path)
            if meta:
                out.append(meta)
        out.sort(key=lambda m: m.get("updated_at", ""), reverse=True)
        return out

    def create_project(self, name, source, language="minilang"):
        name = (name or "").strip() or "未命名项目"
        meta = _empty_project(name, source, language)
        d = storage.project_dir(meta["id"])
        os.makedirs(d, exist_ok=True)
        storage.write_json(os.path.join(d, "meta.json"), meta)
        # 初始版本
        self.save_version(meta["id"], source, "初始版本")
        return self.get_project(meta["id"])

    def get_project(self, pid):
        meta = storage.read_json(os.path.join(storage.project_dir(pid), "meta.json"))
        return meta

    def update_project(self, pid, fields):
        meta_path = os.path.join(storage.project_dir(pid), "meta.json")
        def _mut(data):
            if data is None:
                return None, False
            for k, v in fields.items():
                if k in ("name", "description", "last_source", "language"):
                    data[k] = v
            data["updated_at"] = storage.now_iso()
            return data, True
        new_meta, _ = storage.update_json(meta_path, _mut)
        return new_meta

    def delete_project(self, pid):
        d = storage.project_dir(pid)
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
        # 清理其调试会话
        for sid in [s for s, sess in self.debug_sessions.items() if sess.project_id == pid]:
            self.debug_sessions.pop(sid, None)
        return True

    # ==================================================================
    # 历史版本
    # ==================================================================
    def list_versions(self, pid):
        vdir = storage.versions_dir(pid)
        out = []
        if not os.path.isdir(vdir):
            return out
        for name in os.listdir(vdir):
            mp = os.path.join(vdir, name, "manifest.json")
            m = storage.read_json(mp)
            if m:
                out.append(m)
        out.sort(key=lambda m: m.get("created_at", ""), reverse=True)
        return out

    def save_version(self, pid, source, message=""):
        compiled = compiler_mod.compile_source(source)
        meta_path = os.path.join(storage.project_dir(pid), "meta.json")
        manifest = _empty_version(pid, source, message, compiled)
        vdir = storage.version_dir(pid, manifest["id"])
        os.makedirs(vdir, exist_ok=True)
        storage.write_json(os.path.join(vdir, "manifest.json"), manifest)
        storage.ensure_text(os.path.join(vdir, "source.txt"), source)
        storage.write_json(os.path.join(vdir, "compile.json"), {
            "diagnostics": compiled.diagnostics.to_list(),
            "success": compiled.success,
            "stage": compiled.stage,
        })
        storage.write_json(os.path.join(vdir, "run.json"), [])
        # 更新项目 meta
        def _mut(data):
            if data is None:
                return None, False
            data["version_count"] = int(data.get("version_count", 0)) + 1
            data["updated_at"] = storage.now_iso()
            data["last_source"] = source
            return data, True
        storage.update_json(meta_path, _mut)
        return manifest

    def get_version(self, pid, vid):
        vdir = storage.version_dir(pid, vid)
        manifest = storage.read_json(os.path.join(vdir, "manifest.json"))
        if not manifest:
            return None
        source = ""
        src_path = os.path.join(vdir, "source.txt")
        if os.path.exists(src_path):
            with open(src_path, "r", encoding="utf-8") as f:
                source = f.read()
        runs = storage.read_json(os.path.join(vdir, "run.json"), [])
        comp = storage.read_json(os.path.join(vdir, "compile.json"), {})
        return {"manifest": manifest, "source": source, "runs": runs, "compile": comp}

    def restore_version(self, pid, vid):
        ver = self.get_version(pid, vid)
        if not ver:
            return None
        src = ver["source"]
        msg = f"恢复到版本 {vid[:6]}"
        self.update_project(pid, {"last_source": src})
        return self.save_version(pid, src, msg)

    def diff_versions(self, pid, va, vb):
        a = self.get_version(pid, va)
        b = self.get_version(pid, vb)
        if not a or not b:
            return None
        return _diff_lines(a["source"], b["source"])

    # ==================================================================
    # 编译
    # ==================================================================
    def compile(self, source, pid=None, vid=None):
        result = compiler_mod.compile_source(source)
        if pid and vid:
            vdir = storage.version_dir(pid, vid)
            storage.write_json(os.path.join(vdir, "compile.json"), {
                "diagnostics": result.diagnostics.to_list(),
                "success": result.success,
                "stage": result.stage,
            })
        return result

    def compile_view(self, source, detail="all"):
        """供前端页面渲染的编译结果（token/ast/symbols/bytecode 按需返回）。"""
        result = compiler_mod.compile_source(source)
        view = result.to_dict()
        if "tokens" in detail or detail == "all":
            view["tokens"] = [{"type": t.type, "text": t.text, "line": t.line,
                               "column": t.column, "pos": t.pos} for t in result.tokens]
        if detail == "all" and result.ast is not None:
            view["ast"] = result.ast.to_dict()
        if detail == "all" and result.symbol_table is not None:
            view["symbols"] = result.symbol_table.to_dict()
        if detail == "all" and result.bytecode is not None:
            view["bytecode"] = result.bytecode.to_dict()
            view["bytecode"]["functions"].reverse()
        return view

    # ==================================================================
    # 运行（普通 / 性能剖析）
    # ==================================================================
    def run(self, source, options=None):
        options = options or {}
        result = compiler_mod.compile_source(source)
        if not result.success:
            return {
                "ok": False,
                "diagnostics": result.diagnostics.to_list(),
                "output": [],
                "stage": result.stage,
            }
        vm = vm_mod.VM(result.bytecode, result.source_lines)
        prof = None
        tracer = None
        want_profile = options.get("profile", False)
        if want_profile:
            prof = profiler_mod.Profiler()
            vm.profiler = prof
            if options.get("sample", True):
                prof.start_sampling(float(options.get("sample_interval_ms", 1.0)))
        # 执行轨迹：与剖析共用同一批 VM 钩子，口径一致、可同时开启
        trace_id = None
        if options.get("trace"):
            tracer = tracer_mod.Tracer(
                mode="record",
                granularity=options.get("trace_granularity", "line"),
                max_events=options.get("trace_max_events", tracer_mod.DEFAULT_MAX_EVENTS))
            vm.tracer = tracer
        if options.get("inputs"):
            vm.input_queue = list(options["inputs"])
        vm.start()
        if tracer is not None:
            # 初始检查点：程序入口、任何指令执行前（seq = -1）
            tracer.add_checkpoint(vm, -1)
        try:
            vm.run()
        except tracer_mod.TraceLimitReached:
            pass  # 轨迹截断；已录部分有效
        if prof:
            prof.attach_vm(vm)
            prof.stop_sampling()
            report = prof.report()
        else:
            report = None
        if tracer is not None:
            trace_id = self._persist_trace(source, options, result.source_lines,
                                           vm, tracer)
        heap = vm.heap.snapshot([]) if options.get("memory", False) else None
        out = {
            "ok": True,
            "output": list(vm.output),
            "return_value": vm.return_value,
            "error": vm.error.to_dict() if vm.error else None,
            "instruction_count": vm.instruction_count,
            "elapsed_ms": round(vm.elapsed_ms(), 3),
            "profile": report,
            "memory": heap,
        }
        if tracer is not None:
            out["trace"] = self.trace_summary(trace_id)
        return out

    def record_run(self, pid, vid, source, options=None):
        """运行并持久化运行记录到版本目录。"""
        options = options or {}
        started = time.time()
        out = self.run(source, options)
        rec = {
            "id": storage.new_id("run"),
            "timestamp": storage.now_iso(),
            "elapsed_ms": out.get("elapsed_ms", 0.0),
            "ok": out.get("ok", False),
            "output": out.get("output", []),
            "return_value": out.get("return_value"),
            "instruction_count": out.get("instruction_count", 0),
            "error": out.get("error"),
            "profiled": bool(out.get("profile")),
        }
        # 若开启了轨迹录制，运行记录里带上轨迹 id，口径与 profile 一致
        if isinstance(out.get("trace"), dict):
            rec["trace_id"] = out["trace"].get("id")
            rec["trace_event_count"] = out["trace"].get("event_count", 0)
        if pid and vid:
            vdir = storage.version_dir(pid, vid)
            run_path = os.path.join(vdir, "run.json")
            def _mut(data):
                data = data if isinstance(data, list) else []
                data.append(rec)
                if len(data) > 200:
                    data = data[-200:]
                return data, True
            storage.update_json(run_path, _mut)
        # 全局运行记录索引
        global_rec = dict(rec)
        global_rec["project_id"] = pid
        global_rec["version_id"] = vid
        storage.write_json(os.path.join(config.RUNS_DIR, rec["id"] + ".json"), global_rec)
        return rec

    # ==================================================================
    # 调试会话
    # ==================================================================
    def _new_session_id(self):
        self._session_counter += 1
        return f"dbg-{self._session_counter:x}-{int(time.time()*1000)%100000:x}"

    def debug_start(self, source, breakpoints=None, pid=None, vid=None):
        """创建并启动一个调试会话（编译 -> 建 VM -> 建调试器 -> 启动）。"""
        sid = self._new_session_id()
        sess = DebugSession(sid, source, [b + 1 for b in (breakpoints or [])], pid, vid)
        self.debug_sessions[sid] = sess
        sess.start()
        return self.debug_state(sid)

    def debug_state(self, sid):
        sess = self.debug_sessions.get(sid)
        if not sess:
            return {"ok": False, "error": "会话不存在或已过期", "session_id": sid}
        return sess.state()

    def debug_command(self, sid, command, breakpoints=None):
        sess = self.debug_sessions.get(sid)
        if not sess:
            return {"ok": False, "error": "会话不存在或已过期"}
        if breakpoints is not None:
            sess.set_breakpoints(breakpoints)
        getattr(sess, command)()
        return self.debug_state(sid)

    def debug_stop(self, sid):
        sess = self.debug_sessions.pop(sid, None)
        return {"ok": True, "removed": bool(sess)}

    def debug_sessions_list(self):
        return [{"session_id": s.id, "project_id": s.project_id,
                 "started": s.started, "finished": s.vm.finished if s.vm else False}
                for s in self.debug_sessions.values()]

    def memory_snapshot(self, sid):
        sess = self.debug_sessions.get(sid)
        if not sess or not sess.vm:
            return None
        return sess.vm.heap.snapshot(sess.vm.frame_snapshot())

    # ==================================================================
    # 执行轨迹
    # ==================================================================
    def _trace_path(self, tid):
        return os.path.join(config.TRACES_DIR, tid + ".json")

    def _trace_events_path(self, tid):
        return os.path.join(config.TRACES_DIR, tid + ".events.json")

    def _persist_trace(self, source, options, source_lines, vm, tracer):
        """录制结束：轨迹摘要 / 事件分文件落盘并驻留内存，返回轨迹 id。

        摘要（不含事件）单独存一份，使"最近轨迹"列表无需解析可达数 MB 的
        事件数组；事件写在 .events.json，仅在打开该轨迹时读取。
        """
        tid = storage.new_id("trace")
        summary = {
            "id": tid,
            "timestamp": storage.now_iso(),
            "source": source,
            "source_lines": source_lines,
            "inputs": list(vm.input_queue),
            "granularity": tracer.granularity,
            "max_events": tracer.max_events,
            "truncated": tracer.truncated,
            "finished": vm.finished,
            "event_count": len(tracer.events),
            "type_counts": tracer.type_counts(),
            "instruction_count": vm.instruction_count,
            "elapsed_ms": round(vm.elapsed_ms(), 3),
            "output": list(vm.output),
            "return_value": _serialize_plain(vm.return_value),
            "error": vm.error.to_dict() if vm.error else None,
        }
        sess = TraceSession(tid, dict(summary, events=tracer.events),
                            checkpoints=tracer.checkpoints)
        self.trace_sessions[tid] = sess
        storage.write_json(self._trace_path(tid), summary)
        storage.write_json(self._trace_events_path(tid), tracer.events)
        self._prune_trace_files()
        return tid

    def _prune_trace_files(self):
        """只保留最近的若干条轨迹（摘要与事件文件成对清理）。"""
        try:
            files = [f for f in os.listdir(config.TRACES_DIR)
                     if f.startswith("trace-") and f.endswith(".json")
                     and not f.endswith(".events.json")]
        except OSError:
            return
        if len(files) <= config.TRACE_KEEP_FILES:
            return
        full = [os.path.join(config.TRACES_DIR, f) for f in files]
        full.sort(key=lambda p, st=os.stat: st(p).st_mtime, reverse=True)
        for old in full[config.TRACE_KEEP_FILES:]:
            tid = os.path.basename(old)[:-5]
            for p in (old, self._trace_events_path(tid)):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def trace_start(self, source, options=None):
        """运行并录制轨迹（/api/trace/start）：返回摘要 + 全部事件 + 源码。"""
        options = dict(options or {})
        options["trace"] = True
        out = self.run(source, options)
        if not out.get("ok"):
            return out
        return out["trace"]

    def _load_trace(self, tid):
        sess = self.trace_sessions.get(tid)
        if sess is not None:
            return sess
        summary = storage.read_json(self._trace_path(tid))
        if not summary:
            return None
        # 事件按需从独立文件读取（列表接口不触发）
        events = storage.read_json(self._trace_events_path(tid), [])
        sess = TraceSession(tid, dict(summary, events=events))
        self.trace_sessions[tid] = sess
        return sess

    def trace_summary(self, tid):
        sess = self._load_trace(tid)
        if not sess:
            return None
        return sess.summary(include_events=True)

    def trace_state_at(self, tid, seq):
        """回放轨迹到第 seq 步，返回当时的执行位置 / 变量 / 调用栈。"""
        sess = self._load_trace(tid)
        if not sess:
            return None
        return sess.state_at(seq)

    def trace_delete(self, tid):
        self.trace_sessions.pop(tid, None)
        for path in (self._trace_path(tid), self._trace_events_path(tid)):
            if os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
        return True

    def trace_list(self):
        out = []
        seen = set()
        for tid, sess in self.trace_sessions.items():
            out.append(sess.summary(include_events=False))
            seen.add(tid)
        if os.path.isdir(config.TRACES_DIR):
            for f in os.listdir(config.TRACES_DIR):
                # 摘要文件：trace-*.json（排除事件文件 .events.json）
                if not (f.startswith("trace-") and f.endswith(".json")
                        and not f.endswith(".events.json")):
                    continue
                tid = f[:-5]
                if tid in seen:
                    continue
                summ = storage.read_json(os.path.join(config.TRACES_DIR, f))
                if summ:
                    # 只放精简视图：不解析可能很大的事件文件
                    out.append(TraceSession(tid, summ).summary(include_events=False))
        out.sort(key=lambda s: s.get("timestamp", ""), reverse=True)
        return out


def _serialize_plain(v):
    from . import runtime as rt
    if v is None:
        return None
    return rt.serialize_value(v)


class DebugSession:
    """一个交互式调试会话：持有 VM 与 Debugger，状态跨 HTTP 请求保留。"""

    def __init__(self, sid, source, breakpoints, pid=None, vid=None):
        self.id = sid
        self.source = source
        self.breakpoints = set(breakpoints)
        self.project_id = pid
        self.version_id = vid
        self.result = compiler_mod.compile_source(source)
        self.vm: Optional[vm_mod.VM] = None
        self.debugger: Optional[debugger_mod.Debugger] = None
        self.started = False

    def set_breakpoints(self, lines):
        self.breakpoints = set(lines)
        if self.debugger:
            self.debugger.set_breakpoints(list(self.breakpoints))

    def start(self):
        if not self.result.success:
            self.started = False
            return
        self.vm = vm_mod.VM(self.result.bytecode, self.result.source_lines)
        self.debugger = debugger_mod.Debugger(self.vm, self.breakpoints)
        self.debugger.start()
        self.started = True

    def state(self):
        if not self.result.success:
            return {
                "ok": False,
                "compile_failed": True,
                "diagnostics": self.result.diagnostics.to_list(),
                "source_lines": self.result.source_lines,
            }
        if not self.started or self.vm is None:
            return {"ok": True, "not_started": True, "diagnostics": self.result.diagnostics.to_list()}
        snap = self.debugger.snapshot()
        snap["ok"] = True
        snap["session_id"] = self.id
        snap["source_lines"] = self.result.source_lines
        snap["source"] = self.source
        snap["diagnostics"] = self.result.diagnostics.to_list()
        snap["breakpoint_instructions"] = self._breakpoint_hits()
        snap["memory"] = self.vm.heap.snapshot(self.vm.frame_snapshot())
        return snap

    def _breakpoint_hits(self):
        """返回每个函数里命中断点的指令偏移（供前端高亮字节码）。"""
        out = []
        for name, fc in self.result.bytecode.functions.items():
            for ins in fc.instructions:
                if ins.line in self.breakpoints:
                    out.append({"function": name, "offset": ins.offset, "line": ins.line})
        main = self.result.bytecode.main
        if main:
            for ins in main.instructions:
                if ins.line in self.breakpoints:
                    out.append({"function": "<main>", "offset": ins.offset, "line": ins.line})
        return out

    # ---- 命令分发 ----
    def continue_(self):
        if self.debugger:
            self.debugger.continue_()

    def step_instruction(self):
        if self.debugger:
            self.debugger.step_instruction()

    def step_into(self):
        if self.debugger:
            self.debugger.step_into()

    def step_over(self):
        if self.debugger:
            self.debugger.step_over()

    def step_out(self):
        if self.debugger:
            self.debugger.step_out()


class TraceSession:
    """一次执行轨迹：事件流（持久化）+ 稀疏检查点（仅内存，按需重建）。

    查看任意一步时，从最近检查点恢复一个新 VM，以 replay 模式确定性重放到目标
    步骤；重放途中 time/random 注入录制值，input 走录制时的输入队列。
    """

    def __init__(self, tid, data, checkpoints=None):
        self.id = tid
        self.data = data
        self.events = data.get("events", [])
        # seq -> 深克隆状态；只在内存，重启后按需重放重建
        self.checkpoints: Dict[int, dict] = checkpoints or {}

    def summary(self, include_events=True):
        d = {k: v for k, v in self.data.items() if k != "events"}
        d["id"] = self.id
        if include_events:
            d["events"] = self.events
        return d

    def state_at(self, seq):
        seq = max(-1, min(int(seq), len(self.events) - 1))
        cp_seq, cp = self._nearest_checkpoint(seq)
        # 初始状态（程序入口，任何指令执行前）
        if seq < 0:
            vm = self._fresh_vm()
            vm.start()
            tracer_mod.restore_state(vm, cp)
            return tracer_mod.snapshot_at(vm, None, -1)

        ev = self.events[seq]
        vm = self._fresh_vm()
        vm.start()
        tracer_mod.restore_state(vm, cp)
        # 检查点捕获在干净的指令边界，帧状态就在克隆里；行去重状态从事件流重建。
        tr = tracer_mod.Tracer(
            mode="replay",
            granularity=self.data.get("granularity", "line"),
            stop_seq=seq,
            builtin_replays=self._nondeterministic_map())
        # 复用检查点存储，首次深回放后后续查看即变快
        tr.checkpoints = self.checkpoints
        self._cp_obj_count_sync()
        tr._cp_obj_count = dict(self._cp_obj_count)
        vm.tracer = tr
        tr.seq = cp_seq
        line_keys, last_off = self._dedup_state_at(cp_seq) if cp_seq >= 0 else ({}, {})
        tr._line_keys = line_keys
        tr._last_off = last_off
        vm.paused = False
        vm.pause_reason = None
        # 目标恰为检查点自身时不再续跑；否则由 TraceStop 在目标事件处中止
        vm.run(pause_fn=tr.should_pause)
        return tracer_mod.snapshot_at(vm, ev, seq)

    def _nondeterministic_map(self):
        """从事件流提取 time/random 录制值（调用序数 -> 原始值）。"""
        out = {}
        for ev in self.events:
            if (len(ev) > tracer_mod.I_NONDET
                    and ev[tracer_mod.I_TYPE] == tracer_mod.EV_CALL
                    and ev[tracer_mod.I_KIND] == "builtin"
                    and ev[tracer_mod.I_CALLEE] in ("time", "random")
                    and ev[tracer_mod.I_NONDET] is not None
                    and ev[tracer_mod.I_RESULT] is not None):
                out[ev[tracer_mod.I_NONDET]] = tracer_mod.deserialize_primitive(
                    ev[tracer_mod.I_RESULT])
        return out

    def _nearest_checkpoint(self, seq):
        """返回 <= seq 的最近检查点；没有则从程序入口（seq=-1）重放。"""
        if -1 not in self.checkpoints:
            vm0 = self._fresh_vm()
            vm0.start()
            self.checkpoints[-1] = tracer_mod.capture_state(vm0)
            self._cp_obj_count_sync()
        avail = [s for s in self.checkpoints if s <= seq]
        s = max(avail)
        return s, self.checkpoints[s]

    def _cp_obj_count_sync(self):
        self._cp_obj_count = {s: len(st.get("objects", {}))
                              for s, st in self.checkpoints.items()}

    def _dedup_state_at(self, cp_seq):
        """从事件流重建检查点边界（事件 cp_seq 已完成）处的行去重状态。

        完整模拟 tracer.on_instruction 的转移：line/insn 事件更新行键，
        相邻指令偏移回退视作循环回边并清键；enter/return 维护帧深度。
        """
        line_keys, last_off = {}, {}
        prev_off = {}       # depth -> (func, offset)
        for ev in self.events[:cp_seq + 1]:
            t = ev[tracer_mod.I_TYPE]
            depth, func, line = (ev[tracer_mod.I_DEPTH], ev[tracer_mod.I_FUNC],
                                 ev[tracer_mod.I_LINE])
            if t == tracer_mod.EV_RETURN:
                line_keys.pop(depth, None)
                last_off.pop(depth, None)
                prev_off.pop(depth, None)
                continue
            if t not in (tracer_mod.EV_LINE, tracer_mod.EV_INSN):
                continue
            off = ev[tracer_mod.I_OFF]
            po = prev_off.get(depth)
            if po is not None and po[0] == func and off < po[1]:
                line_keys.pop(depth, None)
            prev_off[depth] = (func, off)
            last_off[depth] = (func, off)
            line_keys[depth] = (func, line)
        return line_keys, last_off

    def _fresh_vm(self):
        result = compiler_mod.compile_source(self.data["source"])
        if not result.success:
            raise ValueError("轨迹源码无法重新编译")
        vm = vm_mod.VM(result.bytecode, result.source_lines)
        if self.data.get("inputs"):
            vm.input_queue = list(self.data["inputs"])
        return vm
