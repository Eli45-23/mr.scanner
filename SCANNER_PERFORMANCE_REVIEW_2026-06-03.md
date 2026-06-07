# Scanner Performance Review - 2026-06-03

## Executive Summary

- **Overall grade today:** C / Needs Work.
- **Best thing the bot did:** It correctly identified that the day was broadly weak and that AAPL, QQQ, and SPY were spending much of the session under key opening-range / premarket-low levels.
- **Worst thing the bot did:** The legacy grade system over-promoted some alerts to A/A+ even when Phase 2 was warning about weak confirmation, contradicting setup direction, or incomplete market context.
- **Biggest missed opportunity:** AAPL downside continuation. AAPL moved from the 314 area toward 309, but the bot mostly kept that as watch / Avoid / DO_NOT_CHASE instead of producing one clean, timely short alert.
- **Biggest false alert:** SPY 10:39 bearish SMS. It used a bullish liquidity sweep primary setup while the alert direction was bearish, so the label and trade idea conflicted even though the short-side move gave a small favorable push.
- **Main timing issue:** The bot often detected the correct directional pressure after the move was already underway. Several entries were marked LATE or DO_NOT_CHASE correctly, but the legacy alert grade still sometimes looked too strong.
- **Main tuning recommendation:** Keep Phase 2 as a hard veto for SMS when direction conflicts, confirmation is weak, market regime is unknown/choppy, or risk is HIGH/DO_NOT_CHASE. Keep watch rows visible, but make SMS much stricter.

Data used: today's `logs/alerts.jsonl` plus Alpaca 1-minute bars for AAPL, QQQ, and SPY from 9:30 ET through the review time.

## Alert-by-Alert Review

| Time ET | Symbol | Direction | Primary Setup | Confidence | Confirmation | Risk | Entry | Result | Timing | Notes |
|---|---:|---|---|---:|---:|---|---|---|---|---|
| 09:42 | AAPL | Bullish | 5-Min ORB Long | 89 HIGH | 58 NORMAL | LOW | LATE | Bad/weak | LATE | Only +0.07% favorable, then about -0.77% adverse over 30m. Correctly no SMS. |
| 09:47 | AAPL | Bearish | Possible Sweep - Wait | 48 LOW | 48 NORMAL | HIGH | LATE | Good watch | LATE | Watch caught reversal pressure; about +0.54% favorable over 30m, but confidence was too low for SMS. |
| 09:51 | AAPL | Bullish | Clean Breakout | 78 MEDIUM | 53 NORMAL | MEDIUM | GOOD_POSITION | Bad/weak | TOO EARLY | Breakout failed; about -0.62% adverse over 30m. No SMS was correct. |
| 09:53 | QQQ | Bearish | Bullish Liquidity Sweep Reclaim | 93 HIGH | 66 NORMAL | LOW | EARLY | Bad label | CONFLICTED | Bearish alert carried bullish primary setup. Small +0.25% favorable, but direction/label conflict should block SMS. |
| 09:57 | QQQ | Bearish | 5-Min ORB Short | 73 MEDIUM | 56 NORMAL | MEDIUM | LATE | Weak | LATE | +0.07% favorable, -0.46% adverse over 30m. Watch only was right. |
| 10:17 | SPY | Bearish SMS | Bearish Liquidity Sweep Rejection | 90 HIGH | 54 NORMAL | LOW | EARLY | Acceptable but small | EARLY | 30m max favorable about +0.10%, max adverse +0.02%. Tradable scalp only, not a strong move. |
| 10:27 | AAPL | Bearish watch | Clean Breakdown | 59 LOW | 50 NORMAL | HIGH | EARLY | Good missed SMS candidate | GOOD | +0.87% favorable over 30m. Bot saw it but kept it watch/low confidence. |
| 10:29 | QQQ | Bearish SMS | 5-Min ORB Short | 80 HIGH | 52 NORMAL | LOW | EARLY | Weak SMS | TOO EARLY | Went adverse first; only +0.14% max favorable and -0.15% adverse over 30m. Confirmation/candle warnings mattered. |
| 10:35 | AAPL | Bearish | 5-Min ORB Short | 89 HIGH | 52 NORMAL | LOW | GOOD_POSITION | Good missed alert | GOOD | +0.89% favorable, only +0.05% adverse. Score was low because legacy category/volume filters undercut it. |
| 10:39 | SPY | Bearish SMS | Bullish Liquidity Sweep Reclaim | 88 HIGH | 56 NORMAL | LOW | GOOD_POSITION | Bad label / mixed result | CONFLICTED | Short worked a little (+0.15% favorable), but primary setup was bullish. Should not be SMS. |
| 10:47 | AAPL | Bearish | 5-Min ORB Short | 100 HIGH | 56 NORMAL | DO_NOT_CHASE | DO_NOT_CHASE | Good read, late entry | DO_NOT_CHASE | +0.61% favorable, but after extension. Watch-only/blocked SMS was correct. |
| 10:50 | QQQ | Bearish SMS | 5-Min ORB Short | 100 HIGH | 60 NORMAL | LOW | GOOD_POSITION | Marginal | OK/weak | Only +0.11% favorable and +0.11% adverse; not enough edge for A+. |
| 11:04 | AAPL | Bearish | 5-Min ORB Short | 95 HIGH | 52 NORMAL | DO_NOT_CHASE | LATE | Late but directionally right | LATE | +0.28% favorable, +0.29% adverse. DO_NOT_CHASE warning was useful. |
| 11:24 | QQQ | Bearish SMS | 5-Min ORB Short | 100 HIGH | 58 NORMAL | LOW | GOOD_POSITION | Acceptable | GOOD | +0.21% favorable, +0.19% adverse. Reasonable scalp, but still not A+ quality. |
| 12:16 | AAPL | Bearish watch | Possible Sweep - Wait | 41 LOW | 44 WEAK | DO_NOT_CHASE | DO_NOT_CHASE | Directionally good, late | DO_NOT_CHASE | +0.54% favorable. Bot correctly warned, but this was part of the broader missed AAPL trend. |
| 12:25 | QQQ | Bearish watch | 5-Min ORB Short | 78 MEDIUM | 58 NORMAL | MEDIUM | EARLY | Good watch | GOOD | Continued lower after watch; useful but SMS filter stayed conservative. |
| 13:45 | SPY | Bearish SMS | 5-Min ORB Short | 100 HIGH | 66 NORMAL | LOW | EARLY | Bad SMS | TOO EARLY | Minimal favorable movement (+0.02%) and adverse +0.07%. Price was not below EMA9. |
| 13:48 | QQQ | Bearish SMS | 5-Min ORB Short | 100 HIGH | 58 NORMAL | LOW | EARLY | Bad SMS | TOO EARLY | No useful favorable follow-through; drifted against the alert over 15-30m. |

