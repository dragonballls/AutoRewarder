# Manual Rewards activity discovery

This is an isolated, offline review helper. It does not modify the existing Rewards task runner and does not connect to Microsoft services.

## What it does

- Classifies user-supplied activity records as Read to Earn, Explore on Bing, Daily Set, punchcard/quest, quiz, poll, More Activities, or other.
- Normalizes visible points and progress where supplied.
- Removes exact duplicate records.
- Displays only HTTPS links on Microsoft-owned Rewards, Bing, MSN, or Microsoft domains, for the user to open manually.
- Produces a Markdown review report or normalized JSON.

## What it intentionally does not do

- It does not log in, access cookies, call Rewards endpoints, open pages, click activity cards, submit searches, read articles, answer quizzes/polls, or claim points.
- It does not generate search phrases or imitate human input.
- It does not alter src/dailytasks/runner.py, src/dailytasks/new_dashboard.py, src/search/, src/emulator/, or the existing scheduler.

Microsoft says Rewards searches must be manually entered for genuine personal research and specifically advises against programs, bots, and macros for searching. See [Microsoft Support: Limiting your searches in Microsoft Rewards](https://support.microsoft.com/en-us/accounts-billing/rewards/limiting-your-searches-in-microsoft-rewards).

## Input format

Create a JSON file yourself using the activity details shown on your Rewards dashboard. For example:

    {
      "activities": [
        {
          "title": "Read to Earn",
          "description": "Read an available MSN story",
          "section": "More Activities",
          "points": 5,
          "status": "available",
          "url": "https://www.msn.com/"
        },
        {
          "title": "Explore on Bing: Space",
          "section": "More Activities",
          "points": 10,
          "progress": "0/1",
          "url": "https://www.bing.com/"
        }
      ]
    }

Only include details actually shown to you. Do not add an activity or point value based on assumptions.

## Run it

From the repository root:

    python -m src.manual_tasks.activity_discovery path\to\activities.json

For normalized JSON:

    python -m src.manual_tasks.activity_discovery path\to\activities.json --json

## Tests

Run the isolated tests with:

    python -m unittest discover -s tests/manual_tasks -v

These tests cover classification, progress parsing, trusted-link validation, deduplication, malformed snapshots, size limits, and report output. They do not simulate a live Microsoft Rewards session.
