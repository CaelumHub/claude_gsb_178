# -*- coding: utf-8 -*-
"""
执行轨迹（Execution Trace）记录与回放。

在一次普通运行结束后，把"程序都执行了哪些步骤"沉淀为可步进回放的事件流，
并支持在任意一步重建当时的执行位置、变量与调用栈。它与调试器、剖析器挂在
VM 的同一批回调点上，因此三者的记录口径天然一致：

  * 行（line）   —— 与调试器 step_into 相同的"源码行"粒度：顺序执行时源行
                    变化才记录，而循环回边（JUMP 跳回已访问过的同一行）会重新
                    记一步，因此循环的每一轮、递归的每一层都不会丢；
                    CALL/RETURN 等结构性指令不重复记行。
  * 调用（call） —— 与剖析器 function_enter 同一点：用户函数帧压入后记录，
                    携带实参；此时光标位于"被调用函数入口"，可看到形参已就位。
  * 返回（return）—— 与剖析器 function_exit 同一点：帧弹出、返回值已压入
                    调用方操作数栈后记录。
  * 内置（builtin）—— print/len/push 等内置调用一次一条，携带实参与返回值。
  * 开始 / 出错  —— 程序入口、运行期错误（错误事件携带结构化诊断信息）。

规模控制（"覆盖循环与递归产生的大量步骤而不压垮页面"）：
  * 事件有上限（默认 config.MAX_TRACE_EVENTS），超出后停止记录并置 truncated，
    程序本身继续跑完；
  * 不逐步保存变量快照，而是周期性保存 VM **检查点**（checkpoint）。查看第 N
    步时，取不晚于 N 的最近检查点恢复进一个新 VM，再确定性重放到 N。检查点
    只深拷贝会被原地修改的列表，字符串/函数/常量共享，内存开销很小；
  * 前端按页拉取事件（默认每页 200 条），数万条事件也不会一次性压进 DOM。

重放得到的状态字段与调试器快照（Debugger.snapshot）保持同一套键名，
"执行轨迹回放"与"单步调试"两页可以直接复用同一份渲染逻辑。
"""

from typing import Any, Dict, List, Optional

from . import bytecode as bc
from . import runtime as rt
from . import memory_model


# 事件种类
EV_START = "start"
EV_LINE = "line"
EV_CALL = "call"
EV_RETURN = "return"
EV_BUILTIN = "builtin"
EV_ERROR = "error"

# 在"指令执行前"发射、且重放时应停在该指令执行之前的事件。
# （start 只在 vm.start() 里发射一次，重放从检查点恢复、不会再次经过，故不在此列）
_PRE_INSTRUCTION_KINDS = (EV_LINE, EV_ERROR)

# 这些指令由结构性事件（call/return/builtin）表达，不再重复产生行事件
_NO_LINE_OPS = {
    bc.OP_CALL, bc.OP_RETURN, bc.OP_RETURN_NONE, bc.OP_LINE, bc.OP_NOP,
}