## Good Alerts

### AAPL 10:27 Bearish Watch - Clean Breakdown

- **Why it was good:** It identified the real downside continuation near the opening-range / premarket-low area.
- **Timing:** GOOD. It came before a meaningful continuation leg.
- **Post-alert move:** About +0.87% max favorable over 30 minutes, about +0.10% max adverse.
- **Trade opportunity:** Yes. This was one of the best practical alerts of the day, but it remained a watch because strategy confidence was only 59 LOW and risk was HIGH.
- **What to tune:** The bot should recognize a clean AAPL downside continuation after failed bullish attempts as a higher-quality short if price remains below VWAP/EMA9 and SPY/QQQ are weak.

### AAPL 10:35 Bearish - 5-Min ORB Short

- **Why it was good:** Direction and level were right; AAPL continued down with very little adverse movement.
- **Timing:** GOOD.
- **Post-alert move:** About +0.89% max favorable, about +0.05% max adverse.
- **Trade opportunity:** Yes. This was likely the best clean AAPL short opportunity in the reviewed data.
- **Issue:** The bot did not elevate it cleanly enough for SMS because the legacy category/score path did not align well with Phase 1/2 quality.

### QQQ 11:24 Bearish SMS - 5-Min ORB Short

- **Why it was good:** It caught a bearish continuation attempt in QQQ with high strategy confidence and acceptable confirmation.
- **Timing:** GOOD, though not perfect.
- **Post-alert move:** About +0.21% favorable and +0.19% adverse over 30 minutes.
- **Trade opportunity:** Reasonable scalp only. It should not be treated as an A+ trade; better as B/A- quality.

## Bad / Weak Alerts

### AAPL 09:42 Bullish ORB Long

- **Why it fired:** Price was breaking above early opening-range levels with high Phase 1 ORB confidence.
- **Why weak:** Entry was LATE, and the broader session soon rejected the move.
- **Post-alert move:** Only about +0.07% favorable, then roughly -0.77% adverse over 30 minutes.
- **Logic module that should warn harder:** Extension/timing and candle quality. This was an opening squeeze that failed quickly.

### AAPL 09:51 Clean Breakout

- **Why it fired:** Price reclaimed/broke key upside levels.
- **Why weak:** Confirmation was only 53 NORMAL, and it failed into the broader weakness.
- **Post-alert move:** Slight favorable/no follow-through, then about -0.62% adverse.
- **Needed warning:** More emphasis on failed follow-through after the initial 5-minute/15-minute period.

