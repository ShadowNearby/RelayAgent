package com.relayagent.app

import androidx.recyclerview.widget.RecyclerView
import androidx.test.core.app.ActivityScenario
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.action.ViewActions.closeSoftKeyboard
import androidx.test.espresso.action.ViewActions.replaceText
import androidx.test.espresso.assertion.ViewAssertions.matches
import androidx.test.espresso.matcher.ViewMatchers.isDisplayed
import androidx.test.espresso.matcher.ViewMatchers.withId
import androidx.test.espresso.matcher.ViewMatchers.withText
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.hamcrest.Matchers.not
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/**
 * UI smoke for the conversation-style home screen + the secondary
 * activities, on a real device. Deliberately stops short of tapping send
 * with a goal: that path requires the accessibility service and fires the
 * MediaProjection consent dialog.
 */
@RunWith(AndroidJUnit4::class)
class MainActivitySmokeTest {

    @Before
    fun resetThread() {
        InstrumentationRegistry.getInstrumentation().runOnMainSync {
            ChatStore.items.clear()
        }
    }

    @Test
    fun homeShowsComposerChipsAndGreeting() {
        ActivityScenario.launch(MainActivity::class.java).use {
            onView(withId(R.id.composerInput)).check(matches(isDisplayed()))
            onView(withId(R.id.sendBtn)).check(matches(isDisplayed()))
            onView(withId(R.id.emptyState)).check(matches(isDisplayed()))
            onView(withId(R.id.exampleChips)).check(matches(isDisplayed()))
        }
    }

    @Test
    fun composerKeepsTypedText() {
        ActivityScenario.launch(MainActivity::class.java).use {
            onView(withId(R.id.composerInput))
                .perform(replaceText("帮我导航回家"), closeSoftKeyboard())
            onView(withId(R.id.composerInput)).check(matches(withText("帮我导航回家")))
        }
    }

    @Test
    fun threadRendersUserBubbleAndAnswerCard() {
        InstrumentationRegistry.getInstrumentation().runOnMainSync {
            ChatStore.items.add(ChatItem.User("smoke-user-goal"))
            ChatStore.items.add(
                ChatItem.Answer(true, "smoke-verdict", "smoke-answer-text", null)
            )
        }
        ActivityScenario.launch(MainActivity::class.java).use {
            onView(withText("smoke-user-goal")).check(matches(isDisplayed()))
            onView(withText("smoke-verdict")).check(matches(isDisplayed()))
            onView(withText("smoke-answer-text")).check(matches(isDisplayed()))
            onView(withId(R.id.emptyState)).check(matches(not(isDisplayed())))
        }
    }

    @Test
    fun examplesActivityListsBundledTasks() {
        ActivityScenario.launch(ExamplesActivity::class.java).use { scenario ->
            onView(withId(R.id.list)).check(matches(isDisplayed()))
            scenario.onActivity { a ->
                val count = a.findViewById<RecyclerView>(R.id.list).adapter?.itemCount ?: 0
                assertTrue("bundled examples.json produced no rows", count > 0)
            }
        }
    }

    @Test
    fun logActivityOpensOnWhateverIsOnDisk() {
        // The run list is the user's real traj_logs dir — may be empty or not;
        // either way the activity must come up with its list wired.
        ActivityScenario.launch(LogActivity::class.java).use { scenario ->
            scenario.onActivity { a ->
                val list = a.findViewById<RecyclerView>(R.id.list)
                assertTrue("run list has no adapter", list.adapter != null)
            }
        }
    }

    @Test
    fun settingsActivityShowsTheConfigForm() {
        ActivityScenario.launch(SettingsActivity::class.java).use {
            onView(withId(R.id.fieldBaseUrl)).check(matches(isDisplayed()))
            onView(withId(R.id.fieldModel)).check(matches(isDisplayed()))
        }
    }
}
