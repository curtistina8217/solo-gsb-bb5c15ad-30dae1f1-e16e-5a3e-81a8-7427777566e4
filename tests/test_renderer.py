"""renderer 核心逻辑测试(仅依赖标准库):python3 -m unittest discover -s tests"""

import unittest

from app.renderer import (
    PEDAL_ALREADY_DOWN,
    PEDAL_ALREADY_UP,
    SOSTENUTO_ALREADY_DOWN,
    SOSTENUTO_ALREADY_UP,
    UNPAIRED_NOTE_OFF,
    render,
    render_stats,
)


def ev(time_ms, channel, kind, pitch=None):
    return {
        "time_ms": time_ms,
        "channel": channel,
        "type": kind,
        "pitch": pitch,
    }


class RenderTests(unittest.TestCase):
    def test_basic_on_off(self):
        result = render(
            1000,
            [ev(0, 0, "note_on", 60), ev(500, 0, "note_off", 60)],
        )
        self.assertEqual(
            result["intervals"],
            [{"channel": 0, "pitch": 60, "start_ms": 0, "end_ms": 500}],
        )
        self.assertEqual(result["errors"], [])

    def test_end_ms_closes_open_notes(self):
        result = render(200, [ev(10, 3, "note_on", 72)])
        self.assertEqual(
            result["intervals"],
            [{"channel": 3, "pitch": 72, "start_ms": 10, "end_ms": 200}],
        )

    def test_unpaired_note_off(self):
        result = render(
            100,
            [ev(0, 0, "note_on", 60), ev(10, 0, "note_off", 60), ev(20, 0, "note_off", 60)],
        )
        self.assertEqual(
            result["errors"], [{"index": 2, "reason": UNPAIRED_NOTE_OFF}]
        )

    def test_repeated_keys_get_independent_intervals_fifo(self):
        # 两次按键:0ms / 100ms;两次松键:200ms / 300ms,FIFO 配对
        result = render(
            1000,
            [
                ev(0, 0, "note_on", 60),
                ev(100, 0, "note_on", 60),
                ev(200, 0, "note_off", 60),
                ev(300, 0, "note_off", 60),
            ],
        )
        self.assertEqual(
            result["intervals"],
            [
                {"channel": 0, "pitch": 60, "start_ms": 0, "end_ms": 200},
                {"channel": 0, "pitch": 60, "start_ms": 100, "end_ms": 300},
            ],
        )

    def test_pedal_defers_release_and_cuts_all_together(self):
        result = render(
            1000,
            [
                ev(0, 0, "note_on", 60),
                ev(50, 0, "note_on", 64),
                ev(100, 0, "pedal_down"),
                ev(200, 0, "note_off", 60),   # 待释放
                ev(250, 0, "note_off", 64),   # 待释放
                ev(400, 0, "pedal_up"),       # 两个音同时截断于 400
            ],
        )
        self.assertEqual(
            result["intervals"],
            [
                {"channel": 0, "pitch": 60, "start_ms": 0, "end_ms": 400},
                {"channel": 0, "pitch": 64, "start_ms": 50, "end_ms": 400},
            ],
        )
        self.assertEqual(result["errors"], [])

    def test_reon_while_pedal_held_is_independent(self):
        # 踩着踏板时松键再重按:旧音等抬踏板截断,新音独立
        result = render(
            1000,
            [
                ev(0, 0, "note_on", 60),
                ev(50, 0, "pedal_down"),
                ev(100, 0, "note_off", 60),  # 旧音 -> 待释放
                ev(200, 0, "note_on", 60),   # 新音,独立区间
                ev(300, 0, "note_off", 60),  # 配对新音;踏板仍踩着 -> 也待释放
                ev(500, 0, "pedal_up"),      # 两音同时截断于 500
            ],
        )
        self.assertEqual(
            result["intervals"],
            [
                {"channel": 0, "pitch": 60, "start_ms": 0, "end_ms": 500},
                {"channel": 0, "pitch": 60, "start_ms": 200, "end_ms": 500},
            ],
        )

    def test_note_off_while_pedal_down_consumes_from_held_fifo(self):
        result = render(
            1000,
            [
                ev(0, 0, "note_on", 60),
                ev(10, 0, "note_on", 60),
                ev(20, 0, "pedal_down"),
                ev(30, 0, "note_off", 60),  # 配对最早按住的那个
                ev(40, 0, "pedal_up"),      # 截断待释放的旧音
                # 第二次按下的音仍按住,到 end_ms 才结束
            ],
        )
        self.assertEqual(
            result["intervals"],
            [
                {"channel": 0, "pitch": 60, "start_ms": 0, "end_ms": 40},
                {"channel": 0, "pitch": 60, "start_ms": 10, "end_ms": 1000},
            ],
        )

    def test_invalid_pedal_switches(self):
        result = render(
            1000,
            [
                ev(0, 0, "pedal_down"),
                ev(10, 0, "pedal_down"),  # 重复踩
                ev(20, 0, "pedal_up"),
                ev(30, 0, "pedal_up"),    # 未踩时抬
            ],
        )
        self.assertEqual(
            result["errors"],
            [
                {"index": 1, "reason": PEDAL_ALREADY_DOWN},
                {"index": 3, "reason": PEDAL_ALREADY_UP},
            ],
        )

    def test_channels_are_independent(self):
        result = render(
            1000,
            [
                ev(0, 0, "pedal_down"),
                ev(0, 1, "note_on", 60),   # 通道1没踩踏板
                ev(10, 1, "note_off", 60),  # 立即截断
                ev(20, 0, "note_on", 70),   # 通道0踩着踏板
                ev(30, 0, "note_off", 70),  # 待释放,到 end_ms 才截断
            ],
        )
        self.assertEqual(
            result["intervals"],
            [
                {"channel": 1, "pitch": 60, "start_ms": 0, "end_ms": 10},
                {"channel": 0, "pitch": 70, "start_ms": 20, "end_ms": 1000},
            ],
        )
        self.assertEqual(result["errors"], [])

    def test_same_timestamp_input_order_is_kept(self):
        # 同一时刻:先抬踏板截断待释放音,再按下新音
        result = render(
            1000,
            [
                ev(0, 0, "note_on", 60),
                ev(5, 0, "pedal_down"),
                ev(10, 0, "note_off", 60),
                ev(20, 0, "pedal_up"),
                ev(20, 0, "note_on", 60),  # 同时刻,按输入顺序新起一个音
            ],
        )
        self.assertEqual(
            result["intervals"],
            [
                {"channel": 0, "pitch": 60, "start_ms": 0, "end_ms": 20},
                {"channel": 0, "pitch": 60, "start_ms": 20, "end_ms": 1000},
            ],
        )

    def test_empty_events(self):
        result = render(0, [])
        self.assertEqual(result, {"intervals": [], "errors": []})

    def test_zero_length_interval(self):
        result = render(
            100,
            [ev(10, 0, "note_on", 60), ev(10, 0, "note_off", 60)],
        )
        self.assertEqual(
            result["intervals"],
            [{"channel": 0, "pitch": 60, "start_ms": 10, "end_ms": 10}],
        )

    def test_unpaired_note_off_while_pedal_held(self):
        # 踏板踩着也不能凭空配对松键
        result = render(
            100,
            [ev(0, 0, "pedal_down"), ev(10, 0, "note_off", 60)],
        )
        self.assertEqual(
            result["errors"], [{"index": 1, "reason": UNPAIRED_NOTE_OFF}]
        )

    def test_sostenuto_locks_only_currently_held_notes(self):
        # 踩选择性踏板时 A 仍按住 -> 锁定;之后新按的 B 不锁定
        result = render(
            1000,
            [
                ev(0, 0, "note_on", 60),   # A
                ev(100, 0, "sostenuto_down"),
                ev(150, 0, "note_on", 64),  # B:踩下之后新按,不锁定
                ev(200, 0, "note_off", 60),  # A:锁定 -> 待释放
                ev(250, 0, "note_off", 64),  # B:未锁定且无其他踏板 -> 立即结束
                ev(400, 0, "sostenuto_up"),  # A 截断于 400
            ],
        )
        self.assertEqual(
            result["intervals"],
            [
                {"channel": 0, "pitch": 60, "start_ms": 0, "end_ms": 400},
                {"channel": 0, "pitch": 64, "start_ms": 150, "end_ms": 250},
            ],
        )
        self.assertEqual(result["errors"], [])

    def test_sostenuto_does_not_lock_already_released_notes(self):
        # 松键后才踩选择性踏板:已结束的音不会被锁定
        result = render(
            1000,
            [
                ev(0, 0, "note_on", 60),
                ev(50, 0, "note_off", 60),  # 立即结束于 50
                ev(100, 0, "sostenuto_down"),
                ev(200, 0, "sostenuto_up"),
            ],
        )
        self.assertEqual(
            result["intervals"],
            [{"channel": 0, "pitch": 60, "start_ms": 0, "end_ms": 50}],
        )

    def test_sostenuto_note_still_held_at_up_keeps_ringing(self):
        # 锁定期间始终没松键,抬踏板不影响该音(键还按着)
        result = render(
            1000,
            [
                ev(0, 0, "note_on", 60),
                ev(100, 0, "sostenuto_down"),
                ev(200, 0, "sostenuto_up"),
                ev(300, 0, "note_off", 60),  # 此时踏板已抬起 -> 立即结束
            ],
        )
        self.assertEqual(
            result["intervals"],
            [{"channel": 0, "pitch": 60, "start_ms": 0, "end_ms": 300}],
        )

    def test_sostenuto_locked_until_end_ms(self):
        result = render(
            1000,
            [
                ev(0, 0, "note_on", 60),
                ev(100, 0, "sostenuto_down"),
                ev(200, 0, "note_off", 60),  # 锁定 -> 延续
                # 选择性踏板一直不抬:到 end_ms 截断
            ],
        )
        self.assertEqual(
            result["intervals"],
            [{"channel": 0, "pitch": 60, "start_ms": 0, "end_ms": 1000}],
        )

    def test_sostenuto_fifo_with_repeated_same_pitch(self):
        # 两个同音高仍按住的音都被锁定,松键按 FIFO 配对
        result = render(
            1000,
            [
                ev(0, 0, "note_on", 60),
                ev(50, 0, "note_on", 60),
                ev(100, 0, "sostenuto_down"),
                ev(200, 0, "note_off", 60),  # FIFO 配对 0ms 的音 -> 锁定待释放
                ev(250, 0, "note_off", 60),  # 配对 50ms 的音 -> 锁定待释放
                ev(400, 0, "sostenuto_up"),  # 两音同时截断于 400
            ],
        )
        self.assertEqual(
            result["intervals"],
            [
                {"channel": 0, "pitch": 60, "start_ms": 0, "end_ms": 400},
                {"channel": 0, "pitch": 60, "start_ms": 50, "end_ms": 400},
            ],
        )

    def test_both_pedals_sustain_up_then_sostenuto_up(self):
        # 两踏板同踩时松键 -> 共同保持;先抬普通踏板不截断,再抬选择性踏板才结束
        result = render(
            1000,
            [
                ev(0, 0, "note_on", 60),
                ev(50, 0, "pedal_down"),
                ev(100, 0, "sostenuto_down"),
                ev(200, 0, "note_off", 60),
                ev(400, 0, "pedal_up"),       # 仍被选择性踏板保持,不截断
                ev(600, 0, "sostenuto_up"),   # 截断于 600
            ],
        )
        self.assertEqual(
            result["intervals"],
            [{"channel": 0, "pitch": 60, "start_ms": 0, "end_ms": 600}],
        )

    def test_both_pedals_sostenuto_up_first(self):
        # 反向:先抬选择性踏板,仍由普通延音踏板保持
        result = render(
            1000,
            [
                ev(0, 0, "note_on", 60),
                ev(50, 0, "pedal_down"),
                ev(100, 0, "sostenuto_down"),
                ev(200, 0, "note_off", 60),
                ev(400, 0, "sostenuto_up"),  # 仍被普通踏板保持,不截断
                ev(600, 0, "pedal_up"),      # 截断于 600
            ],
        )
        self.assertEqual(
            result["intervals"],
            [{"channel": 0, "pitch": 60, "start_ms": 0, "end_ms": 600}],
        )

    def test_sostenuto_sustained_then_sustain_takes_over(self):
        # 仅靠选择性踏板延续的音,之后踩下普通延音踏板也被接管;
        # 先抬选择性踏板不截断,抬普通踏板才结束
        result = render(
            1000,
            [
                ev(0, 0, "note_on", 60),
                ev(50, 0, "sostenuto_down"),
                ev(100, 0, "note_off", 60),  # 仅选择性踏板保持
                ev(200, 0, "pedal_down"),    # 普通延音踏板接管
                ev(300, 0, "sostenuto_up"),  # 仍由普通踏板保持,不截断
                ev(500, 0, "pedal_up"),      # 截断于 500
            ],
        )
        self.assertEqual(
            result["intervals"],
            [{"channel": 0, "pitch": 60, "start_ms": 0, "end_ms": 500}],
        )

    def test_sostenuto_mixed_locked_and_sustain_pending(self):
        # 普通延音先踩:松键 A 待释放(仅普通踏板);
        # 再踩选择性踏板时按住的 B 被锁定;
        # 松键 B 时两踏板共同保持;抬普通踏板只截断 A,B 保留到选择性踏板抬起
        result = render(
            1000,
            [
                ev(0, 0, "note_on", 60),    # A
                ev(50, 0, "pedal_down"),
                ev(100, 0, "note_off", 60),  # A:仅普通踏板待释放
                ev(150, 0, "note_on", 64),   # B
                ev(200, 0, "sostenuto_down"),  # 锁定 B
                ev(250, 0, "note_off", 64),    # B:两踏板共同保持
                ev(400, 0, "pedal_up"),        # A 截断于 400;B 保留
                ev(600, 0, "sostenuto_up"),    # B 截断于 600
            ],
        )
        self.assertEqual(
            result["intervals"],
            [
                {"channel": 0, "pitch": 60, "start_ms": 0, "end_ms": 400},
                {"channel": 0, "pitch": 64, "start_ms": 150, "end_ms": 600},
            ],
        )

    def test_invalid_sostenuto_switches(self):
        result = render(
            1000,
            [
                ev(0, 0, "sostenuto_down"),
                ev(10, 0, "sostenuto_down"),  # 重复踩
                ev(20, 0, "sostenuto_up"),
                ev(30, 0, "sostenuto_up"),    # 未踩时抬
            ],
        )
        self.assertEqual(
            result["errors"],
            [
                {"index": 1, "reason": SOSTENUTO_ALREADY_DOWN},
                {"index": 3, "reason": SOSTENUTO_ALREADY_UP},
            ],
        )

    def test_repeated_sostenuto_down_does_not_relock_new_notes(self):
        # 重复踩下无效、不重新锁定:第一次踩下后新按的音,
        # 在(无效的)第二次踩下时不会被锁定
        result = render(
            1000,
            [
                ev(0, 0, "note_on", 60),
                ev(100, 0, "sostenuto_down"),
                ev(150, 0, "note_on", 64),
                ev(200, 0, "sostenuto_down"),  # 无效,不重新锁定
                ev(300, 0, "note_off", 64),    # 64 未锁定 -> 立即结束
                ev(400, 0, "note_off", 60),    # 60 仍锁定 -> 待释放
                ev(500, 0, "sostenuto_up"),
            ],
        )
        self.assertEqual(
            result["intervals"],
            [
                {"channel": 0, "pitch": 60, "start_ms": 0, "end_ms": 500},
                {"channel": 0, "pitch": 64, "start_ms": 150, "end_ms": 300},
            ],
        )
        self.assertEqual(
            result["errors"],
            [{"index": 3, "reason": SOSTENUTO_ALREADY_DOWN}],
        )

    def test_sostenuto_channels_independent(self):
        result = render(
            1000,
            [
                ev(0, 0, "note_on", 70),
                ev(0, 0, "sostenuto_down"),
                ev(0, 1, "note_on", 60),   # 通道1没踩任何踏板
                ev(10, 1, "note_off", 60),  # 立即截断
                ev(30, 0, "note_off", 70),  # 通道0锁定 -> 待释放
            ],
        )
        self.assertEqual(
            result["intervals"],
            [
                {"channel": 0, "pitch": 70, "start_ms": 0, "end_ms": 1000},
                {"channel": 1, "pitch": 60, "start_ms": 0, "end_ms": 10},
            ],
        )

    def test_sostenuto_same_timestamp_input_order(self):
        # 同一时刻:先松键(此时选择性踏板仍踩着 -> 延续),再抬选择性踏板截断
        result = render(
            1000,
            [
                ev(0, 0, "note_on", 60),
                ev(10, 0, "sostenuto_down"),
                ev(20, 0, "note_off", 60),
                ev(30, 0, "sostenuto_up"),
                ev(30, 0, "note_on", 60),  # 同刻输入顺序在后:新起的音到 end_ms
            ],
        )
        self.assertEqual(
            result["intervals"],
            [
                {"channel": 0, "pitch": 60, "start_ms": 0, "end_ms": 30},
                {"channel": 0, "pitch": 60, "start_ms": 30, "end_ms": 1000},
            ],
        )