### QQQ 10:29 Bearish SMS

- **Why it fired:** 5-Min ORB Short with high strategy confidence and enough legacy score for SMS.
- **Why weak:** Confirmation was only 52, candle warning showed indecision, and it did not move cleanly right away.
- **Post-alert move:** Max favorable about +0.14%, max adverse about +0.15%.
- **Logic module:** Candle strength and confirmation score should have blocked A+ SMS.

### SPY 10:39 Bearish SMS

- **Why it fired:** Legacy scoring saw bearish premarket-low break conditions with tradable option quality.
- **Why bad/weak:** Primary setup was **Bullish Liquidity Sweep Reclaim** while alert direction was bearish. That is a label-direction conflict.
- **Post-alert move:** Small favorable move, but not enough to justify A+.
- **Logic module:** Strategy direction vs alert direction should hard-block SMS.

### SPY 13:45 / QQQ 13:48 Bearish SMS

- **Why they fired:** High relative volume / ORB short structure.
- **Why weak:** Follow-through was poor, and both were too early during midday chop.
- **Logic module:** Market regime, EMA/VWAP context, and candle confirmation should require stronger evidence for midday SMS.

## Missed Setups

### AAPL 10:27-10:35 Downside Continuation

- **Direction:** Bearish.
- **Setup type:** Breakdown / ORB continuation / trend-down continuation.
- **Key levels:** Opening range low near 314.17, premarket low near 313.94, VWAP above price.
- **What bot did:** It produced watch/filtered rows, including Clean Breakdown and 5-Min ORB Short.
- **What it missed:** A clean SMS-quality short opportunity once AAPL stayed below VWAP/EMA9 and continued lower.
- **Likely reason:** Strategy confidence/risk labels were too conservative early, then DO_NOT_CHASE appeared once move extended.

### QQQ Opening Dump 09:32-09:50

- **Direction:** Bearish.
- **Setup type:** Opening dump / ORB breakdown.
- **Key levels:** Opening range low and premarket low area.
- **What bot did:** Meaningful bearish rows appeared after the low was mostly already in.
- **What it missed:** Earlier warning that QQQ had a strong opening downside drive.
- **Likely reason:** Opening range formation and confirmation wait delayed detection.

### AAPL 10:47-11:15 Trend Continuation

- **Direction:** Bearish.
- **Setup type:** Sustained trend down / ORB continuation.
- **Key levels:** VWAP/EMA9 overhead, prior breakdown levels.
- **What bot did:** Repeatedly warned DO_NOT_CHASE.
- **What it missed:** It lacked a clean retest/hold entry label before the move extended.
- **Likely reason:** Retest-hold logic did not find a comfortable entry area; extension logic correctly became dominant.

## Timing Analysis

- **EARLY but useful:** SPY 10:17, QQQ 11:24, QQQ 12:25 watch.
- **GOOD timing:** AAPL 10:27 and 10:35 downside alerts/watches.
- **LATE:** AAPL 09:47 reversal down, QQQ 09:57 fast impulse down, many AAPL trend-down continuation rows after 10:47.
- **TOO EARLY / no confirmation:** AAPL 09:42 long, AAPL 09:51 breakout, QQQ 10:29 SMS, SPY 13:45 SMS, QQQ 13:48 SMS.
- **DO_NOT_CHASE correctly triggered:** AAPL downside continuation after the move extended.
- **MISSED:** AAPL clean short SMS and QQQ opening dump.

Overall timing pattern: the scanner is good at noticing levels, but it often either waits until the move is extended or fires before confirmation is truly clean. The best improvement is not more strategies; it is stricter timing/confirmation gating.

## Strategy Performance

| Strategy Type | Observed Behavior | Worked | Failed / Weak | Tuning Notes |
|---|---|---:|---:|---|
| Breakout / Breakdown | Detected many downside level breaks. | Moderate | Moderate | Needs stronger follow-through and candle-quality gating before SMS. |
| Liquidity Sweep | Caught sweep/reclaim/rejection patterns, but sometimes conflicted with alert direction. | Mixed | Several label conflicts | Direction conflict must block SMS. |
| VWAP Reclaim / Rejection | Useful context but often secondary. | Some | Some missed retest clarity | Improve retest/hold integration around VWAP. |
| Opening Range | Very active, especially ORB Short. | Good for AAPL/QQQ downside | Too many repeated rows | Add better dedupe/quality threshold for repeated ORB shorts. |
| Fakeout Risk | Warnings appeared often and were useful. | Good | Under-weighted for SMS | Fakeout/contradiction warnings should reduce SMS more aggressively. |
| Do Not Chase | Correctly flagged AAPL late shorts. | Good | Sometimes appeared after good entry had already passed | Need earlier “better entry” before extension. |

