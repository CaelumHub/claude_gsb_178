# -*- coding: utf-8 -*-
"""
执行轨迹录制与回放（Execution Tracer）。

目标：程序跑完之后，可以逐步回放"执行到哪一行、调用了什么、进入 / 返回了哪个
函数"，点击任意一步查看当时的**执行位置、变量与调用栈**。

口径与单步调试（debugger.py）、性能剖析（profiler.py）保持一致——挂在 VM 的同
一批钩子上：

  * ``on_instruction`` —— 每条指令执行前（profiler 也是在这里记行命中，debugger
    的 step_into 也是按"源码行是否变化"判定）；
  * 函数调用 / 进入 / 返回 / 运行时错误 —— 与 profiler.function_enter/exit、
    debugger 的调用栈快照使用相同的帧与行号来源。

录制的事件刻意保持紧凑（JSON 数组而非逐帧快照）：

    [seq, type, t_ms, depth, func, line, offset, instr_count, ...负载]

事件类型：
  line   —— 执行位置到达一个（本帧的）新源码行，粒度等同调试器的"单步进入"；
  insn   —— 指令级粒度下的每条指令；
  call   —— 发起一次调用（CALL 执行前），带被调名 / 参数 / 内建返回值；
  enter  —— 进入用户函数（新帧已压入）；
  return —— 函数返回（帧弹出前），带返回值；
  error  —— 运行时错误（诊断已落到 VM 上）。

循环与递归会产生海量步骤，应对手段有三层：
  1. 默认只录"行级"事件（等同 step_into 的粒度），事件数远低于指令数；
  2. 事件总数有上限（默认 5 万，硬上限 20 万），超出时截断并标记 truncated；
  3. 变量 / 调用栈**不逐帧存盘**，而是稀疏地保存 VM 检查点（深克隆），查看某一步
     时从最近检查点确定性地重放到该步——前端再配合虚拟滚动，页面只渲染可见行。

非确定性：``time()`` / ``random()`` 在录制时记下返回值，回放时注入录制值；
``input()`` 通过录制时的输入队列（检查点一并克隆）保证一致。因此回放看到的
变量与录制现场完全一致。
"""

from typing import Any, Dict, List, Optional

from . import bytecode as bc
from . import runtime as rt


# ---------------------------------------------------------------------------
# 事件类型与字段下标
# ---------------------------------------------------------------------------
EV_LINE = "line"
EV_INSN = "insn"
EV_CALL = "call"
EV_ENTER = "enter"
EV_RETURN = "return"
EV_ERROR = "error"

# 所有事件共有的前缀字段
I_SEQ = 0
I_TYPE = 1
I_T = 2
I_DEPTH = 3
I_FUNC = 4
I_LINE = 5
I_OFF = 6
I_IC = 7
# 各类型的负载字段（从下标 8 开始）
I_OP = 8            # line / insn：操作码
I_OPERAND = 9       # insn：操作数
I_CALLEE = 8        # call：被调名（"?": 不可调用值）
I_KIND = 9          # call：user / builtin / invalid
I_ARGC = 10         # call：参数个数
I_ARGS = 11         # call：参数（序列化）
I_RESULT = 12       # call：内建返回值（序列化，用户调用为 None）
I_NONDET = 13       # call：非确定性内建的全局调用序数（time/random）
I_VALUE = 8         # return：返回值（序列化）
I_MAIN = 9          # return：是否 main 帧
I_DIAG = 8          # error：结构化诊断

EVENT_TYPES = (EV_LINE, EV_INSN, EV_CALL, EV_ENTER, EV_RETURN, EV_ERROR)

DEFAULT_MAX_EVENTS = 50_000
MAX_EVENTS_HARD = 200_000

# 检查点：每 512 个事件一个深克隆；最多保留 16 个（超量时间隔翻倍）；
# 所有检查点克隆的堆对象总数预算（防止超大堆把内存吃光）。
CHECKPOINT_INTERVAL = 512
CHECKPOINT_KEEP = 16
HEAP_OBJECT_BUDGET = 300_000

# replay_builtin 返回它表示"正常执行该内建"
PROCEED = object()

# 回放时需要注入录制值的非确定性内建（input 由输入队列保证一致）
_NONDET_BUILTINS = {"time", "random"}


class TraceStop(Exception):
    """回放时到达目标步骤：中止当前指令，VM 状态停在该步，不构成错误。"""