class ExecutionTracer:
    """一次运行的执行轨迹记录器（通过 ``vm.tracer`` 被 VM 回调）。"""

    def __init__(self, checkpoint_every=512, max_events=20000,
                 mode="record", target: Optional[int] = None):
        self.checkpoint_every = checkpoint_every
        self.max_events = max_events
        # record：正式记录（带检查点）；replay：重放计数，只数事件不存检查点
        self.mode = mode
        self.target = target

        self.events: List[Dict[str, Any]] = []
        self.checkpoints: List[Dict[str, Any]] = []
        # 与 vm.frames 对齐的轨迹帧栈：[{name, last_line, last_ip}]
        self._frames: List[Dict[str, Any]] = []
        self._seq = 0
        self.disabled = False          # 事件超上限后置 True
        self.truncated = False
        self.abort_step = False        # 重放命中"执行前"目标时，让 VM 跳过本条指令
        self.at_target = False
        self.vm = None

        # 汇总信息
        self.max_depth = 0
        self._counts: Dict[str, int] = {
            EV_LINE: 0, EV_CALL: 0, EV_RETURN: 0, EV_BUILTIN: 0,
            EV_START: 0, EV_ERROR: 0,
        }
        # name -> {"calls": int, "lines": {line: 行事件数}}
        self._functions: Dict[str, Dict[str, Any]] = {}
        self.instruction_count = 0
        self.error: Optional[Any] = None
        self._next_checkpoint = checkpoint_every
        self._cursor_depth = 0

    def attach(self, vm):
        self.vm = vm
        vm.tracer = self

    # ------------------------------------------------------------------
    # VM 回调
    # ------------------------------------------------------------------
    def on_program_start(self, vm):
        """main 帧建立后、首条指令前。"""
        self._frames = [{"name": "<main>", "last_line": 0, "last_ip": -1}]
        self._function_slot("<main>")
        # 0 号检查点：入口状态（首条指令执行前），与调试器"入口暂停"同义
        if self.mode == "record":
            self.checkpoints.append(
                _capture_checkpoint(0, vm, self._frames, ready=True))
        self._emit(vm, EV_START, frame=vm.frames[-1], line=1)

    def before_instruction(self, vm, frame, ins):
        """每条指令执行前（与调试器 should_pause、剖析器 record_instruction 同点）。"""
        if self.disabled:
            return
        # 先对齐规范帧栈（上个 CALL/RETURN 已在 dispatch 中改变过真实帧栈）。
        # 行事件的光标深度以对齐后的帧栈为准——这与重放恢复检查点后逐指令
        # 重建出的帧栈完全一致，保证记录与回放看到相同深度。
        self._sync_frames(vm)
        self._cursor_depth = max(0, len(self._frames) - 1)
        rec = self._frames[-1]
        # 无条件跳转 JUMP 只用于循环回边 / 跳出循环，一定改变控制流；
        # 条件跳转（JUMP_IF_*）不打标记——它们多是 if/while 判断，顺序执行时
        # 不应凭空多出步骤，回边本身由无条件 JUMP 覆盖。
        if ins.op == bc.OP_JUMP:
            rec["jumped"] = True
            rec["last_ip"] = ins.offset
            return
        if ins.op in _NO_LINE_OPS:
            rec["last_ip"] = ins.offset
            return
        line_changed = rec["last_line"] != ins.line
        jumped = rec.get("jumped", False)
        if not line_changed and not jumped:
            rec["last_ip"] = ins.offset
            return
        rec["last_line"] = ins.line
        slot = self._function_slot(frame.func_name)
        slot["lines"][ins.line] = slot["lines"].get(ins.line, 0) + 1
        rec["last_ip"] = ins.offset
        rec["jumped"] = False
        # 周期检查点：取在"行事件即将发射、该源行第一条指令尚未执行"的光标处。
        # 这正是调试器断点/单步暂停位置，也是前端点开该行步骤时要展示的状态。
        # 此时 ip 还停在该指令上（VM 在分发前才自增 ip），显式恢复 ready=True。
        if self.mode == "record" and self._seq >= self._next_checkpoint:
            self.checkpoints.append(
                _capture_checkpoint(self._seq, vm, self._frames, ready=True))
            while self._seq >= self._next_checkpoint:
                self._next_checkpoint += self.checkpoint_every
        self._emit(vm, EV_LINE, frame=frame, line=ins.line, depth=self._cursor_depth)

    def after_instruction(self, vm, frame, ins):
        """每条指令成功执行后的回调（检查点只在行边界前态落盘，这里无需做事）。"""
        return

    def _sync_frames(self, vm):
        """把轨迹帧栈对齐到真实 VM 帧栈（处理上一条 CALL/RETURN 造成的增减）。"""
        # 帧变深：上个指令执行压入了用户函数帧。新帧尚未执行任何指令，故
        # last_line=0 / last_ip=-1——它在第一条源行指令前会产生一个 line 事件，
        # 与"首次记录"路径完全一致。
        while len(self._frames) < len(vm.frames):
            nf = vm.frames[len(self._frames)]
            self._frames.append({"name": nf.func_name, "last_line": 0,
                                 "last_ip": -1, "jumped": False})
        # 帧变浅：上个指令返回到了调用方（return 事件可能已弹过一次）
        while len(self._frames) > len(vm.frames) and len(self._frames) > 1:
            self._frames.pop()

    def on_call_user(self, vm, caller, new_frame, func, args, ins):
        """用户函数帧压入后（剖析器 function_enter 同点）。帧栈由 _sync_frames 对齐。"""
        if self.disabled:
            return
        self.max_depth = max(self.max_depth, len(vm.frames) - 1)
        slot = self._function_slot(func.name)
        slot["calls"] += 1
        self._emit(vm, EV_CALL, frame=vm.frames[-1], line=ins.line,
                   callee=func.name,
                   args=[rt.serialize_value(a) for a in args])

    def on_return(self, vm, returned_frame, value, is_main):
        """帧弹出前后：非 main 在弹出后记录（返回值已到调用方栈顶）。"""
        if self.disabled:
            return
        name = returned_frame.func_name
        line = returned_frame.current_line or 0
        frame = vm.frames[-1] if vm.frames else returned_frame
        self._emit(vm, EV_RETURN, frame=frame, line=line,
                   callee=name,
                   result=rt.serialize_value(value))

    def on_builtin_return(self, vm, frame, ins, callee, args, result):
        """内置函数成功返回后。"""
        if self.disabled:
            return
        self._emit(vm, EV_BUILTIN, frame=frame, line=ins.line,
                   callee=callee.name,
                   args=[rt.serialize_value(a) for a in args][:10],
                   result=rt.serialize_value(result))

    def on_error(self, vm, diagnostic):
        """运行期错误（结构化诊断）。"""
        if self.disabled:
            return
        self.error = diagnostic
        self._emit(vm, EV_ERROR, frame=(vm.frames[-1] if vm.frames else None),
                   line=getattr(diagnostic, "line", 0) or 0,
                   message=diagnostic.message)

    def on_exit(self, vm):
        """exit() 内置触发的程序退出（SystemExitSignal 在 VM 中被捕获后回调）。"""
        if self.disabled:
            return
        frame = vm.frames[-1] if vm.frames else None
        line = frame.current_line if frame is not None else 0
        self._emit(vm, EV_RETURN, frame=frame, line=line,
                   callee="<exit>",
                   result=rt.serialize_value(vm.return_value))

    def on_finish(self, vm):
        """运行结束（service 层在 vm.run() 返回后调用）：汇总指令数。"""
        self.instruction_count = vm.instruction_count

    # ------------------------------------------------------------------
    # 事件发射
    # ------------------------------------------------------------------
    def _emit(self, vm, kind, frame=None, line=0, callee=None, depth=None,
              args=None, result=None, message=None):
        if self.disabled:
            return
        seq = self._seq
        # 深度以光标所在的真实 VM 帧栈为准（与调试器 call_stack 对齐）；
        # 已无 VM 帧可依时（main 返回）退回到轨迹帧栈。
        if depth is None:
            n_vm = len(vm.frames) if vm is not None else 0
            depth = max(0, n_vm - 1) if n_vm else max(0, len(self._frames) - 1)
        ev: Dict[str, Any] = {
            "seq": seq,
            "kind": kind,
            "func": frame.func_name if frame is not None else (callee or "<main>"),
            "line": line or 0,
            "depth": depth if depth is not None else max(0, len(self._frames) - 1),
        }
        if callee is not None:
            ev["callee"] = callee
        if args is not None:
            ev["args"] = args
        if result is not None:
            ev["result"] = result
        if message is not None:
            ev["message"] = message

        self.events.append(ev)
        self._seq += 1
        self._counts[kind] += 1

        if self.mode == "replay" and self.target is not None:
            # 命中目标即停。对"执行前"事件（line/error），光标本应在该指令
            # 执行之前，故令 VM 跳过本次分发；对结构性事件（call/return/builtin），
            # 回调本身就在指令分发之后触发，此刻 VM 状态已是该事件的光标处。
            if seq == self.target:
                self.at_target = True
                if kind in _PRE_INSTRUCTION_KINDS:
                    self.abort_step = True

        # 达到上限：记录截断标记并停用后续回调（仅记录模式；重放需要继续计数）。
        # 程序继续跑完，轨迹截断不影响运行结果本身。
        if self.mode == "record" and self._seq >= self.max_events and not self.truncated:
            self.truncated = True
            self.disabled = True
            if self.vm is not None:
                self.vm.tracer = None

    def _function_slot(self, name) -> Dict[str, Any]:
        slot = self._functions.get(name)
        if slot is None:
            slot = {"calls": 0, "lines": {}}
            self._functions[name] = slot
        return slot

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def summary(self) -> Dict[str, Any]:
        # hot_lines 与剖析器 Profiler.report() 里的同名字段同构（{"line","hits"}），
        # 但口径不同需注意：剖析器按"指令条数"计 hits（热点行=指令执行最多次的
        # 源行），轨迹这里按"源行步骤数"计（该行在回放列表里出现多少次）。
        # 两者共用 VM 的同一回调点、同一函数名/行号，前端可并列对照。
        functions = []
        for name, data in self._functions.items():
            hot = sorted(data["lines"].items(), key=lambda kv: -kv[1])[:10]
            functions.append({
                "name": name,
                "calls": data["calls"],
                "line_steps": sum(data["lines"].values()),
                # 与剖析器 hot_lines 同构：{"line", "hits"}
                "hot_lines": [{"line": ln, "hits": h} for ln, h in hot],
            })
        functions.sort(key=lambda f: -f["line_steps"])
        return {
            "total_events": len(self.events),
            "truncated": self.truncated,
            "max_events": self.max_events,
            "instruction_count": self.instruction_count,
            "max_depth": self.max_depth,
            "counts": dict(self._counts),
            "functions": functions,
            "checkpoint_count": len(self.checkpoints),
        }

    def events_page(self, offset=0, limit=200, kind_filter: Optional[str] = None,
                    func_filter: Optional[str] = None):
        """分页返回事件（可按种类 / 函数名过滤），过滤在服务端做，保证翻页口径。"""
        items = self.events
        if kind_filter and kind_filter != "all":
            groups = {
                "steps": (EV_LINE, EV_START),
                "calls": (EV_CALL, EV_RETURN, EV_BUILTIN),
                "errors": (EV_ERROR,),
            }
            wanted = groups.get(kind_filter)
            if wanted:
                items = [e for e in items if e["kind"] in wanted]
        if func_filter:
            key = func_filter.strip()
            # "属于该函数的步骤"：光标在该函数帧内执行（func/callee 是它本身），
            # 或在调用方发起对它的调用（callee 是它）
            items = [e for e in items
                     if e.get("func") == key
                     or (e.get("callee") == key and e["kind"] in (EV_CALL, EV_BUILTIN))]
        total = len(items)
        page = items[offset:offset + limit]
        return {"events": page, "total": total, "offset": offset, "limit": limit}

    def state_at(self, program, source_lines, seq, limits=None):
        """重建第 seq 步光标处的 VM 状态（调试器快照同构）。"""
        if not self.events:
            return None
        seq = max(0, min(seq, len(self.events) - 1))
        target_event = self.events[seq]

        # 最近的、事件序号不超过目标的检查点。所有检查点都是"行边界前态"：
        # VM 停在第 cp['seq'] 个事件（一个行事件）即将发射、其源行第一条
        # 指令尚未分发的光标处。0 号为入口（ready=True）。
        cp = self.checkpoints[0]
        for c in self.checkpoints:
            if c["seq"] <= seq:
                cp = c
            else:
                break

        from .vm import VM
        vm = VM(program, source_lines, limits=limits or {})
        _restore_checkpoint(vm, cp)

        # 入口检查点已处于 start 事件(0)光标，直接用
        if cp.get("ready") and cp["seq"] == seq:
            pass
        else:
            rt2 = ExecutionTracer(mode="replay", target=seq,
                                  max_events=self.max_events + 1)
            rt2.attach(vm)
            rt2._frames = [dict(r) for r in cp["trace_frames"]]
            # 检查点是"事件 cp['seq'] 即将发射"的前态。入口检查点（ready）的
            # start 事件已在 vm.start() 时计数，故下一条编号是 cp['seq']+1；
            # 周期行检查点的行事件尚未发射，下一条编号就是 cp['seq']。
            rt2._seq = cp["seq"] + (1 if cp.get("ready") else 0)
            rt2._counts = dict(self._counts)

            guard = 0
            while not vm.finished and not rt2.at_target:
                vm.step_instruction()
                guard += 1
                if guard > vm.instruction_limit + 1:
                    break

        snapshot = {
            "ok": True,
            "seq": seq,
            "event": target_event,
            "finished": vm.finished,
            # 以下字段与调试器 Debugger.snapshot() 保持同构，前端可复用渲染
            "paused": False,
            "reason": "replay",
            "breakpoints": [],
            "instruction_count": vm.instruction_count,
            "elapsed_ms": round(vm.elapsed_ms(), 3),
            "next_instruction": (vm.peek_instruction().to_dict()
                                 if vm.peek_instruction() else None),
            "current_position": vm.current_position(),
            "call_stack": vm.frame_snapshot(),
            "operand_stack": vm.stack_snapshot(),
            "globals": {k: rt.serialize_value(v) for k, v in sorted(vm.globals.items())},
            "output": list(vm.output),
            "return_value": rt.serialize_value(vm.return_value),
            "error": vm.error.to_dict() if vm.error else None,
        }
        return snapshot


