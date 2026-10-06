# 蕴 · NekoPal

**English** | [中文](README.md)

> A local-first desktop companion ("猫娘伴友"): she chats with you, **teaches herself from Bilibili**,
> tells you what she learned, and **can actually speak** — edge-tts, optionally re-voiced with RVC.

NekoPal is a Windows desktop app that puts an AI companion in a WeChat-like window.
Everything runs on your own machine: your API key, your SQLite memory, your voice cache, your browser profile.
There is no server of ours and no telemetry.

---

## What she does

| | |
|---|---|
| **Chat** | Streaming replies from DeepSeek (or any OpenAI-compatible endpoint), with a configurable persona: name, how she addresses you, 4 style presets (清冷智者 / 软萌甜妹 / 温柔学姐 / 毒舌傲娇), keywords, and a free-form prompt |
| **Memory** | SQLite store of messages + extracted long-term memories, searchable from the chat search box |
| **Self-study** | On a schedule (default 12:30 / 21:00, plus every N hours) she picks a video from your topic list, pulls **subtitles if you're logged in**, otherwise falls back to **danmaku + comments** ("audience view"), distils 3+ knowledge points into a note, then quizzes you |
| **Voice** | `edge-tts` base voice → per-sentence prosody (rate/pitch/volume follow the detected emotion) → optional **DSP** styling reused from the separate VoiceChanger project → optional **RVC** voice conversion to a voice model you provide. Heavy caching; a master on/off switch; when off, she is text-only and the 「朗读」 button disappears from the chat header |
| **Music** | Log into NetEase Cloud Music / QQ Music by QR code, she reads your listening history, picks a song, and tells you something about it (taste learning: 👍/👎 per song) |
| **Notes** | Every study session becomes a note with a **content-level badge** (subtitles / audience view / description only / title only) so you know how trustworthy it is |
| **Saves** | Game-save-like snapshots of conversation + memory + notes; auto-snapshot on quit, resume on start |
| **Devices** | Optional HTTP gateway for a desktop robot / smart home: **whitelisted devices and actions only**, dangerous actions require confirmation |
| **Motion** | Wheeled-robot commands with hard caps on speed / duration / distance |
| **Budget** | Daily ¥ cap, duplicate-message guard, per-minute rate limit, live balance readout |

![Settings page — sections are foldable](docs/界面预览.png)

## Quick start (Windows)

```powershell
git clone https://github.com/Roeviel/NekoPal.git
cd NekoPal
```

1. Double-click **`安装依赖.bat`** — creates `.venv`, installs dependencies, optionally creates a desktop shortcut.
2. It will copy `config.example.json` → `config.json`. Put your `llm.api_key` in there
   (works without a key too — she falls back to offline canned replies).
3. Double-click **`启动蕴.vbs`** — starts the server and opens the app window.

Diagnostics, all offline unless noted:

```powershell
.venv\Scripts\python.exe tools\selfcheck.py     # deps / config / memory / persona / brain / Bilibili / scheduler / voice / server
.venv\Scripts\python.exe tools\e2e_check.py     # full pipeline against a local mock LLM
.venv\Scripts\python.exe tools\zz_ui_check.py   # clicks through the UI via CDP (needs the app running)
```

## Configuration

Everything lives in `config.json` (created from `config.example.json`).
Highlights: `llm.*`, `persona.*`, `voice.*` (including `voice.rvc`), `bilibili.topics`,
`devices.*`, `motion.*`, `budget.*`, `schedule.*`.

The UI is **Chinese only** for now — localisation PRs are very welcome.

## What is deliberately *not* in this repo

| Not included | Why | What you do |
|---|---|---|
| `config.json` | contains your API key, Bilibili cookie and gateway token | generate it from `config.example.json`; `.gitignore` already blocks it |
| `data/` | chat history, memory DB, voice cache, browser profile, ASR models | created on first run |
| RVC voice models (`.pth` / `.index`) | large, and usually licensed to someone else | bring your own, point `voice.rvc` at them |
| VoiceChanger (DSP project) | it is a separate project; this repo only reuses its DSP chain | optional: put it next to this repo, or set `NEKO_VOICECHANGER_ROOT`. Without it you lose one layer of voice styling — nothing breaks |
| Sticker images | everyone's collection differs, and they are usually someone else's artwork | drop your own into `data/stickers/` |

## Requirements

- **Windows** (the launcher scripts are `.bat` / `.vbs`; the server itself is plain Python and can run elsewhere)
- **Python 3.12+** (developed and tested on 3.14)
- A **DeepSeek API key** for real conversation (otherwise offline fallback)
- Optional: an RVC bundle with its own embedded Python for voice conversion; `ffmpeg` for some audio decoding

## Privacy & safety notes

- The app binds to `127.0.0.1` only. Your API key and cookies never leave your machine except to the
  services you configured (DeepSeek, Bilibili, NetEase).
- `config.json` holds real credentials in plain text — **never commit or share it**.
- Device/motion control is whitelist-based, with caps and confirmation for dangerous actions.

## Compliance

- Bilibili data is fetched through **public, unauthenticated endpoints** (plus your own cookie if you log in),
  used only for local summaries. No bulk scraping, no redistribution. Follow Bilibili's ToS.
- **Voice models are not distributed here.** Using someone else's voice (especially a real person's or a
  voice actor's) requires that you have the rights to it. Impersonation is not okay.

## License

[MIT](LICENSE) — use it, fork it, change it; keep the copyright notice.

---

The full documentation (architecture, measured numbers, and a long list of pitfalls that were actually
hit while building this) is in the Chinese README: **[README.md](README.md)**.