class TraceLimitReached(Exception):
    """录制时达到事件上限：停止推进 VM（轨迹被截断，但已录部分有效）。"""


def clamp_max_events(n) -> int:
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = DEFAULT_MAX_EVENTS
    return max(100, min(n, MAX_EVENTS_HARD))


# ---------------------------------------------------------------------------
# 堆 / 值的深克隆（检查点用）
# ---------------------------------------------------------------------------
def _snapshot_objects(vm) -> Dict[int, rt.RuntimeObject]:
    """从 VM 堆克隆出一份独立的 list/string 对象图（函数对象不复制）。"""
    objs: Dict[int, rt.RuntimeObject] = {}
    for oid, obj in vm.heap._objects.items():
        if isinstance(obj, rt.RuntimeList):
            objs[oid] = rt.RuntimeList(oid, [])
        elif isinstance(obj, rt.RuntimeString):
            objs[oid] = rt.RuntimeString(oid, obj.value)
    for oid, obj in vm.heap._objects.items():
        if isinstance(obj, rt.RuntimeList):
            objs[oid].items = [_ref_value(x, objs) for x in obj.items]
    return objs


def _copy_objects(src: Dict[int, rt.RuntimeObject]) -> Dict[int, rt.RuntimeObject]:
    """对一份已克隆的对象图再做一次独立深拷贝（回放不得污染检查点）。"""
    objs: Dict[int, rt.RuntimeObject] = {}
    for oid, obj in src.items():
        if isinstance(obj, rt.RuntimeList):
            objs[oid] = rt.RuntimeList(oid, [])
        elif isinstance(obj, rt.RuntimeString):
            objs[oid] = rt.RuntimeString(oid, obj.value)
    for oid, obj in src.items():
        if isinstance(obj, rt.RuntimeList):
            objs[oid].items = [_ref_value(x, objs) for x in obj.items]
    return objs


def _ref_value(v, objs):
    """把一个值映射进克隆对象图；函数 / 内建用标记元组占位。"""
    if isinstance(v, rt.RuntimeList):
        return objs[v.oid]
    if isinstance(v, rt.RuntimeString):
        return objs[v.oid]
    if isinstance(v, rt.RuntimeFunction):
        return ("fn", v.name)
    if isinstance(v, rt.BuiltinFunction):
        return ("bi", v.name)
    return v


def _unref_value(v, vm, objs):
    """检查点恢复：标记元组重新绑到新 VM 的函数 / 内建，对象绑回堆。"""
    if isinstance(v, tuple) and len(v) == 2 and v[0] in ("fn", "bi"):
        if v[0] == "fn":
            return vm._functions.get(v[1])
        return vm.builtins.get(v[1])
    if isinstance(v, (rt.RuntimeList, rt.RuntimeString)):
        return objs.get(v.oid, v)
    return v


def capture_state(vm) -> dict:
    """深克隆 VM 当前执行状态为一个检查点。"""
    objs = _snapshot_objects(vm)
    frames = []
    for fr in vm.frames:
        frames.append({
            "name": fr.func_name,
            "is_main": fr.is_main,
            "ip": fr.ip,
            "line": fr.current_line,
            "locals": {k: _ref_value(v, objs) for k, v in fr.locals.items()},
            "stack": [_ref_value(v, objs) for v in fr.stack],
        })
    return {
        "frames": frames,
        "globals": {k: _ref_value(v, objs) for k, v in vm.globals.items()},
        "objects": objs,
        "next_id": vm.heap._next_id,
        "alloc_count": vm.heap._allocation_count,
        "peak": vm.heap._peak_objects,
        "output": list(vm.output),
        "input_queue": list(vm.input_queue),
        "instruction_count": vm.instruction_count,
        "return_value": _ref_value(vm.return_value, objs),
    }


