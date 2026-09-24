"""发声区间渲染核心逻辑(仅依赖标准库,方便独立测试)。

规则:
- 事件按输入顺序逐条处理(同一时刻也保持输入顺序)。
- note_on: 在对应 (channel, pitch) 上生成一个新的发声区间,重复按键
  各自独立,松键按 FIFO 配对“最早仍按住”的 note_on。
- note_off: 按 FIFO 配对松键,配对后:
    * 当下没有任何踏板保持该音 -> 在松键时刻立即截断;
    * 否则 -> 记为“松键后待释放”,声音继续,由当下踩着且能保持它的
      踏板共同保持。普通延音踏板保持全部待释放音;选择性延音踏板只
      保持踩下瞬间仍按住(被锁定)的音。
- pedal_down: 踩下普通延音踏板;踩下后该通道仍在发声的待释放音
  (含此前仅靠选择性踏板延续的音)也由它保持;重复踩下无效。
- pedal_up:   抬起普通延音踏板,不再由它保持的待释放音在同一时刻
  截断;仍被选择性踏板保持的音继续发声;未踩下时抬起无效。
- sostenuto_down: 锁定该通道当下仍按住的全部音;重复踩下无效。
  踩下之后新按的音不进入锁定集合。
- sostenuto_up:   解除锁定;不再被任何踏板保持的待释放音在同一时刻
  截断,仍由普通延音踏板保持的音继续发声;未踩下时抬起无效。
- end_ms:     结束时刻,所有仍在发声的音(按住的、任一踏板待释放的)
  统一在此截断。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable

# 错误原因码
UNPAIRED_NOTE_OFF = "unpaired_note_off"  # 没有可配对的 note_on(松键多于按键)
PEDAL_ALREADY_DOWN = "pedal_already_down"  # 普通延音踏板已踩下,重复 pedal_down
PEDAL_ALREADY_UP = "pedal_already_up"  # 普通延音踏板未踩下,无效 pedal_up
SOSTENUTO_ALREADY_DOWN = "sostenuto_already_down"  # 选择性踏板已踩下,重复踩下
SOSTENUTO_ALREADY_UP = "sostenuto_already_up"  # 选择性踏板未踩下,无效抬起

# 保持者踏板标识
_SUSTAIN = "sustain"      # 普通延音踏板
_SOSTENUTO = "sostenuto"  # 选择性延音踏板


@dataclass
class _Voice:
    """一次 note_on 对应的独立发声区间。"""

    channel: int
    pitch: int
    start_ms: int
    end_ms: int | None = None
    # 松键时刻;None 表示键仍按住(或已随松键立即结束),
    # 用于计算 sustained_duration_ms
    released_ms: int | None = None
    # 是否被当下踩着的选择性延音踏板锁定
    # (最近一次 sostenuto_down 时键仍按住,且此后选择性踏板未抬起)
    sost_locked: bool = False
    # 松键后仍保持该音的踏板集合;None 表示键仍按住或音已结束
    sustainers: set[str] | None = None


def _get_attrs(ev: Any) -> tuple[int, int, str, int | None]:
    """兼容 pydantic 模型 / 普通对象 / dict。"""
    if isinstance(ev, dict):
        return ev["time_ms"], ev["channel"], ev["type"], ev.get("pitch")
    return ev.time_ms, ev.channel, ev.type, getattr(ev, "pitch", None)


def _lift_pedal(
    released: dict[int, list[_Voice]],
    channel: int,
    pedal: str,
    time_ms: int,
) -> None:
    """某块踏板抬起:从该通道各待释放音的保持集合中移除它。

    保持集合变空的音在 time_ms 截断;仍由另一踏板保持的音保留。
    """
    pending = released.get(channel)
    if not pending:
        return
    remaining: list[_Voice] = []
    for voice in pending:
        voice.sustainers.discard(pedal)  # type: ignore[union-attr]
        if voice.sustainers:
            remaining.append(voice)
        else:
            voice.end_ms = time_ms
    if remaining:
        released[channel] = remaining
    else:
        del released[channel]


def _collect(
    end_ms: int, events: Iterable[Any]
) -> tuple[list[_Voice], list[dict]]:
    """把原始事件流处理成发声区间(内部对象)与错误列表。

    区间的 ``end_ms`` 为 None 表示仍在发声,由调用方按 end_ms 收口。
    """
    sustain_down: set[int] = set()  # 正踩着普通延音踏板的通道
    sost_down: set[int] = set()     # 正踩着选择性延音踏板的通道
    # 通道 -> 音高 -> 仍按住的音(FIFO)
    held: dict[int, dict[int, deque[_Voice]]] = {}
    # 通道 -> 松键后仍靠踏板发声的音
    released: dict[int, list[_Voice]] = {}
    voices: list[_Voice] = []       # 全部发声区间(按 note_on 顺序)
    errors: list[dict] = []

    for index, ev in enumerate(events):
        time_ms, channel, kind, pitch = _get_attrs(ev)

        if kind == "note_on":
            voice = _Voice(channel=channel, pitch=pitch, start_ms=time_ms)
            voices.append(voice)
            held.setdefault(channel, {}).setdefault(pitch, deque()).append(voice)

        elif kind == "note_off":
            channel_held = held.get(channel, {})
            queue = channel_held.get(pitch)
            if not queue:
                # 没有任何仍按住的同音高按键:松键无法配对
                errors.append({"index": index, "reason": UNPAIRED_NOTE_OFF})
                continue
            voice = queue.popleft()  # FIFO:配对最早仍按住的 note_on
            if not queue:
                del channel_held[pitch]
            # 松键瞬间能保持该音的踏板:
            # 普通延音踩着即可;选择性踏板要求该音处于锁定集合
            sustainers: set[str] = set()
            if channel in sustain_down:
                sustainers.add(_SUSTAIN)
            if channel in sost_down and voice.sost_locked:
                sustainers.add(_SOSTENUTO)
            if sustainers:
                # 键已松开但声音靠踏板延续
                voice.released_ms = time_ms
                voice.sustainers = sustainers
                released.setdefault(channel, []).append(voice)
            else:
                voice.end_ms = time_ms

        elif kind == "pedal_down":
            if channel in sustain_down:
                errors.append({"index": index, "reason": PEDAL_ALREADY_DOWN})
                # 无效切换不改变状态
            else:
                sustain_down.add(channel)
                # 踩下普通延音踏板:该通道仍在发声的待释放音
                # (此时仅可能由选择性踏板保持)改由两块踏板共同保持
                for voice in released.get(channel, ()):
                    voice.sustainers.add(_SUSTAIN)  # type: ignore[union-attr]

        elif kind == "pedal_up":
            if channel not in sustain_down:
                errors.append({"index": index, "reason": PEDAL_ALREADY_UP})
                # 无效切换不改变状态
            else:
                sustain_down.remove(channel)
                _lift_pedal(released, channel, _SUSTAIN, time_ms)

        elif kind == "sostenuto_down":
            if channel in sost_down:
                errors.append({"index": index, "reason": SOSTENUTO_ALREADY_DOWN})
                # 无效切换不改变状态,也不重新锁定(之后新按的音仍不锁定)
            else:
                sost_down.add(channel)
                # 锁定该通道当下仍按住的全部音(各音高 FIFO 中的每个音)
                for queue in held.get(channel, {}).values():
                    for voice in queue:
                        voice.sost_locked = True

        elif kind == "sostenuto_up":
            if channel not in sost_down:
                errors.append({"index": index, "reason": SOSTENUTO_ALREADY_UP})
                # 无效切换不改变状态
            else:
                sost_down.remove(channel)
                # 仍按住的音解除锁定
                for queue in held.get(channel, {}).values():
                    for voice in queue:
                        voice.sost_locked = False
                _lift_pedal(released, channel, _SOSTENUTO, time_ms)

    return voices, errors


def _interval_dict(voice: _Voice, end_ms: int) -> dict:
    return {
        "channel": voice.channel,
        "pitch": voice.pitch,
        "start_ms": voice.start_ms,
        "end_ms": voice.end_ms if voice.end_ms is not None else end_ms,
    }


def _channel_stats(voices: list[_Voice], end_ms: int) -> list[dict]:
    """按通道汇总统计,通道号升序。

    只统计出现过 note_on 的通道(无效事件不产生区间,不影响统计)。
    峰值按半开区间 [start_ms, end_ms) 内同时发声的音数扫描;
    零长度音(start_ms == end_ms)不参与峰值。
    """
    by_channel: dict[int, list[_Voice]] = {}
    for voice in voices:
        by_channel.setdefault(voice.channel, []).append(voice)

    stats: list[dict] = []
    for channel in sorted(by_channel):
        channel_voices = by_channel[channel]
        total_duration_ms = 0
        sustained_duration_ms = 0
        # 时刻 -> 该时刻同时发生的净变化数:起音 +1,结束 -1(零长度除外)
        deltas: dict[int, int] = {}
        has_positive = False

        for voice in channel_voices:
            stop_ms = voice.end_ms if voice.end_ms is not None else end_ms
            duration = stop_ms - voice.start_ms
            total_duration_ms += duration
            if duration > 0:
                has_positive = True
                deltas[voice.start_ms] = deltas.get(voice.start_ms, 0) + 1
                deltas[stop_ms] = deltas.get(stop_ms, 0) - 1
            if voice.released_ms is not None:
                # 松键后靠任一踏板延续的部分:
                # 松键时刻 -> 最后一块保持踏板抬起(或 end_ms)
                sustained_duration_ms += stop_ms - voice.released_ms

        peak_polyphony = 0
        peak_at_ms: int | None = None
        if has_positive:
            active = 0
            for time_ms in sorted(deltas):
                active += deltas[time_ms]
                if active > peak_polyphony:
                    # 只在首次达到更高峰值时记录,即首次达到峰值的时刻
                    peak_polyphony = active
                    peak_at_ms = time_ms

        stats.append(
            {
                "channel": channel,
                "total_duration_ms": total_duration_ms,
                "peak_polyphony": peak_polyphony,
                "peak_at_ms": peak_at_ms,
                "sustained_duration_ms": sustained_duration_ms,
            }
        )

    return stats


def render(end_ms: int, events: Iterable[Any]) -> dict[str, list[dict]]:
    """把原始事件流渲染成实际发声区间。

    返回 ``{"intervals": [...], "errors": [...]}``:
    intervals 按起音(note_on)事件的处理顺序排列;
    errors 中的 index 为事件在输入 events 中的下标(从 0 开始)。
    """
    voices, errors = _collect(end_ms, events)
    intervals = [_interval_dict(voice, end_ms) for voice in voices]
    return {"intervals": intervals, "errors": errors}


def render_stats(end_ms: int, events: Iterable[Any]) -> dict[str, Any]:
    """渲染发声区间并附带按通道升序的统计。

    返回 ``{"intervals": [...], "errors": [...], "stats": [...]}``。
    """
    voices, errors = _collect(end_ms, events)
    intervals = [_interval_dict(voice, end_ms) for voice in voices]
    stats = _channel_stats(voices, end_ms)
    return {"intervals": intervals, "errors": errors, "stats": stats}
