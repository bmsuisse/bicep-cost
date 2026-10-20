# Design Review: issue #3 cost report charts

**Date**: 2026-10-20  
**Preview**: local temporary HTML viewer; the shipped output is CLI text and Markdown.

## Overall Impression

The chart is readable in both formats. The largest cost appears first, bars share a fixed width, and numeric values align. The retained tables and exclusions remain legible below the chart.

## Findings

No high, medium, or low severity findings in the chart change.

At 390px the chart values extend beyond the initial viewport and require horizontal scrolling. This is acceptable for fixed-width CI output and Markdown code blocks. The preview explicitly uses `overflow-x:auto` on code blocks and the Markdown container; screenshots alone do not establish actual scrolling behavior in GitHub or a terminal.

## What Looks Good

- Labels sit above bars, preserving bar and amount alignment despite different resource-name lengths.
- Descending costs (259.88 then 13.14 USD) match descending bar lengths; percentages clearly identify the known-total denominator.
- Plain monochrome text keeps contrast strong and works without graphical rendering.

## Top Fixes

None required for the reviewed change.

## Screenshots

- [Before, desktop](charts/before-desktop.png)
- [After, desktop](charts/after-desktop.png)
- [Before, mobile](charts/before-mobile.png)
- [After, mobile](charts/after-mobile.png)
