package com.appagentcards.domain.engine

import com.appagentcards.domain.model.*
import javax.inject.Inject

class CoordinateRemapper @Inject constructor() {

    fun remapBounds(
        bounds: Selector.ByBounds,
        sourceMetrics: DeviceMetrics,
        targetMetrics: DeviceMetrics
    ): Selector.ByBounds {
        val box = bounds.box
        val anchor = bounds.anchor
        val srcW = sourceMetrics.widthPx
        val srcH = sourceMetrics.heightPx
        val srcDpi = sourceMetrics.densityDpi
        val tgtW = targetMetrics.widthPx
        val tgtH = targetMetrics.heightPx
        val tgtDpi = targetMetrics.densityDpi

        val wDp = pxToDp(box.width, srcDpi)
        val hDp = pxToDp(box.height, srcDpi)
        val wPx = dpToPx(wDp, tgtDpi)
        val hPx = dpToPx(hDp, tgtDpi)

        val remappedBox = when (anchor) {
            Anchor.BOTTOM_RIGHT -> edgeAnchor(
                leftMargin = null, topMargin = null,
                rightMargin = srcW - box.right, bottomMargin = srcH - box.bottom,
                wPx, hPx, srcDpi, tgtW, tgtH, tgtDpi
            )
            Anchor.TOP_RIGHT -> edgeAnchor(
                leftMargin = null, topMargin = box.top,
                rightMargin = srcW - box.right, bottomMargin = null,
                wPx, hPx, srcDpi, tgtW, tgtH, tgtDpi
            )
            Anchor.BOTTOM_LEFT -> edgeAnchor(
                leftMargin = box.left, topMargin = null,
                rightMargin = null, bottomMargin = srcH - box.bottom,
                wPx, hPx, srcDpi, tgtW, tgtH, tgtDpi
            )
            Anchor.TOP_LEFT -> edgeAnchor(
                leftMargin = box.left, topMargin = box.top,
                rightMargin = null, bottomMargin = null,
                wPx, hPx, srcDpi, tgtW, tgtH, tgtDpi
            )
            Anchor.CENTER, Anchor.NONE -> {
                val sx = tgtW.toFloat() / srcW
                val sy = tgtH.toFloat() / srcH
                Rect(
                    left = (box.left * sx).toInt(),
                    top = (box.top * sy).toInt(),
                    right = (box.right * sx).toInt(),
                    bottom = (box.bottom * sy).toInt()
                )
            }
        }

        return Selector.ByBounds(remappedBox, anchor)
    }

    fun remapSteps(
        steps: List<Step>,
        sourceMetrics: DeviceMetrics?,
        targetMetrics: DeviceMetrics
    ): List<Step> {
        if (sourceMetrics == null || sourceMetrics == targetMetrics) return steps
        return steps.map { step -> remapStep(step, sourceMetrics, targetMetrics) }
    }

    private fun remapStep(
        step: Step,
        sourceMetrics: DeviceMetrics,
        targetMetrics: DeviceMetrics
    ): Step = when (step) {
        is Step.Tap -> Step.Tap(remapSelector(step.selector, sourceMetrics, targetMetrics))
        is Step.TapLabel -> step
        is Step.TapScreenFraction -> step
        is Step.Swipe -> step
        is Step.Wait -> step
    }

    fun remapSelector(
        selector: Selector,
        sourceMetrics: DeviceMetrics,
        targetMetrics: DeviceMetrics
    ): Selector = when (selector) {
        is Selector.ByBounds -> remapBounds(selector, sourceMetrics, targetMetrics)
        else -> selector
    }

    private fun edgeAnchor(
        leftMargin: Int?, topMargin: Int?,
        rightMargin: Int?, bottomMargin: Int?,
        wPx: Int, hPx: Int,
        srcDpi: Int, tgtW: Int, tgtH: Int, tgtDpi: Int
    ): Rect {
        val x1: Int
        val x2: Int
        if (leftMargin != null) {
            x1 = dpToPx(pxToDp(leftMargin, srcDpi), tgtDpi)
            x2 = x1 + wPx
        } else {
            x2 = tgtW - dpToPx(pxToDp(rightMargin!!, srcDpi), tgtDpi)
            x1 = x2 - wPx
        }
        val y1: Int
        val y2: Int
        if (topMargin != null) {
            y1 = dpToPx(pxToDp(topMargin, srcDpi), tgtDpi)
            y2 = y1 + hPx
        } else {
            y2 = tgtH - dpToPx(pxToDp(bottomMargin!!, srcDpi), tgtDpi)
            y1 = y2 - hPx
        }
        return Rect(x1, y1, x2, y2)
    }

    private fun pxToDp(px: Int, dpi: Int): Float = px * 160f / dpi

    private fun dpToPx(dp: Float, dpi: Int): Int = (dp * dpi / 160f).toInt()
}
