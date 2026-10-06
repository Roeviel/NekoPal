"""配置加载：defaults <- config.json <- 环境变量。

设计约定（其他模块都依赖这里）：
    from neko.config import load_config, ROOT, resolve_path
    cfg = load_config()                      # dict
    cfg["llm"]["api_key"]                    # 也可能是环境变量 DEEPSEEK_API_KEY
    resolve_path(cfg["memory"]["db_path"])   # 相对路径 -> 绝对路径
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

# 项目根目录：NekoPal/
ROOT = Path(__file__).resolve().parent.parent

CONFIG_PATH = ROOT / "config.json"
EXAMPLE_PATH = ROOT / "config.example.json"

DEFAULTS: dict[str, Any] = {
    "llm": {
        # 总开关：关掉就**完全不调用大模型**，她会走离线兜底回复。
        # 配合 budget 的三道闸一起用 —— 这是最彻底的那一道（零 API 调用）。
        "enabled": True,
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "api_key": "",
        "temperature": 1.1,
        "max_tokens": 1024,
        "timeout": 90,
    },
    "persona": {
        "name": "小喵",
        "call_user": "主人",
        "style": "软萌甜妹",
        "keywords": ["温柔", "黏人", "好奇", "认真"],
        "meow": True,
        "custom_prompt": "",
    },
    "voice": {
        "enabled": True,
        # 回复时自动朗读。**这个必须存下来。**
        # 踩过的坑：它以前只是 HTML 里写死的 `checked`，没有任何持久化，
        # 于是每次加载都被拉回"开"，用户关了它下次又自己开了（"朗读键莫名开启"）。
        "auto_read": True,
        "engine": "edge-tts",
        # 底声。Xiaoxiao 是 Warm 系，比 Lively 的 Xiaoyi 低更稳，
        # 更贴合「清冷智者」的人设；Xiaoyi 底声已达 296Hz，再变调容易变童声。
        "azure_voice": "zh-CN-XiaoxiaoNeural",
        "rate": "+0%",
        "volume": "+0%",
        # 实测：Xiaoxiao 原声 F0≈235Hz，+2 半音到 264Hz，落在「偏少女但自然」的区间。
        # 之前 Xiaoyi +5 会顶到 393Hz，听起来像童声，和人设冲突。
        "pitch_semitones": 2.0,
        "formant_shift": 0.12,
        "reverb": 0.0,
        "gain_db": 0.0,
        "cache_dir": "data/voice_cache",
        "max_chars": 400,
        # **RVC 变声**（可选）：把 edge-tts 合成的底声换成用模型训练出来的真人音色。
        # 模型和整合包（5.7GB 运行时）都在项目外面，所以这里存的是**绝对路径**，
        # 由使用者自己在 config.json 里填 —— 默认留空 = 不启用。
        # 填好之后每句话会多等约 1 倍音频时长（纯 CPU 推理），
        # 嫌慢可以在设置页一键关掉。
        "rvc": {
            "enabled": False,
            # 整合包目录（里面要有 runtime\python.exe，形如 RVC20240604AMD_Intel）
            "dir": "",
            # 当前用哪个音色（对应下面 voices 里的 name）
            "voice": "",
            # f0 提取方法。rmvpe 质量最好（作者机器上 DirectML 会撞
            # PrivateUse1 调度错误，所以实际只能用 CPU 跑）。
            "f0_method": "rmvpe",
            "timeout_sec": 120,
            # 音色列表，示例：
            #   [{"name": "温柔御姐", "model": r"D:\RVC\models\wenrou.pth", "pitch": 0},
            #    {"name": "带索引的音色", "model": r"D:\RVC\models\x.pth",
            #     "index": r"D:\RVC\models\x.index", "pitch": 0}]
            "voices": [],
        },
    },
    "bilibili": {
        "cookie": "",
        # 学习主题尽量铺开：技术只是一部分，数学/哲学/情感这些也该被她刷到
        "topics": [
            "Python 编程", "高等数学", "线性代数", "概率论",
            "哲学入门", "逻辑学", "心理学",
            "亲密关系与爱情", "沟通与表达",
            "机器学习入门", "经济学思维", "科学史",
            "写作技巧", "视频剪辑技巧",
        ],
        "daily_limit": 3,
        "subtitle_priority": ["zh-CN", "zh-Hans", "zh-Hant", "ai-zh"],
        "search_limit": 20,
        "subtitle_try": 5,
        # 未登录拿不到 AI 字幕，但弹幕和评论区是开放的且内容很实，
        # 所以把它们作为"观众视角"内容源（会如实标注来源，不当成视频原话）
        "use_audience": True,
        "danmaku_limit": 300,
        "comment_limit": 20,
    },
    "wechat": {
        "enabled": True,
        "listen": ["文件传输助手"],
        "reply_target": "文件传输助手",
        "poll_interval": 2.0,
        "send_voice": True,
        "quiet_hours": [],
        "echo_self": False,
    },
    # 外观：头像与聊天背景。值为 data/assets 下的文件名，空字符串表示用内置默认。
    "appearance": {
        "neko_avatar": "",
        "user_avatar": "",
        "chat_background": "",
        # wechat = 仿微信浅色；dark = 原来的深色主题
        "theme": "wechat",
        # 自定义背景的显示强度（0~1），太花的图调低更好读
        "background_opacity": 1.0,
    },
    "memory": {
        "db_path": "data/neko.db",
        "recent_messages": 20,
        # 存档目录：对话 / 记忆 / 笔记整体快照到这里，可随时读回来（类似游戏存档）
        "saves_dir": "data/saves",
        # 哪些时机**自动**存档。默认只保留"关闭程序时"这一种 ——
        # 启动/清除前/读档前都关掉了，因为它们的备份堆在列表里很吵，
        # 而用户真正想要的是"上次关掉之前是什么样"。
        # ⚠ 关掉 before_clear 之后，"清除数据"就没有退路了，删掉就真没了。
        "auto_save": {
            "on_quit": True,        # 关闭程序时（滚动覆盖「最近-退出」，不会堆积）
            "on_start": False,      # 启动时
            "before_clear": False,  # 任何清除动作之前
        },
    },
    "music": {
        # 一起听音乐：扫码登录网易云 -> 读听歌记录 -> 她挑歌、评论、记下来
        "enabled": True,
        # 挑完歌只说一句邀请 + 问要不要讲（**不要长篇评论**）
        "invite_tokens": 80,
        # 用户点头后展开讲的长度
        "explain_tokens": 400,
        # 登录二维码有效期（秒），过期前端提示刷新
        "qr_ttl_sec": 240,
        # **听歌倾向**：她挑歌时会参考这些，用户也能对每首歌点"喜欢/不喜欢"来养它。
        # likes/dislikes 存的是歌手或风格关键词（不区分大小写，子串匹配）。
        "taste": {
            "likes": [],
            "dislikes": [],
            # 优先从"最近在听"里挑（那才是用户真实的口味），而不是随机搜
            "prefer_history": True,
        },
    },
    "schedule": {
        # 默认开启：她的核心价值就是"自己会学"，默认关着等于把这功能藏起来了
        "auto_study": True,
        # 固定时刻（每天）
        "times": ["12:30", "21:00"],
        # 运行期间每隔 N 小时再学一次；0 = 关闭。
        # 加这个是因为"每天两个固定时刻"太脆：那个点没开着软件就等于没学。
        "every_hours": 4,
        # 启动后如果今天还没学过，延迟一会儿补一次
        "catch_up": True,
        "startup_delay": 120,
    },
    # 语音识别（没有字幕时直接听视频的声音）
    "asr": {
        "enabled": True,
        # base 实测 26.5x 实时（20 分钟视频约 48 秒），small 是 9.7x（约 2 分钟）。
        # 准确度差距不大，所以默认 base；想更准可以改 small/medium。
        "model": "base",
        # 最多识别前多少秒。控制等待时间：base 下 300 秒音频约 12 秒转完。
        "max_seconds": 300,
        "language": "zh",
        "initial_prompt": "以下是普通话的课程讲解内容，可能有专业术语。",
        "model_dir": "data/asr_models",
    },
    # 设备控制（桌面机器人 / 智能家居）
    # 蕴不直接碰设备，只把动作交给网关；协议适配（MQTT/HTTP/串口）在网关里做。
    "devices": {
        "enabled": False,
        # 网关地址。网关负责真正的协议适配，收到 POST /action {device, action}
        "gateway": "http://127.0.0.1:8791",
        # 蕴调用网关时带的 token（网关那边校验）
        "token": "",
        "timeout": 6.0,
        # 白名单：**只执行登记过的设备和动作**。
        # 为什么必须有：LLM 会幻觉出你根本没有的设备（"打开客厅的加湿器"）。
        # actions 可以写 ["开","关"]，也可以写 {"开":"light.on"} 映射成网关命令。
        # dangerous=true 的动作不直接执行，要先让你确认。
        "list": [
            {
                "id": "desk_light", "name": "台灯", "kind": "light",
                "actions": {"开": "light.on", "关": "light.off"},
                "dangerous": False,
            },
            {
                "id": "door_lock", "name": "门锁", "kind": "lock",
                "actions": {"开": "lock.unlock", "关": "lock.lock"},
                "dangerous": True,
            },
        ],
    },
    # 用量与预算：实时看余额 + 给自动化调用上闸。
    # 起因是一次真实事故：自检脚本反复调 /api/chat，14 次全花 token，用户事后才知道。
    "budget": {
        "enabled": True,
        # 聊天头部那行小字（已消耗金额）显不显示。设置页里有开关。
        "show": True,
        # 每日花费上限（元）。**按"蕴自己调用的 token"估算**，不是余额差 ——
        # 余额差会把同一个 key 在别处的消耗也算进来（用户反馈过这个问题）。
        "daily_cny": 2.0,
        # 单价（元 / 百万 tokens）。默认是 DeepSeek 的公开价，可自行改。
        # 估算终究是估算：真正扣了多少钱以账单为准。
        "price_in_cache_hit": 0.5,    # 输入·缓存命中
        "price_in_cache_miss": 2.0,   # 输入·缓存未命中
        "price_out": 8.0,             # 输出
        # 闸 1：同一句话在这个窗口内出现第 N+1 次就不再调模型
        "duplicate_window_sec": 600,
        "duplicate_max": 3,
        # 闸 2：每分钟最多调用几次
        "max_per_minute": 12,
        # 余额缓存时长（秒），界面刷新不会每次都打接口
        "balance_ttl_sec": 120,
    },
    "server": {
        "host": "127.0.0.1",
        "port": 8790,
    },
    # 轮式机器人运动控制。和 devices 分开：移动的机器出事代价大得多，
    # 而且带参数（速度/时长/距离），需要独立的限幅逻辑。
    "motion": {
        "enabled": False,
        # 机器人（或其网关）地址；留空则回落到 devices.gateway
        "gateway": "",
        "token": "",
        # ---- 三条安全限幅。**任何一条越界都会被截断，并如实告知用户。** ----
        # 速度上限（0-100 的百分比）
        "max_speed": 70,
        # 单条指令的时长上限（秒）。想走更远就分几次发 —— 累加出来的距离
        # 是可审计的，而"一直往前开"不是。
        "max_duration": 3.0,
        # 单条指令的距离上限（米），只在机器人支持按距离走时用
        "max_distance": 3.0,
        # 她只说"前进"时的缺省值
        "default_speed": 50,
        "default_duration": 1.0,
        # 无障碍确认时的最大速度（更保守）
        "blind_max_speed": 35,
    },
    "window": {
        # 受限的 Windows 会话里 Chromium 渲染进程沙箱会初始化失败、导致窗口空白，
        # 所以默认关闭沙箱。窗口只加载本地页面，这个取舍可接受。
        # 想恢复 Chromium 沙箱就设成 false（或设环境变量 NEKOPAL_DISABLE_SANDBOX=0）。
        "disable_sandbox": True,
    },
    # 资源目录（头像、背景图）
    "assets_dir": "data/assets",
    # 表情包：用户自己导入 + 网络搜索
    "stickers": {
        "user_dir": "data/stickers",   # 用户导入的表情包放这里
        "search": True,
        "count": 12,
        "cache_dir": "data/stickers_cache",
        # 联网搜索时实际发出去的检索式前缀。为什么必须补这一句：
        # 实测「猫猫震惊」搜到的是**相机拍的真猫照片**，「猫娘」搜到的是**壁纸**，
        # 只有加上"动漫 + q版 + 表情包"才会出 Q 版猫娘表情包。
        # 设成空字符串就用她的原词（想自己控制时用）。
        "search_prefix": "动漫猫娘 表情包 可爱 q版",
    },
}

# 环境变量覆盖表：环境变量名 -> 在配置里的路径
ENV_OVERRIDES: dict[str, tuple[str, ...]] = {
    "DEEPSEEK_API_KEY": ("llm", "api_key"),
    "NEKO_LLM_BASE_URL": ("llm", "base_url"),
    "NEKO_LLM_MODEL": ("llm", "model"),
    "BILIBILI_COOKIE": ("bilibili", "cookie"),
    "NEKO_WECHAT_TARGET": ("wechat", "reply_target"),
}


def _deep_merge(base: dict, patch: dict) -> dict:
    """递归合并：patch 覆盖 base，dict 递归，list/标量整体替换。"""
    out = copy.deepcopy(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def resolve_path(value: str | os.PathLike[str]) -> Path:
    """把配置里的相对路径解析为相对项目根目录的绝对路径。"""
    path = Path(value)
    return path if path.is_absolute() else (ROOT / path)


def load_config(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """读取配置。文件不存在时使用默认值 + 环境变量，不抛异常。"""
    cfg = copy.deepcopy(DEFAULTS)

    config_file = Path(path) if path else CONFIG_PATH
    if config_file.exists():
        try:
            raw = json.loads(config_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cfg = _deep_merge(cfg, raw)
        except (json.JSONDecodeError, OSError) as exc:  # 配置坏了也要能起来
            print(f"[config] 读取 {config_file} 失败，改用默认配置：{exc}")

    for env_name, keys in ENV_OVERRIDES.items():
        value = os.environ.get(env_name)
        if not value:
            continue
        node = cfg
        for key in keys[:-1]:
            node = node.setdefault(key, {})
        node[keys[-1]] = value

    # 解析所有输出类路径，避免各处再拼
    cfg["voice"]["cache_dir"] = str(resolve_path(cfg["voice"]["cache_dir"]))
    cfg["memory"]["db_path"] = str(resolve_path(cfg["memory"]["db_path"]))
    cfg["assets_dir"] = str(resolve_path(cfg["assets_dir"]))
    cfg.setdefault("stickers", {})["cache_dir"] = str(resolve_path(cfg["stickers"]["cache_dir"]))
    cfg["_root"] = str(ROOT)
    return cfg


def save_config(cfg: dict[str, Any], path: str | os.PathLike[str] | None = None) -> Path:
    """写回 config.json（去掉内部键）。"""
    config_file = Path(path) if path else CONFIG_PATH
    payload = {k: v for k, v in cfg.items() if not k.startswith("_")}
    # 路径还原成相对形式，保持文件可读
    for section, key in (("voice", "cache_dir"), ("memory", "db_path")):
        value = payload.get(section, {}).get(key)
        if value:
            try:
                payload[section][key] = str(Path(value).relative_to(ROOT)).replace("\\", "/")
            except ValueError:
                pass
    config_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return config_file


def ensure_config_file() -> Path:
    """首次运行时生成 config.json（从 example 复制，或从 defaults 生成）。"""
    if CONFIG_PATH.exists():
        return CONFIG_PATH
    if EXAMPLE_PATH.exists():
        CONFIG_PATH.write_text(EXAMPLE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        return CONFIG_PATH
    return save_config(load_config())


def missing_key_hint(cfg: dict[str, Any]) -> str | None:
    """检查关键配置是否缺失，返回给用户看的提示。"""
    if not (cfg.get("llm", {}).get("api_key") or "").strip():
        return (
            "还没配置 DeepSeek API Key。请编辑 "
            f"{CONFIG_PATH} 里的 llm.api_key，"
            "或设置环境变量 DEEPSEEK_API_KEY。"
        )
    return None


if __name__ == "__main__":  # 自检：python -m neko.config
    c = load_config()
    print(json.dumps(c, ensure_ascii=False, indent=2))
    print("缺失关键配置：", missing_key_hint(c))
