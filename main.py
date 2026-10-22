import time
import re

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Star, Context

# ==============================
# 睡觉模拟插件
#
# 状态机：awake →(哄睡)→ sleepy →(彻底)→ asleep
#   - sleepy   迷迷糊糊/瞌睡：回复变少、变迷糊
#   - asleep   睡着：不回应普通消息，只有被连续 @好几条才被吵醒
#   - awake   醒着
#
# 唤醒：用户连发 N 条消息 → 被吵醒；或说「起床/醒醒/别睡啦」直接唤醒。
# ==============================


class SleepPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 每个会话独立的状态机：session_id -> session state dict
        self.states = {}
        # 可配置项
        self.wake_msg_count = int(getattr(config, "wake_msg_count", 3))   # 连续几条被吵醒
        self.wake_interval = float(getattr(config, "wake_interval", 10))  # 多少秒内算"连续"发送
        logger.info("[睡觉] 睡觉模拟插件已加载。连发%d条会被吵醒，间隔窗口%.0fs。", self.wake_msg_count, self.wake_interval)

    # ---------- 内部工具 ----------
    def _session_state(self, session_id: str) -> dict:
        if session_id not in self.states:
            self.states[session_id] = {
                "state": "awake",
                "last_msg_time": 0.0,
                "msg_times": [],      # 最近 n 条消息的时间戳
            }
        return self.states[session_id]

    def _set_state(self, session_id: str, state: str):
        self.states[session_id]["state"] = state

    # 哄睡话术（命中即进入 sleepy）
    HUSH_PATTERNS = [
        r"快去睡觉", r"快点睡觉", r"去睡觉", r"快睡觉", r"该睡觉",
        r"睡觉吧", r"睡吧", r"快睡", r"早点睡", r"晚安", r"好晚了",
        r"困不困", r"该睡了", r"睡觉了", r"帮我睡觉", r"别熬夜",
    ]
    # 彻底睡着话术（从 sleepy 加强到 asleep）
    DEEP_PATTERNS = [
        r"睡着了吗", r"已睡", r"睡着了", r"快去睡", r"怎么还不睡",
    ]
    # 唤醒话术
    WAKE_PATTERNS = [
        r"起床", r"醒醒", r"别睡了", r"起来", r"亮天了", r"早上好", r"别睡啦",
        r"给我起来", r"该起床", r"睡醒了", r"太阳晒屁股",
    ]

    def _match(self, patterns, text):
        for p in patterns:
            if re.search(p, text):
                return True
        return False

    def _record_msg(self, st: dict, now: float):
        st["msg_times"].append(now)
        st["last_msg_time"] = now
        # 只保留窗口内的
        st["msg_times"] = [t for t in st["msg_times"] if now - t <= self.wake_interval]

    def _count_recent(self, st: dict, now: float) -> int:
        return sum(1 for t in st["msg_times"] if now - t <= self.wake_interval)

    # ---------- 对话唤醒词（命令式，优先级高于状态） ----------
    @filter.command("睡觉")
    async def cmd_sleep(self, event: AstrMessageEvent):
        session_id = event.get_session_id()
        self._set_state(session_id, "asleep")
        yield event.plain_result("呼…那我先去睡觉啦，有事……轻点喊我……(打哈欠)")

    @filter.command("起床")
    async def cmd_wake(self, event: AstrMessageEvent):
        session_id = event.get_session_id()
        if self._session_state(session_id)["state"] != "awake":
            self._set_state(session_id, "awake")
            yield event.plain_result("唔…早上好呀！我刚醒，睡得好舒服～(伸懒腰)")
        else:
            yield event.plain_result("我一直醒着呢，没在睡哦")

    # ---------- 主逻辑：拦截全部消息 ----------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        try:
            session_id = event.get_session_id()
        except Exception:
            session_id = "default"

        st = self._session_state(session_id)
        state = st["state"]
        now = time.time()
        text = (event.get_message_str() or "").strip()

        # 只有非空文本消息才参与状态机
        if not text:
            return

        self._record_msg(st, now)

        # 唤醒词：任何状态都处理
        if self._match(self.WAKE_PATTERNS, text):
            if state != "awake":
                self._set_state(session_id, "awake")
                yield event.plain_result(self._random_wake())
            return

        if state == "asleep":
            # 命令消息（/开头）不吞，放行给命令处理器（如 /找深深、/cu）
            if text.startswith("/"):
                return
            # 睡着状态：连发多条被吵醒，否则不响应（吞消息，不触发 LLM）
            recent = self._count_recent(st, now)
            if recent >= self.wake_msg_count:
                self._set_state(session_id, "awake")
                st["msg_times"] = []  # 重置计数
                yield event.plain_result(self._random_wake(loud=True))
            else:
                # 不回复，吞掉这条消息，避免触发默认 LLM
                event.should_call_llm(False)
                event.stop_event()
            return

        if state == "sleepy":
            # 命令消息放行
            if text.startswith("/"):
                return
            # 迷糊状态：偶尔搭一句，然后慢慢彻底睡着
            if self._match(self.HUSH_PATTERNS, text):
                self._set_state(session_id, "asleep")
                yield event.plain_result(self._random_sleep())
                return
            # 迷糊时对普通消息回句梦话，但不再深入对话
            recent = self._count_recent(st, now)
            if recent <= 1:
                yield event.plain_result(self._random_sleepy())
            else:
                # 被多问几句就彻底睡着
                self._set_state(session_id, "asleep")
                yield event.plain_result(self._random_sleep())
            return

        # awake 状态：检测哄睡话术
        if self._match(self.DEEP_PATTERNS, text) or self._match(self.HUSH_PATTERNS, text):
            self._set_state(session_id, "sleepy")
            yield event.plain_result(self._random_sleepy())
            return

    # ---------- 随机回复池 ----------
    def _random_sleepy(self) -> str:
        import random
        return random.choice([
            "嗯……好困啊……(眼皮打架)",
            "唔…你说什么……我要撑不住啦……",
            "哈欠……我好像迷迷糊糊的了……",
            "别闹……让我眯一会儿……",
            "呼…呼…你说啥……我听不清……",
        ])

    def _random_sleep(self) -> str:
        import random
        return random.choice([
            "呼……(安稳地睡着了)",
            "zzz…Zzz…",
            "(彻底睡着了，只剩轻轻的呼吸声)",
            "唔……好梦……Zzz",
        ])

    def _random_wake(self, loud: bool = False) -> str:
        import random
        if loud:
            return random.choice([
                "唔！谁啊！吵醒我了！(睡眼惺忪地瞪过来)",
                "干嘛呀……人家睡得好好的……(揉眼睛)",
                "呜……被吵醒了！怎么回事！",
                "……(猛然睁眼) 谁在一直发消息！",
            ])
        return random.choice([
            "唔…早上好呀！(伸懒腰)",
            "啊，我醒了！睡饱了超舒服～",
            "嗯？天亮啦？那我起来啦！",
        ])