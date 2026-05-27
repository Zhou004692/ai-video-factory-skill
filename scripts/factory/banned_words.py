"""TTS 前文本预过滤:替换火山 OmniHuman/SeedTTS 已知会拦的词。

只处理"明确多字脏话短语",避免误伤(单字交给 compliance LLM 审查阶段)。
词表来源:references/prompts/compliance.md「音频禁词白名单」+ 实测拦截记录。

J-VERIFY04 实战:shot 6 "...你大爷的!这哪是新车?..."
被 OmniHuman 以 Pre Audio Risk Not Pass (50514) 拦截,烧 ¥2.51 retry。
"""

# 明确禁词 → 替换词。按"长串先匹配"排序,避免短串提前命中。
_REPLACEMENTS = [
    ("你大爷的", "好家伙"),
    ("你妈的",   "哎呀"),
    ("他娘的",   "嘿"),
    ("我擦嘞",   "我去"),
    ("卧了个槽", "我去"),
    ("卧槽",     "我去"),
    ("我擦",     "我去"),
    ("妈了个", "真是的"),
    ("傻逼",     "装样子"),
    ("脑残",     "糊涂"),
    ("nmsl",     ""),
    ("NMSL",     ""),
]


def sanitize(text: str, shot_idx: int | None = None) -> tuple[str, list[str]]:
    """返回 (清洗后文本, 命中的禁词列表)。

    命中的禁词写日志便于审计。空 list 表示原文已干净。
    """
    if not text:
        return text, []
    hits: list[str] = []
    out = text
    for bad, good in _REPLACEMENTS:
        if bad in out:
            hits.append(bad)
            out = out.replace(bad, good)
    if hits and shot_idx is not None:
        print(
            f"[banned_words] shot #{shot_idx} replaced {hits} -> "
            f"{[g for b,g in _REPLACEMENTS if b in hits]}",
            flush=True,
        )
    return out, hits
