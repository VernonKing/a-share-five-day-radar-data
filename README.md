# A-share five-session data feed

This public repository is the data engine for the existing private five-session dashboard. It contains the A-share stock pool, ranking code, product catalog, tests, and the published `latest.json` feed.

GitHub Actions starts on weekdays at about 08:00 UTC (16:00 China time). The runner never performs its first market-data collection before 16:10, then retries temporary coverage gaps every five minutes through the final 16:40 attempt. It validates 98% A-share universe coverage, all four ranking groups, same-day quotes, and adjusted histories. Rejected attempts log the affected stock codes by reason. On holidays or terminal validation failure, the previous file remains intact.

To verify manually, use the repository's **Actions → Daily A-share refresh → Run workflow**. The workflow logs show coverage and the mainland session date. It uses only the repository's built-in `GITHUB_TOKEN`; no stored credential is needed.

Data providers: Tencent quotes, Sina adjusted daily bars through AkShare, and Sina futures daily bars for mapped products. A-share prices and market caps use CNY. Unmapped or unavailable product trends are explicitly labeled as unavailable. This is a market-data display, not investment advice.
