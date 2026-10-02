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
        trace = None
        want_profile = options.get("profile", False)
        if want_profile:
            prof = profiler_mod.Profiler()
            vm.profiler = prof
            if options.get("sample", True):
                prof.start_sampling(float(options.get("sample_interval_ms", 1.0)))
        if options.get("trace", False):
            trace = tracer_mod.ExecutionTracer(
                checkpoint_every=int(options.get("checkpoint_every",
                                                   config.TRACE_CHECKPOINT_EVERY)),
                max_events=int(options.get("max_trace_events",
                                           config.MAX_TRACE_EVENTS)))
            trace.attach(vm)
        if options.get("inputs"):
            vm.input_queue = list(options["inputs"])
        vm.start()
        vm.run()
        if trace is not None:
            trace.on_finish(vm)
        if prof:
            prof.attach_vm(vm)
            prof.stop_sampling()
            report = prof.report()
        else:
            report = None
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
            "trace_summary": trace.summary() if trace is not None else None,
        }
        return out

    # ==================================================================
    # 执行轨迹会话（内存驻留，口径与调试会话一致）
    # ==================================================================
    def trace_run(self, source, options=None):
        """编译 -> 带轨迹运行 -> 驻留 TraceSession，返回会话首屏数据。"""
        options = dict(options or {})
        options["trace"] = True
        result = compiler_mod.compile_source(source)
        if not result.success:
            return {
                "ok": False,
                "diagnostics": result.diagnostics.to_list(),
                "output": [],
                "stage": result.stage,
            }
        vm = vm_mod.VM(result.bytecode, result.source_lines)
        trace = tracer_mod.ExecutionTracer(
            checkpoint_every=int(options.get("checkpoint_every",
                                               config.TRACE_CHECKPOINT_EVERY)),
            max_events=int(options.get("max_trace_events",
                                       config.MAX_TRACE_EVENTS)))
        trace.attach(vm)
        if options.get("inputs"):
            vm.input_queue = list(options["inputs"])
        vm.start()
        vm.run()
        trace.on_finish(vm)

        sid = storage.new_id("trace")
        sess = TraceSession(sid, result, trace, source,
                            list(vm.output), vm.return_value,
                            vm.error.to_dict() if vm.error else None,
                            round(vm.elapsed_ms(), 3), vm.instruction_count)
        # 控制内存驻留数量
        if len(self.trace_sessions) >= config.MAX_TRACE_SESSIONS:
            old = sorted(self.trace_sessions.values(), key=lambda s: s.created_at)
            for s in old[:len(self.trace_sessions) - config.MAX_TRACE_SESSIONS + 1]:
                self.trace_sessions.pop(s.id, None)
        self.trace_sessions[sid] = sess
        return sess.first_page(sid)

    def trace_events(self, sid, offset=0, limit=200, kind=None, func=None):
        sess = self.trace_sessions.get(sid)
        if not sess:
            return {"ok": False, "error": "轨迹会话不存在或已过期"}
        return {"ok": True, "session_id": sid,
                **sess.events_page(offset, limit, kind, func)}

    def trace_state(self, sid, seq):
        sess = self.trace_sessions.get(sid)
        if not sess:
            return {"ok": False, "error": "轨迹会话不存在或已过期"}
        state = sess.state_at(int(seq))
        if state is None:
            return {"ok": False, "error": "轨迹为空"}
        state["session_id"] = sid
        return state

    def trace_summary(self, sid):
        sess = self.trace_sessions.get(sid)
        if not sess:
            return {"ok": False, "error": "轨迹会话不存在或已过期"}
        return {"ok": True, "session_id": sid, "summary": sess.tracer.summary()}

    def trace_sessions_list(self):
        return [{"session_id": s.id, "created_at": s.created_at,
                 "events": len(s.tracer.events),
                 "truncated": s.tracer.truncated}
                for s in self.trace_sessions.values()]

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


class TraceSession:
    """一次带轨迹的运行：持有编译产物与 ExecutionTracer，供分页查询/状态重建。"""

    def __init__(self, sid, result, trace, source, output, return_value,
                 error, elapsed_ms, instruction_count):
        self.id = sid
        self.result = result
        self.tracer = trace
        self.source = source
        self.output = output
        self.return_value = return_value
        self.error = error
        self.elapsed_ms = elapsed_ms
        self.instruction_count = instruction_count
        self.created_at = storage.now_iso()

    def events_page(self, offset, limit, kind, func):
        page = self.tracer.events_page(offset, limit, kind, func)
        page["summary"] = self.tracer.summary()
        page["source_lines"] = self.result.source_lines
        page["output"] = self.output
        page["error"] = self.error
        page["elapsed_ms"] = self.elapsed_ms
        return page

    def first_page(self, sid):
        data = {"ok": True, "session_id": sid,
                "source": self.source,
                "source_lines": self.result.source_lines,
                "output": self.output,
                "return_value": self.return_value,
                "error": self.error,
                "elapsed_ms": self.elapsed_ms,
                "instruction_count": self.instruction_count}
        data.update(self.events_page(0, 200, None, None))
        return data

    def state_at(self, seq):
        return self.tracer.state_at(
            self.result.bytecode, self.result.source_lines, seq)


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
