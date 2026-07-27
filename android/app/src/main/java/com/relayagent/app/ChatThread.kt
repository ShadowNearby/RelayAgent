package com.relayagent.app

import android.content.Context
import android.content.Intent
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import androidx.core.content.ContextCompat
import androidx.recyclerview.widget.RecyclerView
import com.relayagent.app.databinding.ItemChatAnswerBinding
import com.relayagent.app.databinding.ItemChatNoticeBinding
import com.relayagent.app.databinding.ItemChatUserBinding
import com.relayagent.app.databinding.ItemChatWorkingBinding

/**
 * The task thread model + adapter behind the conversation-style home screen.
 *
 * A run renders as: a [ChatItem.User] bubble (the goal), a live
 * [ChatItem.Working] activity card (subtask rows + current step, updated in
 * place from [RunEvents]), then a [ChatItem.Answer] result card. Small
 * [ChatItem.Notice] lines carry side signals (stop requested, waiting for the
 * overlay answer).
 *
 * [ChatStore] keeps the thread in memory across activity recreation (same
 * pattern as [RunLog]); the durable record stays in the trajectory logs
 * browsed via LogActivity.
 */
sealed class ChatItem {

    data class User(val text: String) : ChatItem()

    class Working : ChatItem() {
        data class LegRow(val id: String, val label: String, var done: Boolean = false)

        val legs = mutableListOf<LegRow>()
        var stepLine: String? = null
        var running = true
        var stopping = false
    }

    data class Answer(
        val ok: Boolean,
        val verdict: String,
        val text: String,
        val trajRoot: String? = null,
    ) : ChatItem()

    data class Notice(val text: String) : ChatItem()
}

object ChatStore {
    val items = mutableListOf<ChatItem>()

    private var hydrated = false

    /**
     * Cold-start rebuild of past exchanges from the on-disk run roots, so the
     * home thread isn't amnesiac after process death (this object itself only
     * survives activity recreation). Read-only and best-effort over the same
     * files the log viewer parses ([TrajLog]): meta.json (the request text
     * entry.py persists + kind/error) rebuilds the User bubble, the legs'
     * agent_reply.json rebuild the Answer card; runs without a readable
     * meta.json are skipped. Runs once per process and only into an empty
     * thread, so it can never interleave with a live run's items.
     */
    fun hydrateFromDisk(context: Context, limit: Int = 10) {
        if (hydrated || items.isNotEmpty()) return
        hydrated = true
        val runs = try {
            TrajLog.listRuns(TrajLog.trajRoot(context.filesDir))
        } catch (e: Exception) {
            return // never let history rebuilding break the home screen
        }
        // listRuns is newest-first; append oldest-first so the newest run
        // lands at the bottom of the thread like a live exchange would.
        for (run in runs.take(limit).asReversed()) {
            val request = run.request ?: continue
            val replies = try {
                TrajLog.legDirs(run.dir).mapNotNull { TrajLog.parseLeg(it).reply }
            } catch (e: Exception) {
                emptyList()
            }
            val ok = run.error == null
            items.add(ChatItem.User(request))
            items.add(
                ChatItem.Answer(
                    ok = ok,
                    verdict = context.getString(
                        if (ok) R.string.result_done else R.string.result_failed
                    ),
                    // Same fallback text as RunSession.summarizeBlackboard.
                    text = run.error ?: replies.joinToString("\n\n").ifEmpty { "已执行完毕。" },
                    trajRoot = run.dir.absolutePath,
                )
            )
        }
    }
}

class ChatAdapter(private val items: List<ChatItem>) :
    RecyclerView.Adapter<RecyclerView.ViewHolder>() {

    private companion object {
        const val TYPE_USER = 0
        const val TYPE_WORKING = 1
        const val TYPE_ANSWER = 2
        const val TYPE_NOTICE = 3
    }

    class UserVH(val b: ItemChatUserBinding) : RecyclerView.ViewHolder(b.root)
    class WorkingVH(val b: ItemChatWorkingBinding) : RecyclerView.ViewHolder(b.root)
    class AnswerVH(val b: ItemChatAnswerBinding) : RecyclerView.ViewHolder(b.root)
    class NoticeVH(val b: ItemChatNoticeBinding) : RecyclerView.ViewHolder(b.root)

    override fun getItemCount() = items.size

    override fun getItemViewType(position: Int): Int = when (items[position]) {
        is ChatItem.User -> TYPE_USER
        is ChatItem.Working -> TYPE_WORKING
        is ChatItem.Answer -> TYPE_ANSWER
        is ChatItem.Notice -> TYPE_NOTICE
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): RecyclerView.ViewHolder {
        val inf = LayoutInflater.from(parent.context)
        return when (viewType) {
            TYPE_USER -> UserVH(ItemChatUserBinding.inflate(inf, parent, false))
            TYPE_WORKING -> WorkingVH(ItemChatWorkingBinding.inflate(inf, parent, false))
            TYPE_ANSWER -> AnswerVH(ItemChatAnswerBinding.inflate(inf, parent, false))
            else -> NoticeVH(ItemChatNoticeBinding.inflate(inf, parent, false))
        }
    }

    override fun onBindViewHolder(holder: RecyclerView.ViewHolder, position: Int) {
        when (val item = items[position]) {
            is ChatItem.User -> (holder as UserVH).b.text.text = item.text
            is ChatItem.Working -> bindWorking((holder as WorkingVH).b, item)
            is ChatItem.Answer -> bindAnswer((holder as AnswerVH).b, item)
            is ChatItem.Notice -> (holder as NoticeVH).b.text.text = item.text
        }
    }

    private fun bindWorking(b: ItemChatWorkingBinding, item: ChatItem.Working) {
        val ctx = b.root.context
        b.spinner.visibility = if (item.running) View.VISIBLE else View.GONE
        b.title.text = ctx.getString(
            when {
                item.running && item.stopping -> R.string.working_stopping
                item.running -> R.string.working_title
                else -> R.string.working_done_title
            }
        )
        b.legList.removeAllViews()
        for (leg in item.legs) {
            b.legList.addView(TextView(ctx).apply {
                text = (if (leg.done) "✓  " else "▸  ") + leg.label
                textSize = 13f
                setTextColor(
                    ContextCompat.getColor(
                        ctx, if (leg.done) R.color.status_ok else R.color.on_surface
                    )
                )
                setPadding(0, 4, 0, 4)
            })
        }
        val step = item.stepLine
        if (item.running && !step.isNullOrEmpty()) {
            b.stepLine.visibility = View.VISIBLE
            b.stepLine.text = step
        } else {
            b.stepLine.visibility = View.GONE
        }
    }

    private fun bindAnswer(b: ItemChatAnswerBinding, item: ChatItem.Answer) {
        val ctx = b.root.context
        b.verdict.text = item.verdict
        b.verdict.setTextColor(
            ContextCompat.getColor(ctx, if (item.ok) R.color.status_ok else R.color.status_bad)
        )
        b.text.text = item.text
        if (item.trajRoot != null) {
            b.detailBtn.visibility = View.VISIBLE
            b.detailBtn.setOnClickListener {
                ctx.startActivity(
                    Intent(ctx, RunDetailActivity::class.java)
                        .putExtra(RunDetailActivity.EXTRA_DIR, item.trajRoot)
                )
            }
        } else {
            b.detailBtn.visibility = View.GONE
            b.detailBtn.setOnClickListener(null)
        }
    }
}