# ---------------------------------------------------------------------------
# 检查点：VM 状态的精简深拷贝 / 恢复
# ---------------------------------------------------------------------------
def _capture_checkpoint(seq, vm, trace_frames, ready=False) -> Dict[str, Any]:
    """捕获 VM 状态。列表深拷贝（会被原地修改）；字符串 / 函数 / 常量共享。"""
    oidmap: Dict[int, rt.RuntimeObject] = {}
    # 第一遍：为每个堆对象建立替身（列表先建空壳，字符串/函数不可变直接共享）
    for oid, obj in vm.heap._objects.items():
        if isinstance(obj, rt.RuntimeList):
            oidmap[oid] = rt.RuntimeList(oid, [])
        else:
            oidmap[oid] = obj

    filled: set = set()

    def copyv(v):
        if isinstance(v, rt.RuntimeObject):
            m = oidmap.get(v.oid)
            if m is None:
                return v  # 未登记的堆对象（理论上不存在），保守共享
            if isinstance(m, rt.RuntimeList):
                if m.oid not in filled:
                    m.items = [copyv(x) for x in v.items]
                    m.size = v.size
                    filled.add(m.oid)
            return m
        return v

    # 第二遍：填充列表元素（保持对象身份关系）
    for oid, obj in vm.heap._objects.items():
        if isinstance(obj, rt.RuntimeList):
            copyv(obj)

    frames = []
    for f in vm.frames:
        frames.append({
            "name": f.func_name,
            "code": f.code,
            "func_obj": f.func_obj,
            "is_main": f.is_main,
            "ip": f.ip,
            "current_line": f.current_line,
            "locals": {k: copyv(v) for k, v in f.locals.items()},
            "stack": [copyv(v) for v in f.stack],
        })

    return {
        "seq": seq,
        "instruction_count": vm.instruction_count,
        "frames": frames,
        "globals": {k: copyv(v) for k, v in vm.globals.items()},
        "heap": oidmap,
        "heap_next_id": vm.heap._next_id,
        "heap_alloc_count": vm.heap._allocation_count,
        "heap_peak": vm.heap._peak_objects,
        "input_queue": list(vm.input_queue),
        "output": list(vm.output),
        "return_value": vm.return_value,
        "trace_frames": [{"name": r["name"], "last_line": r["last_line"],
                          "last_ip": r.get("last_ip", -1),
                          "jumped": r.get("jumped", False)}
                         for r in trace_frames],
        "ready": bool(ready),
    }


