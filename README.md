# A-share five-session data feed

This public repository is the data engine for the existing private A-share dashboard. It contains only the three A-share stock pools, ranking code, product catalog, tests, and the published `latest.json` feed.

GitHub Actions runs on weekdays at 07:40 UTC (15:40 China time), waits until 16:00 China time before the first market-data collection, and retries temporary coverage gaps every five minutes through 16:30. It validates 98% universe coverage, all 32 ranked cards, same-day quotes, and adjusted histories. Rejected attempts log the affected stock codes by reason. On holidays or terminal validation failure, the previous file remains intact. Scheduled GitHub Actions runs can be delayed or missed, so the data date is displayed on the dashboard.

To verify manually, use the repository's **Actions → Daily A-share refresh → Run workflow**. The workflow logs show coverage and session date. It uses only the repository's built-in `GITHUB_TOKEN`; no stored credential is needed.

Data providers: Tencent quotes and adjusted daily bars, Sina adjusted fallback through AkShare, and Sina futures daily bars for mapped products. Unmapped or unavailable product trends are explicitly labeled as unavailable. This is a market-data display, not investment advice.
