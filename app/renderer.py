"""发声区间渲染核心逻辑(仅依赖标准库,方便独立测试)。

规则:
- 事件按输入顺序逐条处理(同一时刻也保持输入顺序)。
- note_on: 在对应 (channel, pitch) 上生成一个新的发声区间,重复按键
  各自独立,松键按 FIFO 配对“最早仍按住”的 note_on。
- note_off:
    * 踏板未踩下 -> 该音立即截断;
    * 踏板踩着   -> 记为“待释放”,继续发声。
- pedal_down: 踩下该通道延音踏板;重复踩下是无效切换。
- pedal_up:   抬起踏板,该通道全部待释放音在同一时刻截断;未踩下时抬起无效。
- end_ms:     结束时刻,所有仍在发声的音(按住的、待释放的)统一在此截断。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable

# 错误原因码
UNPAIRED_NOTE_OFF = "unpaired_note_off"  # 没有可配对的 note_on(松键多于按键)
PEDAL_ALREADY_DOWN = "pedal_already_down"  # 踏板已踩下,重复 pedal_down
PEDAL_ALREADY_UP = "pedal_already_up"  # 踏板未踩下,无效 pedal_up


@dataclass
class _Voice:
    """一次 note_on 对应的独立发声区间。"""

    channel: int
    pitch: int
    start_ms: int
    end_ms: int | None = None
    # 踩着踏板松键的时刻;None 表示未经历“松键后踏板延续”,
    # 用于计算 sustained_duration_ms
    released_ms: int | None = None


def _get_attrs(ev: Any) -> tuple[int, int, str, int | None]:
    """兼容 pydantic 模型 / 普通对象 / dict。"""
    if isinstance(ev, dict):
        return ev["time_ms"], ev["channel"], ev["type"], ev.get("pitch")
    return ev.time_ms, ev.channel, ev.type, getattr(ev, "pitch", None)


def _collect(
    end_ms: int, events: Iterable[Any]
) -> tuple[list[_Voice], list[dict]]:
    """把原始事件流处理成发声区间(内部对象)与错误列表。

    区间的 ``end_ms`` 为 None 表示仍在发声,由调用方按 end_ms 收口。
    """
    pedal_down: set[int] = set()                 # 正踩着踏板的通道
    held: dict[tuple[int, int], deque[_Voice]] = {}  # (通道, 音高) -> 仍按住的音
    pending: dict[int, list[_Voice]] = {}        # 通道 -> 松键后等待抬踏板截断的音
    voices: list[_Voice] = []                    # 全部发声区间(按 note_on 顺序)
    errors: list[dict] = []

    for index, ev in enumerate(events):
        time_ms, channel, kind, pitch = _get_attrs(ev)

        if kind == "note_on":
            voice = _Voice(channel=channel, pitch=pitch, start_ms=time_ms)
            voices.append(voice)
            held.setdefault((channel, pitch), deque()).append(voice)

        elif kind == "note_off":
            queue = held.get((channel, pitch))
            if not queue:
                # 没有任何仍按住的同音高按键:松键无法配对
                errors.append({"index": index, "reason": UNPAIRED_NOTE_OFF})
                continue
            voice = queue.popleft()  # FIFO:配对最早仍按住的 note_on
            if not queue:
                del held[(channel, pitch)]
            if channel in pedal_down:
                # 踩着踏板:键松开但声音延续,记为待释放
                voice.released_ms = time_ms
                pending.setdefault(channel, []).append(voice)
            else:
                voice.end_ms = time_ms

        elif kind == "pedal_down":
            if channel in pedal_down:
                errors.append({"index": index, "reason": PEDAL_ALREADY_DOWN})
                # 无效切换不改变状态
            else:
                pedal_down.add(channel)

        elif kind == "pedal_up":
            if channel not in pedal_down:
                errors.append({"index": index, "reason": PEDAL_ALREADY_UP})
                # 无效切换不改变状态
            else:
                pedal_down.remove(channel)
                # 抬起踏板:该通道所有待释放音在同一时刻截断
                for voice in pending.pop(channel, ()):
                    voice.end_ms = time_ms

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
                # 松键后靠踏板延续的部分:松键时刻 -> 抬踏板或 end_ms
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