def _restore_checkpoint(vm, cp):
    """把检查点恢复进一个全新构造的 VM（替代 vm.start()）。"""
    heap = memory_model.Heap()
    heap._objects = dict(cp["heap"])
    heap._next_id = cp.get("heap_next_id", max(cp["heap"].keys(), default=0) + 1)
    heap._allocation_count = cp.get("heap_alloc_count", 0)
    heap._peak_objects = cp.get("heap_peak", len(heap._objects))
    vm.heap = heap

    vm.globals = dict(cp["globals"])
    # 用户函数对象以检查点（首次运行）中的为准，保证 _functions 与堆/全局一致
    vm._functions = {v.name: v for v in cp["heap"].values()
                     if isinstance(v, rt.RuntimeFunction)}

    from .vm import Frame
    vm.frames = []
    for fr in cp["frames"]:
        f = Frame(fr["name"], fr["code"], fr["func_obj"], is_main=fr["is_main"])
        f.ip = fr["ip"]
        f.current_line = fr["current_line"]
        f.locals = dict(fr["locals"])
        f.stack = list(fr["stack"])
        vm.frames.append(f)

    vm.input_queue = list(cp.get("input_queue", []))
    vm.output = list(cp.get("output", []))
    vm.return_value = cp.get("return_value")
    vm.instruction_count = cp.get("instruction_count", 0)
    vm.finished = False
    vm.paused = False
    vm.error = None
