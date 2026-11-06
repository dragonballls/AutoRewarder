# Rewards activity discovery and manual review

The activity-discovery feature is integrated into the existing AutoRewarder window and run sequence. It does not create another application window.

## Existing Start Run integration

During a normal Start Run, after the existing PC/mobile phase reaches its daily-task stage, AutoRewarder performs one read-only discovery pass using the same WebDriver session and account profile. In Daily tasks only mode, the same pass runs after the existing daily-task and Visual Search steps.

The scanner reads the signed-in Rewards dashboard and /earn page, collects visible card labels, shown points, progress, and safe manual handoff URLs, then restores the tab and URL it started from. The existing Daily Set, More Activities, quest, search, Visual Search, and scheduler implementations are not rewritten by this feature.

## Test activities button

The **Test activities** button beside **Start run** runs the same scanner independently. It uses the selected account profile in headless mode, so it does not open a visible Edge window or another application window. The test shares the run lock and waits for browser warmup or balance refresh to finish before trying. Results appear in the existing Activity log.

The test button verifies whether the scanner can read the signed-in dashboard and recognize activity cards. A zero-result scan means no matching cards were recognized at that moment; it does not prove that the account has no available activities. Microsoft can change its dashboard markup or activity availability.

## Activity categories

The scanner classifies visible records as:
- Read to Earn / reading activities
- Explore on Bing / related topics
- Daily Set
- More Activities
- Punchcards / quests
- Quizzes
- Polls
- Other

It normalizes visible point values and progress where the dashboard exposes them, deduplicates matching cards, and displays only HTTPS manual links on Microsoft-owned Rewards, Bing, MSN, or Microsoft domains.

## Manual-only boundary

This feature is a read-only discovery and review layer. It does not click activity cards, submit or generate searches, open article links, simulate reading, answer quizzes or polls, claim points, switch accounts, or change Rewards state. Read to Earn and Explore activities are reported for you to complete yourself.

Microsoft's guidance says searches should be genuine and manually entered, and warns against using programs, bots, or macros to conduct Rewards searches. See [Microsoft Support: Limiting your searches in Microsoft Rewards](https://support.microsoft.com/en-us/accounts-billing/rewards/limiting-your-searches-in-microsoft-rewards).

## Optional offline snapshot review

A snapshot can also be prepared manually as JSON and reviewed without launching a browser:

    python -m src.manual_tasks.activity_discovery path\to\activities.json

For normalized JSON output:

    python -m src.manual_tasks.activity_discovery path\to\activities.json --json

## Tests

Run the isolated suite from the repository root:

    python -m unittest discover -s tests/manual_tasks -v

The tests cover classification, progress parsing, trusted-link validation, deduplication, malformed snapshots, size limits, report output, scanner page routing/restoration, partial page failures, and the absence of activity-execution commands in the read-only scanner script. They do not represent a live Microsoft Rewards compatibility test.
