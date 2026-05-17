"""SeedTTS 2.0 (豆包语音合成 2.0 大模型) WebSocket 客户端。

接口:wss://openspeech.bytedance.com/api/v3/tts/bidirection
鉴权:旧版控制台用 X-Api-App-Id + X-Api-Access-Key + X-Api-Resource-Id
音色:_bigtts 后缀,见 https://www.volcengine.com/docs/6561/1257544

环境变量:
  VOLC_TTS_APPID            APP ID
  VOLC_TTS_TOKEN            Access Token
  SEEDTTS_VOICE_MALE        男声音色 ID(默认 zh_male_M392_conversation_wvae_bigtts)
  SEEDTTS_VOICE_FEMALE      女声音色 ID(默认 zh_female_shuangkuaisisi_moon_bigtts)
"""
import asyncio
import json
import os
import struct
import uuid

import websockets


WS_URL = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"

# 协议常量
PROTO_VERSION = 0b0001
HEADER_SIZE = 0b0001        # *4 bytes = 4 bytes
MSG_FULL_CLIENT_REQ = 0b0001
MSG_FULL_SERVER_RESP = 0b1001
MSG_AUDIO_ONLY_RESP = 0b1011
MSG_ERROR = 0b1111
FLAG_WITH_EVENT = 0b0100
SER_JSON = 0b0001
SER_RAW = 0b0000
COMP_NONE = 0b0000

# 事件
EVT_StartConnection = 1
EVT_FinishConnection = 2
EVT_ConnectionStarted = 50
EVT_ConnectionFailed = 51
EVT_ConnectionFinished = 52
EVT_StartSession = 100
EVT_CancelSession = 101
EVT_FinishSession = 102
EVT_SessionStarted = 150
EVT_SessionCanceled = 151
EVT_SessionFinished = 152
EVT_SessionFailed = 153
EVT_TaskRequest = 200
EVT_TTSSentenceStart = 350
EVT_TTSSentenceEnd = 351
EVT_TTSResponse = 352

DEFAULT_VOICE_MALE = os.getenv(
    "SEEDTTS_VOICE_MALE",
    "zh_male_M392_conversation_wvae_bigtts"
)
DEFAULT_VOICE_FEMALE = os.getenv(
    "SEEDTTS_VOICE_FEMALE",
    "zh_female_shuangkuaisisi_moon_bigtts"
)


def voice_for_gender(gender: str | None) -> str:
    if (gender or "").lower() == "male":
        return DEFAULT_VOICE_MALE
    return DEFAULT_VOICE_FEMALE


def _make_frame(*, msg_type: int, serialization: int, event: int,
                session_id: str | None = None,
                payload: bytes = b'{}') -> bytes:
    out = bytearray()
    out.append((PROTO_VERSION << 4) | HEADER_SIZE)
    out.append((msg_type << 4) | FLAG_WITH_EVENT)
    out.append((serialization << 4) | COMP_NONE)
    out.append(0)                                       # reserved
    out += struct.pack(">i", event)                     # event number
    if session_id is not None:
        sid = session_id.encode("utf-8")
        out += struct.pack(">I", len(sid))
        out += sid
    out += struct.pack(">I", len(payload))
    out += payload
    return bytes(out)


_EVENTS_WITH_CONNECT_ID = {EVT_ConnectionStarted, EVT_ConnectionFailed,
                          EVT_ConnectionFinished}
_EVENTS_WITH_SESSION_ID = {
    EVT_SessionStarted, EVT_SessionCanceled, EVT_SessionFinished,
    EVT_SessionFailed, EVT_TTSSentenceStart, EVT_TTSSentenceEnd,
    EVT_TTSResponse,
}


def _parse_frame(data: bytes) -> dict:
    if len(data) < 4:
        raise ValueError(f"frame too short: {len(data)} bytes")
    header_size = (data[0] & 0x0F) * 4
    msg_type = data[1] >> 4
    flags = data[1] & 0x0F
    pos = header_size

    event = None
    if flags & FLAG_WITH_EVENT:
        event = struct.unpack(">i", data[pos:pos + 4])[0]
        pos += 4

    connect_id = None
    session_id = None
    if event in _EVENTS_WITH_CONNECT_ID:
        size = struct.unpack(">I", data[pos:pos + 4])[0]
        pos += 4
        connect_id = data[pos:pos + size].decode("utf-8", errors="replace")
        pos += size
    elif event in _EVENTS_WITH_SESSION_ID:
        size = struct.unpack(">I", data[pos:pos + 4])[0]
        pos += 4
        session_id = data[pos:pos + size].decode("utf-8", errors="replace")
        pos += size

    payload_size = struct.unpack(">I", data[pos:pos + 4])[0]
    pos += 4
    payload = data[pos:pos + payload_size]

    return {
        "event": event,
        "msg_type": msg_type,
        "connect_id": connect_id,
        "session_id": session_id,
        "payload": payload,
    }