def restore_state(vm, state: dict):
    """把检查点恢复进一个**刚构造、未 start** 的 VM。"""
    objects = _copy_objects(state["objects"])
    # 新 VM 已按相同程序预分配函数对象（同 oid）；list/string 覆盖进来
    vm.heap._objects.update(objects)
    vm.heap._next_id = state["next_id"]
    vm.heap._allocation_count = state["alloc_count"]
    vm.heap._peak_objects = state["peak"]

    def un(v):
        return _unref_value(v, vm, objects)

    vm.globals = {k: un(v) for k, v in state["globals"].items()}
    # 函数提升：确保用户函数对象仍在全局里（globals 快照本身已含，这里仅兜底）
    for name, fn in vm._functions.items():
        vm.globals.setdefault(name, fn)

    from .vm import Frame
    frames = []
    for fst in state["frames"]:
        if fst["is_main"]:
            code = vm.program.main or bc.FunctionCode("<main>", 0, [])
        else:
            code = vm.program.functions[fst["name"]]
        fr = Frame(fst["name"], code, is_main=fst["is_main"])
        if not fst["is_main"]:
            fr.func_obj = vm._functions.get(fst["name"])
        fr.ip = fst["ip"]
        fr.current_line = fst["line"]
        fr.locals = {k: un(v) for k, v in fst["locals"].items()}
        fr.stack = [un(v) for v in fst["stack"]]
        frames.append(fr)
    vm.frames = frames
    vm.output = list(state["output"])
    vm.input_queue = list(state["input_queue"])
    vm.instruction_count = state["instruction_count"]
    vm.return_value = un(state["return_value"])
    vm.finished = not bool(frames)
    vm.paused = False
    vm.error = None


# ---------------------------------------------------------------------------
# 序列化辅助
# ---------------------------------------------------------------------------
def _json_operand(v):
    """指令操作数里只有 int/float/str/None，直接 JSON 安全。"""
    return v if isinstance(v, (int, float, str)) or v is None else str(v)


def deserialize_primitive(sv):
    """把 serialize_value 的原始值结构转回 Python 原始值（仅用于 time/random）。"""
    if sv is None:
        return None
    kind = sv.get("kind")
    if kind in ("int", "float"):
        return sv["value"]
    if kind == "bool":
        return sv["value"] == "true"
    if kind == "null":
        return None
    if kind == "string":
        return sv["value"]
    return sv


