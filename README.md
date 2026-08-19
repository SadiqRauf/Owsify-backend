# 💸 Owsify

> **Track expenses, split bills, and settle group balances effortlessly.**

**Owsify** is an open-source expense-splitting platform engineered to simplify group finances. Whether managing household rent, trips with friends, or shared dining bills, Owsify eliminates awkward money conversations by automating group debt calculation, tracking individual balances, and simplifying complex settlements.

### ✨ Key Features

* **Smart Expense Splitting:** Split bills equally, by percentage, or by exact custom amounts.
* **Simplified Debt Graph:** Integrated balance minimization algorithms to reduce the total number of transactions needed to settle up.
* **Group Management:** Organise expenses by trip, household, or project with multi-currency support.
* **Real-time Balance Tracking:** Instantly see who owes whom at any given moment.
* **Activity & Ledger Logs:** Full historical audit trail of settled payments and edited transactions.

### 🛠️ Tech Stack

* **Frontend:** React / React Native (TypeScript)
* **Backend:** Node.js / Express (or your backend framework)
* **Database:** PostgreSQL / MongoDB


# Splitwise Clone — Backend

FastAPI + PostgreSQL + SQLAlchemy 2.0 + Alembic, with JWT authentication.

## Requirements

- Python 3.13
- PostgreSQL 14+

## Setup

```bash
cd backend
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env          # then edit the values
```

Generate a real secret key before doing anything beyond local work:

```bash
./venv/bin/python -c "import secrets; print(secrets.token_urlsafe(64))"
```

## Database

This project talks to a dedicated cluster on **port 5433** so it does not collide
with any other PostgreSQL install on the machine.

```bash
# One-time: create the cluster
initdb -D /usr/local/var/splitwise-pg -U dev --auth-local=trust --auth-host=trust -E UTF8

# Start it
pg_ctl -D /usr/local/var/splitwise-pg -o "-p 5433" -l /usr/local/var/splitwise-pg/server.log start

# One-time: role and databases
psql -h 127.0.0.1 -p 5433 -U dev -d postgres <<'SQL'
CREATE ROLE splitwise WITH LOGIN PASSWORD 'splitwise' CREATEDB;
CREATE DATABASE splitwise OWNER splitwise;
CREATE DATABASE splitwise_test OWNER splitwise;
SQL
```

Stop it with `pg_ctl -D /usr/local/var/splitwise-pg stop`.

## Migrations

```bash
./venv/bin/alembic upgrade head              # apply
./venv/bin/alembic revision --autogenerate -m "message"
./venv/bin/alembic downgrade -1              # roll back one
./venv/bin/alembic current                   # what is applied
```

`alembic/env.py` pulls the URL and the model metadata from `app.core.config`, so
migrations always target the same database the app uses.

## Run

```bash
./venv/bin/uvicorn app.main:app --reload
```

| URL | What |
| --- | --- |
| <http://localhost:8000/docs> | Swagger UI (has a working **Authorize** button) |
| <http://localhost:8000/redoc> | ReDoc |
| <http://localhost:8000/api/v1/health> | Health + database check |

## Tests

```bash
./venv/bin/pytest
```

Tests run against `splitwise_test`; each one is wrapped in a transaction that is
rolled back, so the suite leaves no rows behind.

## Layout

```
app/
  core/          config, security (hashing + JWT), error types and handlers
  db/            declarative base, engine, session dependency
  models/        SQLAlchemy models
  schemas/       Pydantic request/response models
  services/      business logic, kept out of the route handlers
  api/
    deps.py      DbSession, CurrentUser, ActiveUser, SuperUser
    v1/          the versioned router and its endpoint modules
alembic/         migration environment and versions
tests/           pytest suite
```


### The balance engine

`app/services/balance.py` is where every statement about who owes whom comes from.
Three properties are maintained deliberately, because breaking any of them shows up
as money that has silently appeared or vanished:

1. **Integer cents.** No float touches a balance. Decimal in, cents through the
   arithmetic, Decimal out.
2. **Per currency.** Balances are never summed across currencies. Two people can owe
   each other in USD and EUR simultaneously and both are true; adding them would
   produce a number with no meaning. There is deliberately no combined total.
3. **Zero sum.** Within one currency and scope, every ledger sums to zero.
   `Ledger.assert_consistent()` states this and the test suite exercises it against
   randomly generated webs of debt.

The ledger stores **one signed number per pair** rather than two directed ones, so
the two directions cannot disagree. It is recomputed from the transactions on each
request rather than cached on a row: a stored balance drifts from its transactions
after an edit or delete, and a wrong balance that looks authoritative is worse than
a slow one.

**Debt simplification** (`GET /balances/groups/{id}/simplified`) reduces a web of
debts to the fewest transfers that settle everyone, by repeatedly matching the
largest debtor with the largest creditor. For the brief's example:

    Ali owes Sadiq $50, Ahmed owes Ali $30
    -> nets: Sadiq +50, Ali -20, Ahmed -30
    -> Ahmed pays Sadiq $30, Ali pays Sadiq $20

This deliberately reassigns who pays whom — Ahmed never borrowed from Sadiq
directly — which is why it is a separate view rather than a replacement for the
real pairwise debts.

### Settlements

A settlement is not an expense. An expense creates debt and divides between people;
a settlement discharges debt and moves money one way. They are separate tables so
no balance query has to remember which rows to treat differently.

Deleting a settlement restores the debt it discharged, because the balance is
derived rather than stored.

### Activity

`GET /activity` merges expenses and settlements into one feed, derived from those
tables rather than an append-only log. A derived feed can never describe something
that no longer exists; the cost is that only current state is visible, so a deleted
expense leaves the feed and edits show new values rather than a change history.

### Permissions

- A **group** is invisible to non-members: they get 404, not 403, so ids cannot be
  probed for existence.
- Group **owners** can delete the group and change roles. **Admins** can edit it and
  manage members. The owner cannot leave without transferring ownership first.
- A member who appears in any of the group's expenses cannot be removed until those
  expenses are dealt with, so nobody's debt silently disappears.
- An **expense** can be edited or deleted by whoever added it, whoever paid, or a
  group admin.
- Everyone on a group expense must be in that group. Everyone on a personal expense
  must be a friend, so debt cannot be pushed onto a stranger.

### Errors

Every failure uses one envelope, so clients only parse one shape:

```json
{
  "error": {
    "code": "invalid_credentials",
    "message": "Incorrect email or password.",
    "details": [{ "field": "email", "message": "…", "type": "value_error" }]
  },
  "request_id": "7131d919a311487b8e714d0da7e8fd92"
}
```

`request_id` is also returned as the `X-Request-ID` header and is written to the
log line for the same request.

### Tokens

- **Access token** — stateless JWT, 30 minutes by default, carries `type: access`.
- **Refresh token** — JWT carrying `type: refresh` and a `jti` mirrored by a row
  in `refresh_tokens`, which is what allows revocation.

Refreshing **rotates**: the presented token is revoked and a new pair issued. If a
revoked token is presented again the whole user's sessions are dropped, since a
replay is the signature of a stolen token. An access token is never accepted where
a refresh token is expected, or the other way round.
