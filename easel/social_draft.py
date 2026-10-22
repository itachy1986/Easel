"""Selective, text-only drafting guidance; never requests media generation."""

import re


def social_draft_instruction(message: str) -> str:
    if not re.search(r"小红书|微信公众号|公众号", message):
        return ""
    # Judge the user's requested action, not the article topic: "write about fixing code"
    # is creation, while "debug the publishing API" is product diagnosis.
    if not re.search(r"写|创作|生成|改编|润色|(?:优化(?:这篇|文案|文章|帖子|笔记))", message):
        return ""
    if not re.search(r"文案|文章|图文|帖子|推文|笔记", message):
        return ""
    return (
        "〔社媒草稿格式，仅本轮创作适用〕请把最终可见答复清楚分为："
        "标题、正文、话题标签、Nano Banana 生图提示词 四个独立标题。"
        "提示词用中文，根据本篇内容描述主体、场景、构图、光线、风格和文字安全留白；"
        "比例默认 3:4，用户指定时采用其比例。提示词不得混入发布正文。"
        "这里只写可复制到 Google Flow 的提示词；不要调用生图/视频/付费 API，"
        "不要打开外部账号、登录、扫码或发布。保留用户的真实创作意图，不编造事实。"
    )