# ---------------------------------------------------------------------------
# Tracer
# ---------------------------------------------------------------------------
class Tracer:
    """录制 / 回放执行轨迹。

    mode:
      record —— 追加事件、按间隔存检查点、达上限抛 TraceLimitReached；
      replay —— 只计数（与录制同构以保证 seq 对齐），到 stop_seq 抛 TraceStop，
                沿途顺带补建检查点（首次深回放后后续查看即变快）。
    """

    def __init__(self, mode="record", granularity="line",
                 max_events=DEFAULT_MAX_EVENTS, stop_seq: Optional[int] = None,
                 builtin_replays: Optional[Dict[int, Any]] = None):
        self.mode = mode
        self.granularity = granularity if granularity in ("line", "instruction") else "line"
        self.max_events = clamp_max_events(max_events)
        self.stop_seq = stop_seq
        # 非确定性内建按"调用序数"（第几次调用 time/random）注入录制值；
        # 该序数同时保存在每条 CALL 事件负载里，跨检查点也能正确对齐
        self.builtin_replays = builtin_replays or {}
        self._nondet_index = 0

        self.seq = -1
        self.events: List[list] = [] if mode == "record" else []
        self.checkpoints: Dict[int, dict] = {}
        self.cp_interval = CHECKPOINT_INTERVAL
        self._cp_obj_count: Dict[int, int] = {}
        self.truncated = False
        self._start_time = None
        # depth -> (func_name, line)，行级去重（与 debugger.step_into 同口径）
        self._line_keys: Dict[int, tuple] = {}
        # depth -> (frame 对象 id, 上一条指令偏移)：识别循环回边
        self._last_off: Dict[int, tuple] = {}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def begin_run(self, vm):
        import time
        self._start_time = time.perf_counter()

    def add_checkpoint(self, vm, seq):
        """显式存检查点（如运行前的初始状态 seq=-1）。"""
        state = capture_state(vm)
        self.checkpoints[seq] = state
        self._cp_obj_count[seq] = len(state["objects"])

    def _elapsed_ms(self, vm):
        if self._start_time is None:
            return 0.0
        import time
        return round((time.perf_counter() - self._start_time) * 1000.0, 3)

    # ------------------------------------------------------------------
    # 事件出口
    # ------------------------------------------------------------------
    def _emit(self, vm, etype, depth, func, line, off, payload=None):
        self.seq += 1
        seq = self.seq
        if self.mode == "record":
            ev = [seq, etype, self._elapsed_ms(vm), depth, func, line, off,
                  vm.instruction_count]
            if payload:
                ev.extend(payload)
            self.events.append(ev)
            if len(self.events) >= self.max_events:
                self.truncated = True
                raise TraceLimitReached()
        elif self.mode == "replay" and seq == self.stop_seq:
            # 回放：到达目标步骤，状态停在该事件现场
            raise TraceStop()

    def _checkpoint_boundary(self, vm):
        """在下一指令边界为"上一已完成事件"存检查点。

        检查点必须落在干净的指令边界（上一条指令已完成、当前指令未执行），
        这样从它续跑不会重复产生事件。enter/return 等指令内事件完成后，也会
        在紧接着的下一指令边界被这里覆盖。
        """
        seq = self.seq
        if seq < 0 or seq in self.checkpoints:
            return
        if (seq + 1) % self.cp_interval != 0:
            return
        n_obj = len(vm.heap._objects)
        used = sum(self._cp_obj_count.values())
        if used + n_obj > HEAP_OBJECT_BUDGET:
            return
        self.checkpoints[seq] = capture_state(vm)
        self._cp_obj_count[seq] = n_obj
        self._evict_checkpoints_if_needed()

    def _evict_checkpoints_if_needed(self):
        """检查点超量时，丢弃初始点之外每隔一个的旧点，间隔翻倍。"""
        mutable = sorted(s for s in self.checkpoints if s >= 0)
        if len(mutable) <= CHECKPOINT_KEEP:
            return
        keep = set(mutable[::2])
        for s in mutable:
            if s not in keep:
                self.checkpoints.pop(s, None)
                self._cp_obj_count.pop(s, None)
        self.cp_interval *= 2

    def should_pause(self, vm):
        """回放续跑前 / 调用内建前的暂停判定（挂到 VM.run 与内建调用路径上）。"""
        return self.mode == "replay" and self.seq >= self.stop_seq

    # ------------------------------------------------------------------
    # VM 钩子
    # ------------------------------------------------------------------
    def on_instruction(self, vm, frame, ins):
        # 指令边界：在本指令行号更新前为"上一已完成事件"存检查点，保证检查点的
        # 帧行/ip 与指令边界完全一致（录制 / 回放共用同一口径）
        self._checkpoint_boundary(vm)
        depth = len(vm.frames) - 1
        if self.granularity == "instruction":
            self._emit(vm, EV_INSN, depth, frame.func_name, ins.line, ins.offset,
                       [ins.op, _json_operand(ins.operand)])
        else:
            key = (frame.func_name, ins.line)
            # 循环回边（在同一帧内向后跳转）时清除行键，让每轮迭代都产生事件
            prev = self._last_off.get(depth)
            if prev is not None and prev[0] == id(frame) and ins.offset < prev[1]:
                self._line_keys.pop(depth, None)
            self._last_off[depth] = (id(frame), ins.offset)
            if self._line_keys.get(depth) != key:
                self._line_keys[depth] = key
                self._emit(vm, EV_LINE, depth, frame.func_name, ins.line, ins.offset,
                           [ins.op])
        if ins.op == bc.OP_CALL:
            self._emit_call(vm, frame, ins)

    def _emit_call(self, vm, frame, ins):
        argc = int(ins.operand or 0)
        # 钩子在指令执行前：栈布局为 [..., callee, arg0, ..., argN]
        st = frame.stack
        if len(st) >= argc + 1:
            callee = st[-1 - argc]
            args = st[-argc:] if argc else []
        else:
            callee, args = None, []
        if isinstance(callee, rt.RuntimeFunction):
            name, kind = callee.name, "user"
        elif isinstance(callee, rt.BuiltinFunction):
            name, kind = callee.name, "builtin"
        else:
            name, kind = "?", "invalid"
        payload = [name, kind, argc, [rt.serialize_value(a) for a in args], None,
                   self._nondet_index if (kind == "builtin"
                                          and name in _NONDET_BUILTINS) else None]
        self._emit(vm, EV_CALL, len(vm.frames) - 1, frame.func_name,
                   ins.line, ins.offset, payload)
        if kind == "builtin" and name in _NONDET_BUILTINS:
            self._nondet_index += 1

    def after_builtin(self, vm, callee, result):
        """内建调用执行完：把返回值补进刚录下的 call 事件（仅录制）。"""
        if self.mode != "record" or not self.events:
            return
        ev = self.events[-1]
        if ev[I_TYPE] == EV_CALL:
            ev[I_RESULT] = rt.serialize_value(result)

    @property
    def current_nondet_index(self):
        """当前正在处理的非确定性内建调用序数（与 CALL 事件负载一致）。

        _emit_call 先发事件再自增计数，VM 随后（在内建真正执行前）读取本属性，
        因此两种模式都取 self._nondet_index - 1。
        """
        return self._nondet_index - 1

    def replay_builtin(self, vm, callee, args, nondet_index=None):
        """回放内建调用：time/random 注入录制值（按事件携带的调用序数），其余正常执行。"""
        if (self.mode != "record"
                and getattr(callee, "name", None) in _NONDET_BUILTINS
                and nondet_index is not None
                and nondet_index in self.builtin_replays):
            return self.builtin_replays[nondet_index]
        return PROCEED

    def on_enter(self, vm, new_frame, ins):
        self._emit(vm, EV_ENTER, len(vm.frames) - 1, new_frame.func_name,
                   ins.line, ins.offset)

    def on_return(self, vm, frame, value):
        depth = len(vm.frames) - 1
        try:
            self._emit(vm, EV_RETURN, depth, frame.func_name,
                       frame.current_line, max(0, frame.ip - 1),
                       [rt.serialize_value(value), bool(frame.is_main)])
        finally:
            # 帧弹出后该深度的去重状态失效（回放中止时也需清理）
            self._line_keys.pop(depth, None)
            self._last_off.pop(depth, None)

    def on_error(self, vm, diagnostic):
        if vm.frames:
            frame = vm.frames[-1]
            depth, func, line = len(vm.frames) - 1, frame.func_name, frame.current_line
            off = max(0, frame.ip - 1)
        else:
            depth, func, line, off = 0, "<main>", 0, 0
        self._emit(vm, EV_ERROR, depth, func, line or diagnostic.line, off,
                   [diagnostic.to_dict()])

    # ------------------------------------------------------------------
    # 汇总
    # ------------------------------------------------------------------
    def type_counts(self) -> Dict[str, int]:
        counts = {t: 0 for t in EVENT_TYPES}
        for ev in self.events:
            counts[ev[I_TYPE]] = counts.get(ev[I_TYPE], 0) + 1
        return counts

    def nondeterministic_values(self) -> Dict[int, Any]:
        """从已录事件中提取 time/random 的返回值（调用序数 -> 原始值）。"""
        out = {}
        for ev in self.events:
            if ev[I_TYPE] == EV_CALL and ev[I_KIND] == "builtin" \
                    and ev[I_CALLEE] in _NONDET_BUILTINS \
                    and len(ev) > I_NONDET and ev[I_NONDET] is not None \
                    and ev[I_RESULT] is not None:
                out[ev[I_NONDET]] = deserialize_primitive(ev[I_RESULT])
        return out


