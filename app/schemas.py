"""POST /render 的请求与响应模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

EventType = Literal[
    "note_on",
    "note_off",
    "pedal_down",
    "pedal_up",
    "sostenuto_down",
    "sostenuto_up",
]
_NOTE_TYPES = {"note_on", "note_off"}


class EventIn(BaseModel):
    time_ms: int = Field(ge=0)
    channel: int
    type: EventType
    # 仅 note_on / note_off 需要;两类踏板事件带了 pitch 也会被忽略
    pitch: int | None = None

    @model_validator(mode="after")
    def _require_pitch_for_note_events(self) -> "EventIn":
        if self.type in _NOTE_TYPES and self.pitch is None:
            raise ValueError("pitch is required for note_on / note_off events")
        return self


class RenderRequest(BaseModel):
    end_ms: int = Field(ge=0)
    events: list[EventIn]

    @model_validator(mode="after")
    def _check_time_order_and_bounds(self) -> "RenderRequest":
        previous = 0  # 所有 time_ms 均 >= 0
        for i, event in enumerate(self.events):
            if event.time_ms < previous:
                raise ValueError(
                    f"events[{i}].time_ms is out of order: events must be "
                    "non-decreasing by time_ms (same timestamps keep input order)"
                )
            if event.time_ms > self.end_ms:
                raise ValueError(
                    f"events[{i}].time_ms ({event.time_ms}) is after end_ms ({self.end_ms})"
                )
            previous = event.time_ms
        return self


class Interval(BaseModel):
    channel: int
    pitch: int
    start_ms: int
    end_ms: int


class EventError(BaseModel):
    # 事件在输入 events 中的下标,从 0 开始
    index: int
    # unpaired_note_off | pedal_already_down | pedal_already_up
    # | sostenuto_already_down | sostenuto_already_up
    reason: str


class RenderResponse(BaseModel):
    intervals: list[Interval]
    errors: list[EventError]


class ChannelStats(BaseModel):
    channel: int
    # 该通道所有音区间时长之和(含零长度音,贡献为 0)
    total_duration_ms: int
    # 半开区间 [start_ms, end_ms) 内同时发声的最大音数;零长度音不参与
    peak_polyphony: int
    # 首次达到 peak_polyphony 的时刻;无正时长音时为 null
    peak_at_ms: int | None
    # 松键后仅靠任一踏板(普通延音 / 选择性延音)延续的时长之和
    # (松键时刻 -> 最后一块保持踏板抬起或 end_ms)
    sustained_duration_ms: int


class RenderStatsResponse(BaseModel):
    intervals: list[Interval]
    errors: list[EventError]
    # 每个出现过按键(note_on)的通道一项,按 channel 升序
    stats: list[ChannelStats]