## Confirmation Performance

### Volume Quality / RVOL

- Helped identify weak moves and low-volume ORB attempts.
- It under-penalized some A+ SMS rows where confirmation was only NORMAL and follow-through was small.
- Suggested tuning: require stronger volume confirmation for SMS during the first 30 minutes and midday chop.

### Candle Strength

- Correctly flagged indecision/rejection on several weak alerts.
- Under-penalized QQQ 10:29 and SPY/QQQ midday SMS alerts.
- Suggested tuning: A+ SMS should require buyer/seller control aligned with direction, not INDECISION/REJECTION.

### Retest / Hold

- Helped with GOOD_POSITION labels.
- Missed the clean AAPL downside retest/continuation opportunity before extension took over.
- Suggested tuning: retest-hold should be allowed to upgrade AAPL trend continuation when price retests EMA9/VWAP underside and rejects.

### Extension / Exhaustion

- Worked well on AAPL downside. DO_NOT_CHASE was often correct.
- It may have over-blocked AAPL from a clean short once the trend was established, but that is safer than chasing.
- Suggested tuning: distinguish “trend continuation, pullback entry” from “late chase.”

### Relative Strength vs SPY/QQQ

- Helpful for AAPL context, but early logs often had “market comparison bars unavailable.”
- Suggested tuning: avoid SMS when relative-strength data is unavailable unless all other confirmation is very strong.

### Market Regime

- Choppy/unknown market context was a major issue.
- Suggested tuning: unknown/choppy market should suppress A+ grades and SMS unless the symbol is clearly leading with high confirmation.

### Pressure Score

- Stayed effectively disabled / UNKNOWN, as intended.
- No issue.

## Market Context Review

- SPY and QQQ were mostly weak/choppy, with downside pressure but poor clean trend confirmation at times.
- This helped bearish AAPL ideas and hurt bullish AAPL breakout attempts.
- AAPL showed early strength, then clear relative weakness as it broke down.
- The bot noticed some of this, but market/relative-strength availability warnings were under-weighted in SMS decisions.

## SMS Alert Review

SMS-qualified alerts observed:

- SPY 10:17 bearish: acceptable scalp, not A+ quality.
- QQQ 10:29 bearish: weak SMS, should likely have stayed watch.
- SPY 10:39 bearish: should not be SMS due setup-direction conflict.
- QQQ 10:50 bearish: marginal scalp, not A+.
- QQQ 11:24 bearish: probably the best SMS of the day, but still not A+.
- SPY 13:45 bearish: poor SMS; midday chop / weak follow-through.
- QQQ 13:48 bearish: poor SMS; weak follow-through.

Overall SMS quality: too noisy and too highly graded. Best fix is requiring Phase 2 alignment for SMS:

- no direction conflict
- confirmation score above threshold
- risk not HIGH/DO_NOT_CHASE
- candle aligned with direction
- market regime not UNKNOWN/CHOPPY unless symbol-specific strength/weakness is very clear

## Option Quality Review

- Option selection generally made sense directionally when the alert direction was correct.
- Spreads were usually acceptable and option scores were mostly in the tradable range.
- AAPL option candidates had acceptable spreads, but the stock setup quality was the limiting factor.
- OPRA was unavailable, so the bot used indicative option feed. That is usable for testing but should be treated cautiously for live SMS confidence.
- No major alert passed because of bad option spread; the issue was stock/strategy confirmation, not option quality.

## Final Recommendations

1. Keep all strategy logic intact for now.
2. Do not add new strategies yet.
3. Make Phase 2 a harder SMS gate:
   - block SMS on direction conflict
   - block SMS on HIGH / DO_NOT_CHASE risk
   - block SMS below confirmation threshold
   - suppress A/A+ when market regime is unknown/choppy
4. Improve AAPL bearish trend continuation handling:
   - recognize clean pullback/retest entries earlier
   - avoid waiting until DO_NOT_CHASE dominates
5. Reduce repeated ORB short noise:
   - keep dashboard rows
   - only SMS when a fresh break/retest occurs
6. Treat A+ as rare:
   - strong Phase 1
   - strong Phase 2
   - aligned candle
   - confirmed volume
   - non-choppy market
   - reasonable entry quality

Bottom line: the bot is directionally aware, but alert quality is not yet precise enough. It should stay in close testing mode. The next tuning pass should focus on SMS gating and timing quality, not adding more strategies.
