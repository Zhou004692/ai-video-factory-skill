"""s7: 发布到视频号(占位)。

视频号没有官方发布 API,实际发布有三条路:
1. Playwright RPA 操作"视频号助手" PC 端(需要扫码登录)
2. 第三方聚合分发(易媒助手/西瓜助手),要购买套餐
3. 手动发布 — 这是 SKILL.md 默认推荐:让人工把关 AI 内容

当前实现:**不真实发布**,只把 final.mp4 路径登记下来,推送给用户人工审。
等你确认要真发布,把下面的 _via_playwright 实装即可。
"""
import os

from ..db import conn


async def run(job_id: str) -> None:
    db = conn()
    job = db.execute(
        "SELECT final_path FROM jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    if not job or not job["final_path"]:
        raise RuntimeError("final_path missing")

    mode = os.getenv("PUBLISH_MODE", "manual").lower()
    if mode == "manual":
        # 默认:不发布,只标记为 PUBLISHED 让 pipeline 收尾
        db.execute(
            "UPDATE jobs SET publish_url=? WHERE job_id=?",
            (f"manual:{job['final_path']}", job_id)
        )
        db.commit()
        return

    if mode == "playwright":
        await _via_playwright(job_id, job["final_path"])
        return

    raise RuntimeError(f"unknown PUBLISH_MODE: {mode}")


async def _via_playwright(job_id: str, video_path: str):
    """TODO: Playwright 操作 https://channels.weixin.qq.com/platform。

    实现要点:
    1. 持久化浏览器 profile(扫码一次后保存)
    2. 上传视频,等转码
    3. 填标题、话题、合集
    4. 加 #AI生成 标签(合规要求)
    5. 立即发布 or 定时发布
    6. 抓取发布后的 URL 回写
    """
    raise NotImplementedError(
        "Playwright 发布尚未实装。建议手动发布,或参考 references/troubleshooting.md "
        "里 '视频号 RPA 发布' 章节自行实现"
    )