class RenderStatsTests(unittest.TestCase):
    def test_total_duration_and_sustained(self):
        result = render_stats(
            1000,
            [
                ev(0, 0, "note_on", 60),
                ev(100, 0, "pedal_down"),
                ev(200, 0, "note_off", 60),  # 松键后靠踏板延续 600ms
                ev(800, 0, "pedal_up"),
            ],
        )
        self.assertEqual(
            result["intervals"],
            [{"channel": 0, "pitch": 60, "start_ms": 0, "end_ms": 800}],
        )
        self.assertEqual(
            result["stats"],
            [
                {
                    "channel": 0,
                    "total_duration_ms": 800,
                    "peak_polyphony": 1,
                    "peak_at_ms": 0,
                    "sustained_duration_ms": 600,
                }
            ],
        )

    def test_peak_polyphony_half_open_and_first_peak(self):
        # 音A [0,100),音B [100,200):端点相接不重叠,峰值恒为 1,首次在 0
        result = render_stats(
            300,
            [
                ev(0, 0, "note_on", 60),
                ev(100, 0, "note_off", 60),
                ev(100, 0, "note_on", 64),
                ev(200, 0, "note_off", 64),
            ],
        )
        stats = result["stats"][0]
        self.assertEqual(stats["peak_polyphony"], 1)
        self.assertEqual(stats["peak_at_ms"], 0)
        self.assertEqual(stats["total_duration_ms"], 200)

        # 真正重叠:0 起 A,50 起 B(峰值 2 首次在 50),100 同刻结束两者
        result = render_stats(
            200,
            [
                ev(0, 0, "note_on", 60),
                ev(50, 0, "note_on", 64),
                ev(100, 0, "note_off", 60),
                ev(100, 0, "note_off", 64),
                # 150 再起一个,峰值重新达到 2 不应改写首次峰值时刻
                ev(120, 0, "note_on", 60),
                ev(150, 0, "note_on", 72),
            ],
        )
        stats = result["stats"][0]
        self.assertEqual(stats["peak_polyphony"], 2)
        self.assertEqual(stats["peak_at_ms"], 50)
        self.assertEqual(stats["total_duration_ms"], 100 + 50 + 80 + 50)

    def test_zero_length_notes_excluded_from_peak(self):
        result = render_stats(
            100,
            [
                ev(10, 0, "note_on", 60),
                ev(10, 0, "note_off", 60),  # 零长度
                ev(10, 0, "note_on", 64),
                ev(10, 0, "note_off", 64),  # 零长度
            ],
        )
        stats = result["stats"][0]
        self.assertEqual(stats["total_duration_ms"], 0)
        self.assertEqual(stats["peak_polyphony"], 0)
        self.assertIsNone(stats["peak_at_ms"])
        self.assertEqual(stats["sustained_duration_ms"], 0)

    def test_zero_length_with_pedal_is_sustained(self):
        # 同刻松键本身零时长,但踏板踩着:按既有规则声音延续到抬踏板
        result = render_stats(
            100,
            [
                ev(0, 0, "pedal_down"),
                ev(10, 0, "note_on", 60),
                ev(10, 0, "note_off", 60),  # 松键后靠踏板延续到 50
                ev(50, 0, "pedal_up"),
            ],
        )
        self.assertEqual(
            result["intervals"],
            [{"channel": 0, "pitch": 60, "start_ms": 10, "end_ms": 50}],
        )
        stats = result["stats"][0]
        self.assertEqual(stats["total_duration_ms"], 40)
        self.assertEqual(stats["peak_polyphony"], 1)
        self.assertEqual(stats["peak_at_ms"], 10)
        self.assertEqual(stats["sustained_duration_ms"], 40)

    def test_stats_sorted_by_channel_and_independent(self):
        result = render_stats(
            1000,
            [
                ev(0, 2, "note_on", 60),
                ev(0, 1, "note_on", 60),
                ev(0, 1, "note_on", 64),
                ev(100, 1, "note_off", 60),
                ev(100, 1, "note_off", 64),
                ev(200, 2, "note_off", 60),
            ],
        )
        self.assertEqual([s["channel"] for s in result["stats"]], [1, 2])
        self.assertEqual(result["stats"][0]["peak_polyphony"], 2)
        self.assertEqual(result["stats"][0]["peak_at_ms"], 0)
        self.assertEqual(result["stats"][0]["total_duration_ms"], 200)
        self.assertEqual(result["stats"][1]["peak_polyphony"], 1)
        self.assertEqual(result["stats"][1]["peak_at_ms"], 0)
        self.assertEqual(result["stats"][1]["total_duration_ms"], 200)

    def test_sustained_until_end_ms_and_fifo_pairing(self):
        # 踏板一直不抬:待释放音与仍按住音都到 end_ms
        result = render_stats(
            1000,
            [
                ev(0, 0, "note_on", 60),
                ev(100, 0, "note_on", 60),
                ev(200, 0, "pedal_down"),
                ev(300, 0, "note_off", 60),  # FIFO 配对 0ms 的音:延续 700ms
                ev(400, 0, "note_off", 60),  # 配对 100ms 的音:延续 600ms
            ],
        )
        self.assertEqual(
            result["intervals"],
            [
                {"channel": 0, "pitch": 60, "start_ms": 0, "end_ms": 1000},
                {"channel": 0, "pitch": 60, "start_ms": 100, "end_ms": 1000},
            ],
        )
        stats = result["stats"][0]
        self.assertEqual(stats["total_duration_ms"], 1900)
        self.assertEqual(stats["sustained_duration_ms"], 1300)
        self.assertEqual(stats["peak_polyphony"], 2)
        self.assertEqual(stats["peak_at_ms"], 100)

    def test_semantic_errors_do_not_change_stats(self):
        # 重复踩/抬踏板、松键多于按键:有效事件的统计不受影响
        result = render_stats(
            1000,
            [
                ev(0, 0, "pedal_down"),
                ev(10, 0, "pedal_down"),  # 无效
                ev(20, 0, "note_on", 60),
                ev(30, 0, "note_off", 60),  # 踏板延续
                ev(40, 0, "note_off", 60),  # 无法配对
                ev(50, 0, "pedal_up"),
                ev(60, 0, "pedal_up"),      # 无效
            ],
        )
        self.assertEqual(
            result["errors"],
            [
                {"index": 1, "reason": PEDAL_ALREADY_DOWN},
                {"index": 4, "reason": UNPAIRED_NOTE_OFF},
                {"index": 6, "reason": PEDAL_ALREADY_UP},
            ],
        )
        self.assertEqual(
            result["stats"],
            [
                {
                    "channel": 0,
                    "total_duration_ms": 30,
                    "peak_polyphony": 1,
                    "peak_at_ms": 20,
                    "sustained_duration_ms": 20,
                }
            ],
        )

    def test_no_note_on_yields_no_stats(self):
        # 只有踏板事件的通道不算“出现过按键”
        result = render_stats(
            100,
            [
                ev(0, 0, "pedal_down"),
                ev(10, 0, "pedal_up"),
                ev(20, 0, "sostenuto_down"),
                ev(30, 0, "sostenuto_up"),
            ],
        )
        self.assertEqual(result["stats"], [])

    def test_sostenuto_sustained_duration(self):
        # 松键后仅靠选择性踏板延续:sustained = 600 - 200 = 400
        result = render_stats(
            1000,
            [
                ev(0, 0, "note_on", 60),
                ev(100, 0, "sostenuto_down"),
                ev(200, 0, "note_off", 60),
                ev(600, 0, "sostenuto_up"),
            ],
        )
        stats = result["stats"][0]
        self.assertEqual(stats["total_duration_ms"], 600)
        self.assertEqual(stats["sustained_duration_ms"], 400)
        self.assertEqual(stats["peak_polyphony"], 1)
        self.assertEqual(stats["peak_at_ms"], 0)

    def test_sostenuto_sustained_until_end_ms(self):
        # 选择性踏板不抬:延续部分计到 end_ms(1000 - 200 = 800)
        result = render_stats(
            1000,
            [
                ev(0, 0, "note_on", 60),
                ev(100, 0, "sostenuto_down"),
                ev(200, 0, "note_off", 60),
            ],
        )
        stats = result["stats"][0]
        self.assertEqual(stats["total_duration_ms"], 1000)
        self.assertEqual(stats["sustained_duration_ms"], 800)

    def test_sustained_duration_with_both_pedals(self):
        # 两踏板共同保持:sustained 只按松键 -> 实际结束计一次(600-200=400),
        # 不因两块踏板重复计算
        result = render_stats(
            1000,
            [
                ev(0, 0, "note_on", 60),
                ev(50, 0, "pedal_down"),
                ev(100, 0, "sostenuto_down"),
                ev(200, 0, "note_off", 60),
                ev(400, 0, "pedal_up"),
                ev(600, 0, "sostenuto_up"),
            ],
        )
        stats = result["stats"][0]
        self.assertEqual(stats["total_duration_ms"], 600)
        self.assertEqual(stats["sustained_duration_ms"], 400)

    def test_unlocked_note_off_under_sostenuto_is_not_sustained(self):
        # 踩选择性踏板后新按的音松键即结束:不计 sustained
        result = render_stats(
            1000,
            [
                ev(0, 0, "note_on", 60),
                ev(50, 0, "sostenuto_down"),
                ev(100, 0, "note_on", 64),
                ev(200, 0, "note_off", 64),  # 未锁定,立即结束
                ev(300, 0, "note_off", 60),  # 锁定,延续到 500
                ev(500, 0, "sostenuto_up"),
            ],
        )
        stats = result["stats"][0]
        self.assertEqual(stats["total_duration_ms"], 500 + 100)
        self.assertEqual(stats["sustained_duration_ms"], 200)

    def test_empty_events(self):
        result = render_stats(0, [])
        self.assertEqual(
            result, {"intervals": [], "errors": [], "stats": []}
        )


if __name__ == "__main__":
    unittest.main()