async def synth(text: str,
                voice: str | None = None,
                gender: str | None = None,
                fmt: str = "mp3",
                sample_rate: int = 24000,
                speech_rate: int | None = None,
                emotion: str | None = None,
                emotion_scale: int | None = None,
                model: str | None = None,
                additions: dict | None = None,
                context_texts: list[str] | None = None,
                timeout: float = 120.0) -> bytes:
    """合成,返回完整音频字节流(mp3/wav/pcm)。

    model 可选 seed-tts-2.0-standard / seed-tts-2.0-expressive。
    不传时读 env SEEDTTS_MODEL,默认 expressive(自然度强)。

    speech_rate 默认 -10(略慢于 0),更接近自然对话节奏。可通过 SEEDTTS_SPEECH_RATE 覆盖。

    context_texts 是 expressive 模型独有的语气指令,例如 ["用日常聊天的语气"]。
    """
    if model is None:
        model = os.getenv("SEEDTTS_MODEL", "seed-tts-2.0-expressive")
    if speech_rate is None:
        speech_rate = int(os.getenv("SEEDTTS_SPEECH_RATE", "-10"))
    appid = os.environ.get("VOLC_TTS_APPID")
    token = os.environ.get("VOLC_TTS_TOKEN")
    if not (appid and token):
        raise RuntimeError("VOLC_TTS_APPID/TOKEN not set")

    if not voice:
        voice = voice_for_gender(gender)

    audio_params: dict = {"format": fmt, "sample_rate": sample_rate}
    if speech_rate:
        audio_params["speech_rate"] = speech_rate
    if emotion:
        audio_params["emotion"] = emotion
    if emotion_scale:
        audio_params["emotion_scale"] = max(1, min(5, emotion_scale))

    req_params: dict = {
        "speaker": voice,
        "audio_params": audio_params,
    }
    if model:
        req_params["model"] = model

    # additions / context_texts 合并(都是 expressive 独有)
    _add = dict(additions or {})
    if context_texts and "expressive" in (model or ""):
        _add["context_texts"] = context_texts
    if _add:
        req_params["additions"] = json.dumps(_add, ensure_ascii=False)

    session_id = str(uuid.uuid4())
    headers = {
        "X-Api-App-Id": appid,
        "X-Api-Access-Key": token,
        "X-Api-Resource-Id": "seed-tts-2.0",
        "X-Api-Connect-Id": str(uuid.uuid4()),
    }

    audio_chunks: list[bytes] = []

    async with websockets.connect(
        WS_URL, additional_headers=headers, max_size=None,
        open_timeout=30, close_timeout=10,
    ) as ws:
        # 1. StartConnection
        await ws.send(_make_frame(
            msg_type=MSG_FULL_CLIENT_REQ, serialization=SER_JSON,
            event=EVT_StartConnection,
        ))
        resp = _parse_frame(await ws.recv())
        if resp["event"] != EVT_ConnectionStarted:
            raise RuntimeError(
                f"ConnectionStarted expected, got {resp['event']}: "
                f"{resp['payload'][:300]}"
            )

        # 2. StartSession(初始化参数)
        start_payload = json.dumps({
            "event": EVT_StartSession,
            "namespace": "BidirectionalTTS",
            "req_params": req_params,
        }, ensure_ascii=False).encode("utf-8")
        await ws.send(_make_frame(
            msg_type=MSG_FULL_CLIENT_REQ, serialization=SER_JSON,
            event=EVT_StartSession, session_id=session_id,
            payload=start_payload,
        ))
        resp = _parse_frame(await ws.recv())
        if resp["event"] != EVT_SessionStarted:
            raise RuntimeError(
                f"SessionStarted expected, got event={resp['event']}: "
                f"{resp['payload'][:300]}"
            )

        # 3. TaskRequest(发送文本)
        task_payload = json.dumps({
            "event": EVT_TaskRequest,
            "namespace": "BidirectionalTTS",
            "req_params": {"text": text},
        }, ensure_ascii=False).encode("utf-8")
        await ws.send(_make_frame(
            msg_type=MSG_FULL_CLIENT_REQ, serialization=SER_JSON,
            event=EVT_TaskRequest, session_id=session_id,
            payload=task_payload,
        ))

        # 4. FinishSession(声明没有更多文本)
        await ws.send(_make_frame(
            msg_type=MSG_FULL_CLIENT_REQ, serialization=SER_JSON,
            event=EVT_FinishSession, session_id=session_id,
        ))

        # 5. 接收音频帧,直到 SessionFinished
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError("SeedTTS overall timeout")
            data = await asyncio.wait_for(ws.recv(), timeout=remaining)
            r = _parse_frame(data)
            ev = r["event"]
            if ev == EVT_TTSResponse:
                audio_chunks.append(r["payload"])
            elif ev == EVT_SessionFinished:
                break
            elif ev in (EVT_SessionFailed, EVT_SessionCanceled):
                raise RuntimeError(
                    f"SeedTTS session ended abnormally event={ev}: "
                    f"{r['payload'][:500].decode('utf-8', 'replace')}"
                )
            elif r["msg_type"] == MSG_ERROR:
                raise RuntimeError(
                    f"SeedTTS error frame: "
                    f"{r['payload'][:500].decode('utf-8', 'replace')}"
                )
            # 其他状态帧(TTSSentenceStart/End)忽略

        # 6. FinishConnection(最佳实践,允许超时或服务端先关)
        try:
            await ws.send(_make_frame(
                msg_type=MSG_FULL_CLIENT_REQ, serialization=SER_JSON,
                event=EVT_FinishConnection,
            ))
            await asyncio.wait_for(ws.recv(), timeout=5)
        except Exception:
            pass

    if not audio_chunks:
        raise RuntimeError("SeedTTS finished but returned no audio")
    return b"".join(audio_chunks)


def estimate_cost_cents(char_count: int) -> int:
    """SeedTTS 2.0 字符版大约 ¥0.0005/字符(以方舟账单为准)。"""
    return max(1, char_count // 200)
