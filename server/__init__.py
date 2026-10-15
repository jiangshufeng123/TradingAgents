"""The TradingAgents system service: accounts, per-user data, jobs and (later) the web API.

Layout (see the project plan, Web系统方案_v2):

* ``db``        engine and sessions; SQLite by default, PostgreSQL by URL later
* ``models``    users, watchlists, holdings, jobs, costs, schedules
* ``security``  password hashing
* ``services``  per-user operations the API and the CLI share; they call the
                tradingagents core and never duplicate its logic
* ``importer``  brings the single-user files (watchlist, portfolio, paper
                account, cost ledger) into an account

What is shared by everyone stays where it is: market-data caches, screening
data, board lots, and the decision memory log with its learned beliefs.
"""
