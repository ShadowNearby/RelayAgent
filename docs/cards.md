<h1 align="center">Supported Reference Cards</h1>

<p align="center">
  <b>MVP scope, v0.1 — 10 verified Android reference cards · 50 declared capabilities</b>
</p>

<p align="center">
  <b>English</b> | <a href="cards.zh.md">中文</a>
</p>

Ten verified Android reference cards, **50 declared capabilities** total. *Card class* (single-bubble TextView vs. multi-node RecyclerView) drives the reply-extraction strategy — see [`x_capture_full_reply`](manifest_conventions.md#-3-when-to-set-x_capture_full_reply) in the manifest conventions.

| App | Package | Capabilities | Card class |
| --- | --- | --- | --- |
| Amap (高德地图) | com.autonavi.minimap | POI search, navigation, ride hailing, trip planning | mixed |
| Tongyi Qwen (通义千问) | com.aliyun.tongyi | foundation_llm, train/ride/food/hotel/movie booking, product search/purchase/order tracking | mixed |
| Ctrip (携程旅行) | ctrip.android.view | flights, hotels, trains, attractions, package tours | mixed |
| Gemini | com.google.android.apps.bard | foundation_llm, public-web retrieval, Google-service read/write when authorized | mixed |
| RedNote (小红书) | com.xingin.xhs | community UGC Q&A via AI search | multi-node |
| WeChat (微信) | com.tencent.mm | Yuanbao chat surface, AI search | mixed |
| WPS Office | cn.wps.moffice_eng | AI doc / PPT / writing assist | single-bubble |
| Reddit | com.reddit.frontpage | Reddit Ask vertical community search and summarization | multi-node |
| Booking.com | com.booking | travel discovery, itinerary planning, accommodation search | mixed |
| Microsoft Copilot | com.microsoft.copilot | foundation_llm, nearby POI search, product search | single-bubble |

Quality bar per card: all required SPEC fields populated, ≥2 real example prompts per capability, verified manually within 30 days of submission, `handoff_to_user_required` correct for every irreversible capability.

Submitting a card: [CONTRIBUTING.md](../CONTRIBUTING.md). The manifest specification is in [SPEC.md](../SPEC.md); authoring conventions in [manifest conventions](manifest_conventions.md).
