<h1 align="center">支持的参考卡片</h1>

<p align="center">
  <b>MVP 范围 v0.1 —— 10 张已验证的安卓参考卡片，共 50 个声明能力</b>
</p>

<p align="center">
  <a href="cards.md">English</a> | <b>中文</b>
</p>

10 张已验证的安卓参考卡片，共 **50 个声明能力**。*卡片类别*（单气泡 TextView vs. 多节点 RecyclerView）决定取回复的策略——见 manifest 约定里的 [`x_capture_full_reply`](manifest_conventions.zh.md#-3-x_capture_full_reply-开不开)。

| App | 包名 | 能力 | 卡片类别 |
| --- | --- | --- | --- |
| 高德地图 | com.autonavi.minimap | POI 搜索、导航、打车、行程规划 | mixed |
| 通义千问 | com.aliyun.tongyi | foundation_llm、火车/打车/外卖/酒店/影演出预订、商品搜索/购买/订单追踪 | mixed |
| 携程旅行 | ctrip.android.view | 机票、酒店、火车、景点、跟团游 | mixed |
| Gemini | com.google.android.apps.bard | foundation_llm、公网检索、授权后 Google 服务读写 | mixed |
| 小红书 | com.xingin.xhs | AI 搜索驱动的社区 UGC 问答 | multi-node |
| 微信 | com.tencent.mm | 元宝对话面、AI 搜索 | mixed |
| WPS Office | cn.wps.moffice_eng | AI 文档 / PPT / 写作辅助 | single-bubble |
| Reddit | com.reddit.frontpage | Reddit Ask 垂类社区搜索与总结 | multi-node |
| Booking.com | com.booking | 旅行发现、行程规划、住宿搜索 | mixed |
| Microsoft Copilot | com.microsoft.copilot | foundation_llm、附近 POI 搜索、商品搜索 | single-bubble |

每张卡的质量门槛：所有必填 SPEC 字段齐全、每个能力 ≥2 条真实示例 prompt、30 天内人工验证过、每个不可逆能力的 `handoff_to_user_required` 正确。

提交卡片见 [CONTRIBUTING.md](../CONTRIBUTING.md)。manifest 规范见 [SPEC.md](../SPEC.md)，写卡片约定见 [Manifest 约定](manifest_conventions.zh.md)。