def snapshot_at(vm, ev: Optional[list], seq: int) -> dict:
    """生成某一步的完整状态快照（结构与 Debugger.snapshot 对齐）。"""
    top = vm.frames[-1] if vm.frames else None
    ins = vm.peek_instruction()
    etype = ev[I_TYPE] if ev else None
    is_main_ret = etype == EV_RETURN and len(ev) > I_MAIN and ev[I_MAIN]
    # enter/return 事件发生在指令执行中间：位置以事件自带的（调用点/帧）行为准
    if etype in (EV_ENTER, EV_RETURN, EV_ERROR, EV_CALL) and ev is not None:
        position = [ev[I_FUNC], ev[I_LINE]]
    else:
        position = [top.func_name if top else "<main>",
                    top.current_line if top else 0]
    finished = bool(vm.finished) or is_main_ret or vm.error is not None
    return {
        "seq": seq,
        "event": ev,
        "finished": finished,
        "instruction_count": vm.instruction_count,
        "elapsed_ms": ev[I_T] if ev else round(vm.elapsed_ms(), 3),
        "error": vm.error.to_dict() if vm.error else None,
        "next_instruction": ins.to_dict() if ins else None,
        "position": position,
        "call_stack": vm.frame_snapshot(),
        "operand_stack": vm.stack_snapshot(),
        "globals": {k: rt.serialize_value(v) for k, v in sorted(vm.globals.items())},
        "output": list(vm.output),
        "return_value": rt.serialize_value(vm.return_value),
    }
