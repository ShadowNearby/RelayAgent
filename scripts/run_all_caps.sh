#!/usr/bin/env bash
# Iterate over every manifest capability and drive scripts/run_test.py.
# Each run's traj_logs/user_task/ is moved to results/<run_id>/<pkg>__<cap>/.
set -u
cd "$(dirname "$0")/.."

RUN_ID="${RUN_ID:-$(date +%Y%m%d-%H%M%S)}"
OUT_ROOT="test-results/run_all_caps/${RUN_ID}"
mkdir -p "$OUT_ROOT"
SUMMARY="$OUT_ROOT/summary.tsv"
echo -e "status\tpkg\tcap\tprompt\ttraj_dir\texit\tseconds" > "$SUMMARY"

# (pkg | cap | prompt) — observational prompts where commit would be irreversible.
CASES=(
  # cn.wps.moffice_eng
  "cn.wps.moffice_eng|chat|用一句话介绍 WPS AI 能做什么"
  "cn.wps.moffice_eng|ai_ppt|帮我做一份关于光合作用的 PPT"
  "cn.wps.moffice_eng|quick_writing|帮我写一封请假邮件，明天上午有事"
  "cn.wps.moffice_eng|doc_reading|帮我总结这份合同的关键条款"
  "cn.wps.moffice_eng|web_summary|帮我总结这个网页：https://zh.wikipedia.org/wiki/光合作用"
  # com.aliyun.tongyi
  "com.aliyun.tongyi|chat|请用一句话介绍杭州西湖"
  "com.aliyun.tongyi|book_train|帮我订一张明天下午两点左右上海到南京的高铁票"
  "com.aliyun.tongyi|order_food|帮我点三杯蜜雪冰城蜜桃四季春"
  "com.aliyun.tongyi|hail_ride|帮我叫一辆经济型车，从上海人民广场到虹桥火车站"
  "com.aliyun.tongyi|book_hotel|帮我订明晚上海外滩附近500元以内的一间酒店"
  "com.aliyun.tongyi|book_movie|帮我订今晚上海一张电影票"
  # com.autonavi.minimap
  "com.autonavi.minimap|find_nearby|附近有什么加油站"
  "com.autonavi.minimap|navigate_to|帮我导航到上海外滩"
  "com.autonavi.minimap|hail_ride|帮我叫一辆经济型车从上海人民广场到虹桥火车站"
  "com.autonavi.minimap|plan_trip|帮我规划一个上海周末两日游"
  # com.taobao.taobao
  "com.taobao.taobao|search_product|帮我找一台适合学生的平板电脑，预算2000以内"
  "com.taobao.taobao|compare_products|帮我比较 iPhone 17 和 iPhone 16 哪个更值得买"
  "com.taobao.taobao|buy_product|帮我下单一包10公斤的膨润土猫砂"
  "com.taobao.taobao|order_local_delivery|帮我下单一瓶附近半小时能送到的矿泉水"
  "com.taobao.taobao|track_order|我最近买的东西到哪了"
  # com.tencent.mm
  "com.tencent.mm|ai_search|今天上海天气怎么样"
  # com.xingin.xhs
  "com.xingin.xhs|qa_community_knowledge|周末上海带娃去哪玩"
  # ctrip.android.view
  "ctrip.android.view|chat_travel_qa|三亚十一期间天气和穿衣建议"
  "ctrip.android.view|search_flight|帮我订下周一上海到北京下午的机票"
  "ctrip.android.view|search_hotel|帮我订明晚上海外滩附近800元以内的酒店"
  "ctrip.android.view|search_train|帮我订周五早上8点北京到天津的高铁票"
  "ctrip.android.view|plan_trip|我想十一去三亚玩四天怎么安排"
  "ctrip.android.view|search_attraction_info|上海迪士尼乐园的开放时间和票价"
)

# Optional filter: pass a regex to run only matching pkg|cap entries.
FILTER="${FILTER:-}"

for line in "${CASES[@]}"; do
  IFS='|' read -r pkg cap prompt <<<"$line"
  if [[ -n "$FILTER" ]] && ! [[ "$pkg|$cap" =~ $FILTER ]]; then
    continue
  fi
  slug="${pkg}__${cap}"
  dest="$OUT_ROOT/$slug"
  log="$OUT_ROOT/$slug.log"
  echo
  echo "═══════════════════════════════════════════════════════"
  echo "▶ [$slug]  $prompt"
  echo "═══════════════════════════════════════════════════════"
  t0=$(date +%s)
  uv run scripts/run_test.py "$pkg" "$prompt" --max-step 30 < /dev/null > "$log" 2>&1
  rc=$?
  t1=$(date +%s)
  elapsed=$((t1 - t0))
  if [[ -d traj_logs/user_task ]]; then
    mv traj_logs/user_task "$dest"
  else
    mkdir -p "$dest"
  fi
  status="ok"; [[ $rc -ne 0 ]] && status="fail"
  echo -e "${status}\t${pkg}\t${cap}\t${prompt}\t${dest}\t${rc}\t${elapsed}" >> "$SUMMARY"
  echo "▶ done rc=$rc in ${elapsed}s  → $dest"
  echo "▶ summary so far:"
  column -t -s $'\t' "$SUMMARY" | tail -5
done

echo
echo "═══════════════════════════════════════════════════════"
echo "ALL DONE.  Summary: $SUMMARY"
column -t -s $'\t' "$SUMMARY"
